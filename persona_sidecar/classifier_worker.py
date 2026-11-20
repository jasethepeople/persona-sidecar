import logging
import time

from .affect import LABEL_DELTAS, apply_label_in_transaction
from .database import append_audit, connect, transaction

log = logging.getLogger(__name__)


def _claim_job(db_path):
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT job_id,message_id,persona_id,text FROM classifier_jobs "
                "WHERE status='waiting' ORDER BY job_id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE classifier_jobs SET status='processing' WHERE job_id=?", (row["job_id"],))
            return dict(row)
    finally:
        conn.close()


class ClassifierWorker:
    def __init__(self, db_path, config, classifier, clock=time.time):
        self.db_path = db_path
        self.config = config
        self.classifier = classifier
        self.clock = clock

    def recover_processing(self):
        now = float(self.clock())
        conn = connect(self.db_path)
        try:
            with transaction(conn):
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM classifier_jobs WHERE status='processing'"
                ).fetchone()["n"]
                if count:
                    conn.execute("UPDATE classifier_jobs SET status='waiting' WHERE status='processing'")
                    append_audit(conn, {"type": "classifier.recovered", "jobs": count}, now)
                return count
        finally:
            conn.close()

    def process_one(self, now=None):
        job = _claim_job(self.db_path)
        if not job:
            return False
        now = float(self.clock() if now is None else now)
        try:
            label, arousal = self.classifier.classify(job["text"])
            if label not in LABEL_DELTAS:
                raise ValueError("classifier returned an unknown label")
            arousal = float(arousal)
            if not 0 <= arousal <= 1:
                raise ValueError("classifier returned arousal outside [0, 1]")
        except Exception as exc:
            log.warning("classifier job %s failed: %s", job["job_id"], type(exc).__name__)
            conn = connect(self.db_path)
            try:
                with transaction(conn):
                    conn.execute(
                        "UPDATE classifier_jobs SET status='failed',finished_at=?,error_type=? WHERE job_id=?",
                        (now, type(exc).__name__, job["job_id"]),
                    )
                    append_audit(conn, {
                        "type": "classifier.failed", "message_id": job["message_id"],
                        "persona_id": job["persona_id"], "error_type": type(exc).__name__,
                    }, now)
            finally:
                conn.close()
            return True
        conn = connect(self.db_path)
        try:
            with transaction(conn):
                applied = apply_label_in_transaction(
                    conn, self.config, job["persona_id"], label, now, arousal
                )
                if applied is None:
                    raise ValueError("classifier result was not applied")
                conn.execute(
                    "UPDATE classifier_jobs SET status='applied',finished_at=?,error_type=NULL WHERE job_id=?",
                    (now, job["job_id"]),
                )
                append_audit(conn, {
                    "type": "classifier.applied", "message_id": job["message_id"],
                    "persona_id": job["persona_id"], "label": label,
                }, now)
        finally:
            conn.close()
        return True

    def run(self, stop_event):
        self.recover_processing()
        interval = max(0.05, float(self.config["classifier"].get("poll_seconds", 0.25)))
        while not stop_event.is_set():
            if not self.process_one():
                stop_event.wait(interval)
