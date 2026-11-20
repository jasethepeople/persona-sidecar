import random
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from persona_sidecar.damage import damage_text, split_bursts
from persona_sidecar.egress import Egress
from persona_sidecar.timing import cumulative_schedule


class DeliveryAndTimingTests(unittest.TestCase):
    def test_timing_is_seeded_cumulative_and_reading_speed_scaled(self):
        bursts = ["one two", "three four five"]
        settings = {
            "reading_wpm": 120,
            "latency_median_seconds": 2,
            "latency_sigma": 0.5,
        }
        first = cumulative_schedule(bursts, settings, random.Random(7), 100)
        repeated = cumulative_schedule(bursts, settings, random.Random(7), 100)
        faster = cumulative_schedule(
            bursts, {**settings, "reading_wpm": 600}, random.Random(7), 100
        )
        self.assertEqual(first, repeated)
        self.assertGreater(first[0]["due_at"], 100)
        self.assertGreater(first[1]["due_at"], first[0]["due_at"])
        self.assertGreater(first[-1]["due_at"] - 100, faster[-1]["due_at"] - 100)

    def test_split_bursts_preserves_sentence_boundaries_and_limit(self):
        pieces = split_bursts("First sentence. " + ("word " * 50), max_chars=30)
        self.assertEqual(pieces[0], "First sentence.")
        self.assertTrue(all(len(piece) <= 30 for piece in pieces))

    def test_text_damage_supports_typos_lowercase_punctuation_and_correction(self):
        damaged = damage_text(
            "Hello there.",
            {
                "enabled": True,
                "lowercase_start_probability": 1,
                "drop_terminal_punctuation_probability": 1,
                "self_correction_probability": 1,
                "typo_probabilities": {
                    "substitution": 1,
                    "transposition": 0,
                    "deletion": 0,
                    "insertion": 0,
                },
            },
            random.Random(4),
        )
        self.assertGreaterEqual(len(damaged), 2)
        self.assertTrue(damaged[0][0].islower())
        self.assertFalse(damaged[0].endswith("."))
        self.assertTrue(damaged[1].startswith("*"))

    def test_direct_egress_reaches_loopback_http_without_environment_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"127.0.0.1"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            egress = Egress({"mode": "direct", "timeout_seconds": 1})
            status, body, _headers = egress.request(
                f"http://127.0.0.1:{server.server_address[1]}/"
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, b"127.0.0.1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
