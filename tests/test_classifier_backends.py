import json
import tempfile
import unittest
from pathlib import Path

from persona_sidecar.affect import apply_label, get_state
from persona_sidecar.classifier import RulesClassifier, create_classifier
from persona_sidecar.config import DEFAULT_CONFIG
from tests.helpers import make_config


ROOT = Path(__file__).resolve().parents[1]


class ClassifierBackendTests(unittest.TestCase):
    def test_rules_backend_is_the_default_and_matches_labelled_samples(self):
        self.assertEqual(DEFAULT_CONFIG["classifier"]["backend"], "rules")
        classifier = create_classifier(DEFAULT_CONFIG["classifier"])
        samples = json.loads((ROOT / "tests" / "fixtures" / "classifier_samples.json").read_text(encoding="utf-8"))
        self.assertEqual(len(samples), 22)
        self.assertEqual(sum(classifier.classify(item["text"])[0] == item["label"] for item in samples), 22)

    def test_word_boundaries_avoid_substring_false_positives(self):
        classifier = RulesClassifier()
        self.assertEqual(classifier.classify("I love these gloves")[0], "affectionate")
        self.assertEqual(classifier.classify("These gloves are comfortable")[0], "neutral")

    def test_classifier_result_moves_affect_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory) / "sidecar.sqlite3")
            classifier = RulesClassifier()
            before = get_state(Path(config["database"]["path"]), config, "p1", 1000)
            label, arousal = classifier.classify("Haha, that was fun — just joking!")
            result = apply_label(Path(config["database"]["path"]), config, "p1", label, 1001, arousal=arousal)
            self.assertEqual(label, "playful")
            self.assertGreater(result["state"]["base"]["play"], before["base"]["play"])

    def test_ollama_is_selectable_without_changing_the_safe_default(self):
        settings = dict(DEFAULT_CONFIG["classifier"], backend="ollama", model="qwen2.5:0.5b")
        self.assertEqual(create_classifier(settings).__class__.__name__, "OllamaClassifier")
        self.assertEqual(DEFAULT_CONFIG["classifier"]["backend"], "rules")

    def test_ollama_request_uses_compact_prompt_and_configured_deadline(self):
        from unittest import mock

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return b'{"message":{"content":"{\\"label\\":\\"struggling\\",\\"arousal\\":0.75}"}}'

        class Opener:
            def open(self, request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return Response()

        captured = {}
        settings = dict(DEFAULT_CONFIG["classifier"], backend="ollama", model="qwen2.5:0.5b", timeout_seconds=8)
        classifier = create_classifier(settings)
        with mock.patch("persona_sidecar.classifier.urllib.request.build_opener", return_value=Opener()):
            self.assertEqual(classifier.classify("I am overwhelmed today."), ("struggling", 0.75))
        payload = json.loads(captured["request"].data)
        prompt = payload["messages"][0]["content"]
        self.assertLess(len(prompt), 260)
        self.assertEqual(captured["timeout"], 8)
        self.assertEqual(payload["model"], "qwen2.5:0.5b")
        self.assertEqual(captured["request"].full_url, "http://127.0.0.1:11434/api/chat")


    def test_ollama_timeout_is_bounded_and_propagates_as_failure(self):
        from unittest import mock

        captured = {}

        class TimeoutOpener:
            def open(self, request, timeout):
                captured["timeout"] = timeout
                raise TimeoutError("mock inference deadline")

        settings = dict(DEFAULT_CONFIG["classifier"], backend="ollama", model="qwen2.5:0.5b", timeout_seconds=0.02)
        classifier = create_classifier(settings)
        with mock.patch("persona_sidecar.classifier.urllib.request.build_opener", return_value=TimeoutOpener()):
            with self.assertRaises(TimeoutError):
                classifier.classify("I feel overwhelmed.")
        self.assertEqual(captured["timeout"], 0.02)


    def test_ollama_timeout_is_fail_safe_after_delivery(self):
        from unittest import mock

        from persona_sidecar.affect import state_file_path
        from persona_sidecar.classifier_worker import ClassifierWorker
        from persona_sidecar.database import connect
        from persona_sidecar.queue import enqueue_message
        from persona_sidecar.scheduler import Scheduler
        from tests.helpers import FakeAdapter, FakeEgress

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory) / "sidecar.sqlite3")
            db_path = Path(config["database"]["path"])
            queued = enqueue_message(
                db_path, config, "p1", "I am overwhelmed today.", "I am overwhelmed",
                "ollama-timeout-key", now=1000,
            )
            adapter = FakeAdapter()
            scheduler = Scheduler(
                db_path, adapter, FakeEgress(), config["delivery"], clock=lambda: 100000,
            )
            self.assertTrue(scheduler.process_one(now=100000))
            self.assertEqual(len(adapter.calls), 1)

            settings = dict(DEFAULT_CONFIG["classifier"], backend="ollama", model="qwen2.5:0.5b", timeout_seconds=0.01)
            classifier = create_classifier(settings)
            captured = {}

            class TimeoutOpener:
                def open(self, _request, timeout):
                    captured["timeout"] = timeout
                    raise TimeoutError("bounded test timeout")

            worker = ClassifierWorker(db_path, config, classifier, clock=lambda: 100001)
            with mock.patch("persona_sidecar.classifier.urllib.request.build_opener", return_value=TimeoutOpener()):
                self.assertTrue(worker.process_one(now=100001))
            self.assertEqual(captured["timeout"], 0.01)
            self.assertFalse(state_file_path(config, "p1").exists())

            conn = connect(db_path)
            try:
                job = conn.execute("SELECT status,error_type FROM classifier_jobs WHERE message_id=?", (queued["message_id"],)).fetchone()
                sent = conn.execute("SELECT status FROM delivery_queue WHERE message_id=?", (queued["message_id"],)).fetchone()
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["error_type"], "TimeoutError")
                self.assertEqual(sent["status"], "sent")
            finally:
                conn.close()

    def test_ollama_slow_backend_obeys_timeout(self):
        import time
        from unittest import mock

        captured = {}

        class SlowOpener:
            def open(self, request, timeout):
                captured["timeout"] = timeout
                time.sleep(timeout + 0.01)
                raise TimeoutError("simulated Ollama stall")

        settings = dict(DEFAULT_CONFIG["classifier"], backend="ollama", model="qwen2.5:0.5b", timeout_seconds=0.02)
        classifier = create_classifier(settings)
        started = time.perf_counter()
        with mock.patch("persona_sidecar.classifier.urllib.request.build_opener", return_value=SlowOpener()):
            with self.assertRaises(TimeoutError):
                classifier.classify("I am overwhelmed today.")
        self.assertEqual(captured["timeout"], 0.02)
        self.assertLess(time.perf_counter() - started, 0.2)


if __name__ == "__main__":
    unittest.main()
