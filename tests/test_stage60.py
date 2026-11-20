import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from persona_sidecar.affect import advance_state, apply_label, get_state, initial_state, state_file_path
from persona_sidecar.classifier_worker import ClassifierWorker
from persona_sidecar.database import connect, verify_audit
from persona_sidecar.gateway import SidecarService
from persona_sidecar.queue import claim_due, enqueue_message, recover_inflight
from persona_sidecar.scheduler import Scheduler
from tests.helpers import FakeAdapter, FakeEgress, FailingClassifier, make_config


class Stage60Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "sidecar.sqlite3"
        self.config = make_config(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_16_six_dimensions_clamp_and_decay_toward_anchor(self):
        anchor = self.config["personas"]["default"]["anchor"]
        state = initial_state(anchor, 0)
        state["base"] = {key: 1.4 for key in anchor}
        state["mood"] = {key: 0.9 for key in anchor}
        after = advance_state(state, anchor, 24 * 3600, self.config["affect"])
        self.assertEqual(set(after["base"]), set(anchor))
        self.assertEqual(set(after["mood"]), set(anchor))
        for key in anchor:
            self.assertGreaterEqual(after["base"][key], 0.0)
            self.assertLessEqual(after["base"][key], 1.0)
            self.assertGreaterEqual(after["mood"][key], 0.0)
            self.assertLessEqual(after["mood"][key], 1.0)
            self.assertLess(abs(after["mood"][key] - anchor[key]), abs(0.9 - anchor[key]))

    def test_17_repeated_label_inside_15_minutes_attenuates(self):
        first = apply_label(self.db_path, self.config, "p1", "playful", 1000, arousal=1.0)
        second = apply_label(self.db_path, self.config, "p1", "playful", 1060, arousal=1.0)
        self.assertEqual(first["prior_same_label"], 0)
        self.assertEqual(second["prior_same_label"], 1)
        self.assertAlmostEqual(first["habituation_scale"], 1.0)
        self.assertAlmostEqual(second["habituation_scale"], 0.7)
        self.assertLess(second["deltas"]["play"], first["deltas"]["play"])

    def test_18_classifier_failure_leaves_state_untouched_and_does_not_block_send(self):
        result = enqueue_message(
            self.db_path, self.config, "p1", "Hello there.", "a user message",
            "stage18-key", now=1000,
        )
        adapter = FakeAdapter()
        scheduler = Scheduler(
            self.db_path, adapter, FakeEgress(), self.config["delivery"],
            clock=lambda: 100000,
        )
        self.assertTrue(scheduler.process_one(now=100000))
        self.assertEqual(len(adapter.calls), 1)

        worker = ClassifierWorker(
            self.db_path, self.config, FailingClassifier(), clock=lambda: 100001,
        )
        self.assertTrue(worker.process_one(now=100001))
        conn = connect(self.db_path)
        try:
            self.assertFalse(state_file_path(self.config, "p1").exists())
            job = conn.execute(
                "SELECT status FROM classifier_jobs WHERE message_id=?",
                (result["message_id"],),
            ).fetchone()
            self.assertEqual(job["status"], "failed")
            sent = conn.execute(
                "SELECT status,observed_ip FROM delivery_queue WHERE message_id=?",
                (result["message_id"],),
            ).fetchone()
            self.assertEqual(sent["status"], "sent")
            self.assertEqual(sent["observed_ip"], "203.0.113.7")
        finally:
            conn.close()

    def test_19_state_survives_reopen_and_is_per_persona(self):
        first = apply_label(
            self.db_path, self.config, "persona-a", "playful", 5000, arousal=1.0
        )
        anchor = self.config["personas"]["default"]["anchor"]
        self.assertGreater(first["state"]["base"]["play"], anchor["play"])
        reopened = get_state(self.db_path, self.config, "persona-a", 5000)
        other = get_state(self.db_path, self.config, "persona-b", 5000)
        self.assertAlmostEqual(reopened["base"]["play"], first["state"]["base"]["play"])
        self.assertAlmostEqual(other["base"]["play"], anchor["play"])
        self.assertTrue(state_file_path(self.config, "persona-a").exists())
        self.assertTrue(state_file_path(self.config, "persona-b").exists())
        self.assertEqual(len(list(Path(self.config["state"]["directory"]).glob("persona-*.json"))), 2)

    def test_file_queue_recovers_with_stable_idempotency_key(self):
        enqueued = enqueue_message(
            self.db_path, self.config, "p1", "recover me",
            request_key="recover-key", now=10,
        )
        first = claim_due(self.db_path, now=1000)
        self.assertEqual(first["message_id"], enqueued["message_id"])
        adapter = FakeAdapter()
        adapter.send(
            persona_id=first["persona_id"], message_id=first["message_id"],
            burst_index=first["burst_index"], text=first["text"],
            idempotency_key=first["idempotency_key"], observed_ip="203.0.113.7",
        )
        recover_inflight(self.db_path, now=1001)
        scheduler = Scheduler(
            self.db_path, adapter, FakeEgress(), self.config["delivery"],
            clock=lambda: 1002,
        )
        self.assertTrue(scheduler.process_one(now=1002))
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(adapter.effects, {first["idempotency_key"]})
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT status,observed_ip FROM delivery_queue WHERE message_id=?",
                (enqueued["message_id"],),
            ).fetchone()
            self.assertEqual(row["status"], "sent")
            self.assertEqual(row["observed_ip"], "203.0.113.7")
        finally:
            conn.close()

    def test_audit_chain_verifies_and_rejects_mutation(self):
        enqueue_message(
            self.db_path, self.config, "p1", "audit me",
            request_key="audit-key", now=10,
        )
        conn = connect(self.db_path)
        try:
            self.assertTrue(verify_audit(conn)["valid"])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE audit_log SET event_json='{}' WHERE seq=1")
        finally:
            conn.close()

    def test_gateway_requires_token_and_enforces_rate_limit(self):
        self.config["gateway"]["requests_per_minute"] = 2
        service = SidecarService(
            self.config, adapter=FakeAdapter(), egress=FakeEgress(),
            classifier=FailingClassifier(), token="test-secret",
        )
        server = service.make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/internal/health", timeout=2)
            self.assertEqual(error.exception.code, 401)
            headers = {"Authorization": "Bearer test-secret"}
            self.assertEqual(urlopen(Request(base + "/internal/health", headers=headers), timeout=2).status, 200)
            self.assertEqual(urlopen(Request(base + "/internal/status", headers=headers), timeout=2).status, 200)
            with self.assertRaises(HTTPError) as limited:
                urlopen(Request(base + "/internal/health", headers=headers), timeout=2)
            self.assertEqual(limited.exception.code, 429)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_outgoing_endpoint_persists_before_ack_and_is_idempotent(self):
        service = SidecarService(
            self.config, adapter=FakeAdapter(), egress=FakeEgress(), token="test-secret",
        )
        server = service.make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            payload = json.dumps({
                "persona_id": "default", "text": "hello", "classify_text": "hi there",
            }).encode()
            headers = {
                "Authorization": "Bearer test-secret",
                "Content-Type": "application/json",
                "Idempotency-Key": "gateway-idem",
            }
            first = Request(base + "/internal/send", data=payload, headers=headers, method="POST")
            with urlopen(first, timeout=2) as response:
                accepted = json.loads(response.read())
                self.assertEqual(response.status, 202)
            second = Request(base + "/internal/send", data=payload, headers=headers, method="POST")
            with urlopen(second, timeout=2) as response:
                duplicate = json.loads(response.read())
            self.assertEqual(accepted["message_id"], duplicate["message_id"])
            self.assertFalse(accepted["duplicate"])
            self.assertTrue(duplicate["duplicate"])
            conn = connect(self.db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0], accepted["queued_bursts"])
            finally:
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
