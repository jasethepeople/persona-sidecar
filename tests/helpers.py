import copy
import socket
from pathlib import Path

from persona_sidecar.config import DEFAULT_CONFIG
from persona_sidecar.database import init_db


def make_config(db_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    db_path = Path(db_path)
    config["database"]["path"] = str(db_path)
    config["state"]["directory"] = str(db_path.parent / "personas")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config["server"]["port"] = sock.getsockname()[1]
    config["gateway"]["requests_per_minute"] = 20
    config["delivery"]["poll_seconds"] = 0.05
    config["delivery"]["max_attempts"] = 5
    config["timing"].update({
        "latency_median_seconds": 0.05,
        "latency_sigma": 0.0,
        "reading_wpm": 240,
        "max_burst_chars": 40,
    })
    config["damage"]["enabled"] = False
    config["classifier"]["poll_seconds"] = 0.05
    init_db(db_path)
    return config


class FakeEgress:
    def observe_ip(self):
        return "203.0.113.7", None


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.effects = set()

    @property
    def applied_keys(self):
        return self.effects

    def send(self, **kwargs):
        self.calls.append(kwargs)
        self.effects.add(kwargs["idempotency_key"])
        return {"status": 200}


class FailingClassifier:
    def classify(self, _text):
        raise TimeoutError("offline test timeout")
