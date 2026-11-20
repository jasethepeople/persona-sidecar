import hashlib
import json
import random
import sqlite3
import uuid

from .damage import damage_text, split_bursts
from .database import append_audit, connect, transaction
from .timing import cumulative_schedule


def _seeded_rng(message_id):
    seed = int.from_bytes(hashlib.sha256(message_id.encode("utf-8")).digest()[:8], "big")
    return random.Random(seed)


def enqueue_message(db_path, config, persona_id, text, classify_text=None, request_key=None, now=None, rng=None):
    import time

    now = float(now if now is not None else time.time())
    text = str(text).strip()
    persona_id = str(persona_id).strip()
    if not persona_id or len(persona_id) > 128:
        raise ValueError("persona_id must contain 1 to 128 characters")
    if not text or len(text) > int(config["gateway"]["max_text_chars"]):
        raise ValueError("text is empty or exceeds the configured message limit")
    if classify_text is not None:
        classify_text = str(classify_text).strip()
        if len(classify_text) > int(config["gateway"]["max_text_chars"]):
            raise ValueError("classify_text exceeds the configured message limit")
        if not classify_text:
            classify_text = None
    request_key = (str(request_key or uuid.uuid4().hex)).strip()
    if not request_key or len(request_key) > 200:
        raise ValueError("Idempotency-Key must contain 1 to 200 characters")
    request_hash = hashlib.sha256(json.dumps(
        [persona_id, text, classify_text], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    message_id = uuid.uuid4().hex
    rng = rng or _seeded_rng(request_key)
    parts = damage_text(text, config["damage"], rng)
    bursts = []
    for part in parts:
        bursts.extend(split_bursts(part, config["timing"]["max_burst_chars"]))
    scheduled = cumulative_schedule(bursts, config["timing"], rng, now)
    conn = connect(db_path)
    try:
        with transaction(conn):
            existing = conn.execute(
                "SELECT message_id,persona_id,request_hash FROM messages WHERE request_key=?", (request_key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError("Idempotency-Key was already used with different content")
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM delivery_queue WHERE message_id=?", (existing["message_id"],)
                ).fetchone()["n"]
                return {
                    "message_id": existing["message_id"], "persona_id": existing["persona_id"],
                    "queued_bursts": count, "duplicate": True,
                }
            conn.execute(
                "INSERT INTO messages(message_id,request_key,request_hash,persona_id,classify_text,created_at) VALUES(?,?,?,?,?,?)",
                (message_id, request_key, request_hash, persona_id, classify_text, now),
            )
            for item in scheduled:
                burst_idem = f"{message_id}:{item['burst_index']}"
                conn.execute(
                    "INSERT INTO delivery_queue(message_id,persona_id,burst_index,text,idempotency_key,due_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (message_id, persona_id, item["burst_index"], item["text"], burst_idem, item["due_at"], now),
                )
            append_audit(conn, {
                "type": "delivery.queued", "message_id": message_id,
                "persona_id": persona_id, "bursts": len(scheduled),
                "classify_after_send": bool(classify_text),
            }, now)
        return {"message_id": message_id, "persona_id": persona_id, "queued_bursts": len(scheduled), "duplicate": False}
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute("SELECT message_id,persona_id,request_hash FROM messages WHERE request_key=?", (request_key,)).fetchone()
        if not existing:
            raise
        if existing["request_hash"] != request_hash:
            raise ValueError("Idempotency-Key was already used with different content")
        count = conn.execute("SELECT COUNT(*) AS n FROM delivery_queue WHERE message_id=?", (existing["message_id"],)).fetchone()["n"]
        return {"message_id": existing["message_id"], "persona_id": existing["persona_id"], "queued_bursts": count, "duplicate": True}
    finally:
        conn.close()


def claim_due(db_path, now):
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM delivery_queue WHERE status='pending' AND due_at<=? AND next_attempt_at<=? "
                "ORDER BY due_at,queue_id LIMIT 1", (float(now), float(now))
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE delivery_queue SET status='inflight',attempts=attempts+1 WHERE queue_id=? AND status='pending'",
                (row["queue_id"],),
            )
            result = dict(row)
            result["attempts"] += 1
            return result
    finally:
        conn.close()


def record_attempt(db_path, queue_id, observed_ip, observation_error, now):
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute("SELECT message_id,persona_id,burst_index FROM delivery_queue WHERE queue_id=?", (queue_id,)).fetchone()
            if not row:
                raise KeyError("delivery queue item not found")
            conn.execute("UPDATE delivery_queue SET observed_ip=? WHERE queue_id=?", (observed_ip, queue_id))
            append_audit(conn, {
                "type": "delivery.attempt", "message_id": row["message_id"],
                "persona_id": row["persona_id"], "burst_index": row["burst_index"],
                "observed_ip": observed_ip, "ip_observation_error": observation_error,
            }, now)
    finally:
        conn.close()


def mark_sent(db_path, queue_id, now):
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT message_id,persona_id,burst_index,status FROM delivery_queue WHERE queue_id=?", (queue_id,)
            ).fetchone()
            if not row or row["status"] != "inflight":
                return False
            conn.execute(
                "UPDATE delivery_queue SET status='sent',sent_at=? WHERE queue_id=?",
                (float(now), queue_id),
            )
            append_audit(conn, {
                "type": "delivery.sent", "message_id": row["message_id"],
                "persona_id": row["persona_id"], "burst_index": row["burst_index"],
            }, now)
            outstanding = conn.execute(
                "SELECT COUNT(*) AS n FROM delivery_queue WHERE message_id=? AND status!='sent'",
                (row["message_id"],),
            ).fetchone()["n"]
            if outstanding == 0:
                message = conn.execute(
                    "SELECT classify_text FROM messages WHERE message_id=?", (row["message_id"],)
                ).fetchone()
                if message and message["classify_text"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO classifier_jobs(message_id,persona_id,text,status,created_at) "
                        "VALUES(?,?,?,'waiting',?)",
                        (row["message_id"], row["persona_id"], message["classify_text"], float(now)),
                    )
                    append_audit(conn, {
                        "type": "classifier.queued", "message_id": row["message_id"],
                        "persona_id": row["persona_id"],
                    }, now)
        return True
    finally:
        conn.close()


def mark_failed(db_path, item, error_type, now, max_attempts=8):
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute("SELECT status FROM delivery_queue WHERE queue_id=?", (item["queue_id"],)).fetchone()
            if not row or row["status"] != "inflight":
                return
            terminal = int(item["attempts"]) >= int(max_attempts)
            status = "failed" if terminal else "pending"
            delay = min(300.0, 2.0 ** min(int(item["attempts"]), 8))
            conn.execute(
                "UPDATE delivery_queue SET status=?,next_attempt_at=? WHERE queue_id=?",
                (status, float(now) + (0.0 if terminal else delay), item["queue_id"]),
            )
            append_audit(conn, {
                "type": "delivery.failed" if terminal else "delivery.retry_scheduled",
                "message_id": item["message_id"], "persona_id": item["persona_id"],
                "burst_index": item["burst_index"], "attempt": int(item["attempts"]),
                "error_type": str(error_type)[:80],
            }, now)
    finally:
        conn.close()


def recover_inflight(db_path, now):
    conn = connect(db_path)
    try:
        with transaction(conn):
            count = conn.execute("SELECT COUNT(*) AS n FROM delivery_queue WHERE status='inflight'").fetchone()["n"]
            conn.execute("UPDATE delivery_queue SET status='pending',next_attempt_at=0 WHERE status='inflight'")
            processing = conn.execute("SELECT COUNT(*) AS n FROM classifier_jobs WHERE status='processing'").fetchone()["n"]
            conn.execute("UPDATE classifier_jobs SET status='waiting' WHERE status='processing'")
            if count or processing:
                append_audit(conn, {
                    "type": "recovery.requeued", "delivery_items": count,
                    "classifier_jobs": processing,
                }, now)
            return count
    finally:
        conn.close()


def queue_status(db_path, limit=25):
    conn = connect(db_path)
    try:
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM delivery_queue GROUP BY status"
        )}
        recent = [dict(row) for row in conn.execute(
            "SELECT message_id,persona_id,burst_index,status,attempts,observed_ip,due_at,sent_at "
            "FROM delivery_queue ORDER BY queue_id DESC LIMIT ?", (int(limit),)
        )]
        return {"counts": counts, "recent": recent}
    finally:
        conn.close()
