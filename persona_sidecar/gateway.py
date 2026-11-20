import hashlib
import hmac
import ipaddress
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .affect import format_context, get_state
from .adapters import WebhookAdapter
from .classifier import create_classifier
from .classifier_worker import ClassifierWorker
from .config import validate_config
from .database import connect, init_db, transaction, verify_audit
from .egress import Egress
from .queue import enqueue_message, queue_status, recover_inflight
from .scheduler import Scheduler


class SidecarService:
    def __init__(self, config, adapter=None, egress=None, classifier=None, token=None, clock=time.time):
        self.config = validate_config(config)
        self.db_path = self.config["database"]["path"]
        self.clock = clock
        init_db(self.db_path)
        self.token = token
        self.egress = egress or Egress(self.config["egress"])
        self.adapter = adapter or WebhookAdapter(self.config["delivery"], self.egress)
        self.classifier = classifier or create_classifier(self.config["classifier"])
        self.scheduler = Scheduler(
            self.db_path, self.adapter, self.egress, self.config["delivery"], clock=clock
        )
        self.classifier_worker = ClassifierWorker(
            self.db_path, self.config, self.classifier, clock=clock
        )
        self.stop_event = threading.Event()
        self.log = logging.getLogger("persona_sidecar")

    def _gateway_token(self):
        if self.token is not None:
            if not isinstance(self.token, str) or len(self.token) < 8:
                raise RuntimeError("injected gateway token must contain at least 8 characters")
            return self.token
        name = self.config["gateway"]["token_env"]
        token = os.environ.get(name, "")
        if len(token) < 24:
            raise RuntimeError(f"environment variable {name} must contain a gateway token of at least 24 characters")
        return token

    def _rate_limit(self, token, now):
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        window = int(float(now) // 60)
        limit = int(self.config["gateway"]["requests_per_minute"])
        conn = connect(self.db_path)
        try:
            with transaction(conn):
                row = conn.execute(
                    "SELECT window_start,request_count FROM rate_buckets WHERE token_hash=?",
                    (digest,),
                ).fetchone()
                count = int(row["request_count"]) + 1 if row and int(row["window_start"]) == window else 1
                conn.execute(
                    "INSERT INTO rate_buckets(token_hash,window_start,request_count) VALUES(?,?,?) "
                    "ON CONFLICT(token_hash) DO UPDATE SET window_start=excluded.window_start,request_count=excluded.request_count",
                    (digest, window, count),
                )
                return count <= limit
        finally:
            conn.close()

    def make_server(self):
        service = self
        config = self.config
        token = self._gateway_token()

        class Handler(BaseHTTPRequestHandler):
            server_version = "PersonaSidecar/0.1"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                service.log.info("gateway %s", fmt % args)

            def _respond(self, status, payload, content_type="application/json"):
                if isinstance(payload, bytes):
                    body = payload
                elif isinstance(payload, str) and content_type == "text/plain; charset=utf-8":
                    body = payload.encode("utf-8")
                else:
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if status == 429:
                    self.send_header("Retry-After", "60")
                self.end_headers()
                self.wfile.write(body)

            def _authorize(self):
                try:
                    remote = ipaddress.ip_address(self.client_address[0])
                    if not remote.is_loopback:
                        self._respond(403, {"error": "loopback only"})
                        return False
                except ValueError:
                    self._respond(403, {"error": "loopback only"})
                    return False
                auth = self.headers.get("Authorization", "")
                supplied = auth[7:] if auth.startswith("Bearer ") else ""
                if not supplied or not hmac.compare_digest(supplied, token):
                    self._respond(401, {"error": "unauthorized"})
                    return False
                if not service._rate_limit(token, service.clock()):
                    self._respond(429, {"error": "rate limit exceeded", "retry_after_seconds": 60})
                    return False
                return True

            def do_GET(self):
                if not self._authorize():
                    return
                parsed = urlsplit(self.path)
                if parsed.path == "/internal/health":
                    self._respond(200, {"ok": True})
                elif parsed.path == "/internal/status":
                    self._respond(200, queue_status(service.db_path))
                elif parsed.path == "/internal/context":
                    persona_id = parse_qs(parsed.query).get("persona", [""])[0]
                    if not persona_id or len(persona_id) > 128:
                        self._respond(400, {"error": "persona query parameter required"})
                        return
                    state = get_state(service.db_path, config, persona_id, service.clock())
                    self._respond(200, format_context(state), "text/plain; charset=utf-8")
                elif parsed.path == "/internal/audit/verify":
                    conn = connect(service.db_path)
                    try:
                        self._respond(200, verify_audit(conn))
                    finally:
                        conn.close()
                else:
                    self._respond(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorize():
                    return
                if urlsplit(self.path).path != "/internal/send":
                    self._respond(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    max_bytes = int(config["server"]["max_body_bytes"])
                    if length <= 0 or length > max_bytes:
                        self._respond(413 if length > max_bytes else 400, {"error": "invalid request size"})
                        return
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("JSON object required")
                    result = enqueue_message(
                        service.db_path,
                        config,
                        payload.get("persona_id", ""),
                        payload.get("text", ""),
                        payload.get("classify_text"),
                        self.headers.get("Idempotency-Key"),
                        now=service.clock(),
                    )
                    self._respond(202, result)
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._respond(400, {"error": str(exc)})
                except Exception:
                    service.log.exception("send request failed")
                    self._respond(500, {"error": "internal error"})

        return ThreadingHTTPServer(
            (config["server"]["host"], int(config["server"]["port"])), Handler
        )

    def serve(self):
        server = self.make_server()
        self.classifier_worker.recover_processing()
        recover_inflight(self.db_path, self.clock())
        scheduler_thread = threading.Thread(
            target=self.scheduler.run, args=(self.stop_event,),
            name="sidecar-scheduler", daemon=True,
        )
        classifier_thread = threading.Thread(
            target=self.classifier_worker.run, args=(self.stop_event,),
            name="sidecar-classifier", daemon=True,
        )
        scheduler_thread.start()
        classifier_thread.start()
        self.log.info("sidecar listening on %s:%s", *server.server_address[:2])
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            server.server_close()
            scheduler_thread.join(timeout=2)
            classifier_thread.join(timeout=2)
