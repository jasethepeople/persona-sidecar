#!/usr/bin/env python3
"""PLACEHOLDER receiver for the persona sidecar (Nov 19, 2026).

Listens on 127.0.0.1:24700 and accepts POST /messages from the sidecar's
webhook adapter. It deduplicates Idempotency-Key values (persisted, so a
restart keeps the set) and appends each accepted delivery to
data/receiver-deliveries.jsonl.

This is test infrastructure, NOT the real destination for persona
messages. When the real receiver exists, point the sidecar's
delivery.url at it instead (one line in drives.config.json) and stop
this service.
"""
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
LOG = os.path.join(DATA, "receiver-deliveries.jsonl")
KEYS = os.path.join(DATA, "receiver-seen-keys.json")
LOCK = threading.Lock()


def _load_keys():
    try:
        with open(KEYS) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_keys(keys):
    tmp = KEYS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(keys), f)
    os.replace(tmp, KEYS)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'{"ok": true, "placeholder": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/messages":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        key = self.headers.get("Idempotency-Key", "")
        with LOCK:
            seen = _load_keys()
            if key and key in seen:
                body = b'{"received": false, "duplicate": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                payload = {"_raw_unparseable": True}
            os.makedirs(DATA, exist_ok=True)
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "idempotency_key": key,
                "payload": payload,
            }
            with open(LOG, "a") as f:
                f.write(json.dumps(record) + "\n")
            if key:
                seen.add(key)
                _save_keys(seen)
        body = b'{"received": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", 24700), Handler).serve_forever()
