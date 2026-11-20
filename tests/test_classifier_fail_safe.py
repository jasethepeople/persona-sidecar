import tempfile
import unittest
from pathlib import Path
from unittest import mock

from persona_sidecar.classifier import OllamaClassifier, RulesClassifier
from persona_sidecar.affect import state_file_path
from persona_sidecar.classifier_worker import ClassifierWorker
from persona_sidecar.database import connect
from persona_sidecar.queue import enqueue_message
from persona_sidecar.scheduler import Scheduler
from tests.helpers import FakeAdapter, FakeEgress, make_config


class ClassifierFailSafeTests(unittest.TestCase):
    def run_delivery_with_failure(self, classifier, fail):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory) / "sidecar.sqlite3")
            item = enqueue_message(Path(config["database"]["path"]), config, "p1", "Please classify me.", "a user message", request_key="failsafe-key", now=1000)
            adapter = FakeAdapter()
            scheduler = Scheduler(Path(config["database"]["path"]), adapter, FakeEgress(), config["delivery"], clock=lambda: 100000)
            self.assertTrue(scheduler.process_one(now=100000))
            with fail:
                worker = ClassifierWorker(Path(config["database"]["path"]), config, classifier, clock=lambda: 100001)
                self.assertTrue(worker.process_one(now=100001))
            self.assertEqual(len(adapter.calls), 1)
            self.assertFalse(state_file_path(config, "p1").exists())
            conn = connect(Path(config["database"]["path"]))
            try:
                job = conn.execute("SELECT status FROM classifier_jobs WHERE message_id=?", (item["message_id"],)).fetchone()
                self.assertEqual(job["status"], "failed")
                delivery = conn.execute("SELECT status FROM delivery_queue WHERE message_id=?", (item["message_id"],)).fetchone()
                self.assertEqual(delivery["status"], "sent")
            finally:
                conn.close()

    def test_rules_backend_failure_does_not_block_delivery_or_mutate_affect(self):
        classifier = RulesClassifier()
        failure = mock.patch.object(classifier, "classify", side_effect=TimeoutError("simulated classifier stall"))
        self.run_delivery_with_failure(classifier, failure)

    def test_ollama_backend_timeout_does_not_block_delivery_or_mutate_affect(self):
        settings = {"endpoint": "http://127.0.0.1:11434/api/chat", "model": "qwen2.5:0.5b", "timeout_seconds": 8}
        classifier = OllamaClassifier(settings)
        failure = mock.patch.object(classifier, "classify", side_effect=TimeoutError("simulated Ollama timeout"))
        self.run_delivery_with_failure(classifier, failure)


if __name__ == "__main__":
    unittest.main()
