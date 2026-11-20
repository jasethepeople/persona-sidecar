import argparse
import copy
import random
import tempfile
from pathlib import Path

from .affect import advance_state, apply_label, get_state, initial_state, state_file_path
from .config import DEFAULT_CONFIG
from .database import connect, init_db, verify_audit
from .queue import claim_due, enqueue_message, recover_inflight
from .scheduler import Scheduler
from .timing import cumulative_schedule
from .classifier_worker import ClassifierWorker


class SimEgress:
    def observe_ip(self):
        return "203.0.113.7", None


class SimAdapter:
    def __init__(self):
        self.calls = []
        self.effects = set()

    def send(self, **kwargs):
        self.calls.append(kwargs)
        self.effects.add(kwargs["idempotency_key"])


class SimFailingClassifier:
    def classify(self, _text):
        raise TimeoutError("offline simulation classifier failure")


def run(seed=7):
    checks = []
    with tempfile.TemporaryDirectory(prefix="persona-sidecar-") as temp:
        db_path = Path(temp) / "sidecar.sqlite3"
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["database"]["path"] = str(db_path)
        config["state"]["directory"] = str(Path(temp) / "personas")
        config["damage"]["enabled"] = False
        init_db(db_path)

        anchor = config["personas"]["default"]["anchor"]
        state = initial_state(anchor, 0)
        state["base"] = {key: 1.4 for key in anchor}
        state["mood"] = {key: 0.9 for key in anchor}
        after_decay = advance_state(state, anchor, 86400, config["affect"])
        bounded = all(0 <= value <= 1 for group in ("base", "mood") for value in after_decay[group].values())
        moved = all(abs(after_decay["mood"][key] - anchor[key]) < abs(0.9 - anchor[key]) for key in anchor)
        checks.append(("16 bounds and decay", bounded and moved))

        first = apply_label(db_path, config, "alpha", "playful", 1000, arousal=1.0)
        second = apply_label(db_path, config, "alpha", "playful", 1060, arousal=1.0)
        checks.append(("17 15-minute habituation", first["prior_same_label"] == 0 and second["habituation_scale"] == 0.7 and second["deltas"]["play"] < first["deltas"]["play"]))

        queued = enqueue_message(
            db_path, config, "sender", "A simulated outbound reply.",
            "simulated user text", f"simulation-{seed}", now=100,
            rng=random.Random(seed),
        )
        adapter = SimAdapter()
        scheduler = Scheduler(db_path, adapter, SimEgress(), config["delivery"], clock=lambda: 100000)
        sent = scheduler.process_one(now=100000)
        worker = ClassifierWorker(db_path, config, SimFailingClassifier(), clock=lambda: 100001)
        processed = worker.process_one(now=100001)
        conn = connect(db_path)
        try:
            state_exists = state_file_path(config, "sender").exists()
            job = conn.execute(
                "SELECT status FROM classifier_jobs WHERE message_id=?", (queued["message_id"],)
            ).fetchone()
            delivery = conn.execute(
                "SELECT status FROM delivery_queue WHERE message_id=?", (queued["message_id"],)
            ).fetchone()
        finally:
            conn.close()
        checks.append(("18 classifier failure is isolated from send", sent and processed and len(adapter.calls) == 1 and not state_exists and job["status"] == "failed" and delivery["status"] == "sent"))

        apply_label(db_path, config, "persona-a", "affectionate", 5000, arousal=0.8)
        persisted = get_state(db_path, config, "persona-a", 5000)
        isolated = get_state(db_path, config, "persona-b", 5000)
        checks.append(("19 persistent per-persona state", persisted["base"]["elation"] > anchor["elation"] and isolated["base"]["elation"] == anchor["elation"]))

        settings = config["timing"]
        bursts = ["first simulated burst", "second simulated burst"]
        schedule_a = cumulative_schedule(bursts, settings, random.Random(seed), 0)
        schedule_b = cumulative_schedule(bursts, settings, random.Random(seed), 0)
        checks.append(("seeded cumulative timing", schedule_a == schedule_b and schedule_a[1]["due_at"] > schedule_a[0]["due_at"]))

        recovery_queue = enqueue_message(
            db_path, config, "alpha", "recoverable", request_key=f"recovery-{seed}", now=200,
            rng=random.Random(seed),
        )
        first_attempt = claim_due(db_path, now=100000)
        recovery_adapter = SimAdapter()
        recovery_adapter.send(
            persona_id=first_attempt["persona_id"], message_id=first_attempt["message_id"],
            burst_index=first_attempt["burst_index"], text=first_attempt["text"],
            idempotency_key=first_attempt["idempotency_key"], observed_ip="203.0.113.7",
        )
        recover_inflight(db_path, 100001)
        recovery_scheduler = Scheduler(db_path, recovery_adapter, SimEgress(), config["delivery"], clock=lambda: 100002)
        recovered = recovery_scheduler.process_one(now=100002)
        checks.append(("idempotent queue recovery", recovered and len(recovery_adapter.calls) == 2 and len(recovery_adapter.effects) == 1 and recovery_queue["message_id"] == first_attempt["message_id"]))

        conn = connect(db_path)
        try:
            audit = verify_audit(conn)
        finally:
            conn.close()
        checks.append(("MOLT Camp 1 audit chain", audit["valid"] and audit["records"] > 0))

    failed = [name for name, passed in checks if not passed]
    for name, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print(f"Seed: {seed}; checks: {len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description="Seeded offline persona-sidecar simulation")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    raise SystemExit(run(args.seed))


if __name__ == "__main__":
    main()
