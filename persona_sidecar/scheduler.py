import logging
import threading
import time

from .database import init_db
from .queue import claim_due, mark_failed, mark_sent, record_attempt, recover_inflight


class Scheduler:
    def __init__(self, db_path, adapter, egress, settings, clock=time.time):
        self.db_path = db_path
        self.adapter = adapter
        self.egress = egress
        self.settings = settings
        self.clock = clock
        self.log = logging.getLogger("persona_sidecar.scheduler")

    def process_one(self, now=None):
        now = float(now if now is not None else self.clock())
        item = claim_due(self.db_path, now)
        if not item:
            return False
        try:
            try:
                observed_ip, observation_error = self.egress.observe_ip()
            except Exception as exc:
                observed_ip, observation_error = None, type(exc).__name__
            record_attempt(self.db_path, item["queue_id"], observed_ip, observation_error, now)
            self.adapter.send(
                persona_id=item["persona_id"],
                message_id=item["message_id"],
                burst_index=item["burst_index"],
                text=item["text"],
                idempotency_key=item["idempotency_key"],
                observed_ip=observed_ip,
            )
            mark_sent(self.db_path, item["queue_id"], self.clock())
        except Exception as exc:
            self.log.warning("delivery failed message=%s burst=%s error=%s", item["message_id"], item["burst_index"], type(exc).__name__)
            mark_failed(self.db_path, item, type(exc).__name__, self.clock(), int(self.settings.get("max_attempts", 8)))
        return True

    def run(self, stop_event):
        init_db(self.db_path)
        recover_inflight(self.db_path, self.clock())
        interval = max(0.05, float(self.settings.get("poll_seconds", 0.25)))
        while not stop_event.is_set():
            if not self.process_one():
                stop_event.wait(interval)
