import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

GENESIS_HASH = "0" * 64


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(db_path):
    path = Path(db_path).expanduser().resolve()
    created_parent = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created_parent:
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                request_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL DEFAULT '',
                persona_id TEXT NOT NULL,
                classify_text TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_queue (
                queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL REFERENCES messages(message_id),
                persona_id TEXT NOT NULL,
                burst_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                due_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','inflight','sent','failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                observed_ip TEXT,
                created_at REAL NOT NULL,
                sent_at REAL,
                UNIQUE(message_id, burst_index)
            );
            CREATE INDEX IF NOT EXISTS delivery_due_idx ON delivery_queue(status,due_at,next_attempt_at);
            CREATE TABLE IF NOT EXISTS classifier_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id),
                persona_id TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting' CHECK(status IN ('waiting','processing','applied','failed')),
                created_at REAL NOT NULL,
                finished_at REAL,
                error_type TEXT
            );
            CREATE INDEX IF NOT EXISTS classifier_wait_idx ON classifier_jobs(status,job_id);
            CREATE TABLE IF NOT EXISTS rate_buckets (
                token_hash TEXT PRIMARY KEY,
                window_start INTEGER NOT NULL,
                request_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                row_hash TEXT NOT NULL UNIQUE
            );
            CREATE TRIGGER IF NOT EXISTS audit_no_update
            BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete
            BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit log is append-only'); END;
            """
        )
        message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "request_hash" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        conn.close()
    return str(path)


def append_audit(conn, event, timestamp):
    prior = conn.execute("SELECT seq,row_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    sequence = int(prior["seq"]) + 1 if prior else 1
    previous_hash = prior["row_hash"] if prior else GENESIS_HASH
    event_json = _canonical(event)
    record = {
        "seq": sequence,
        "timestamp": float(timestamp),
        "event_json": event_json,
        "prev_hash": previous_hash,
    }
    row_hash = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO audit_log(seq,timestamp,event_json,prev_hash,row_hash) VALUES(?,?,?,?,?)",
        (sequence, float(timestamp), event_json, previous_hash, row_hash),
    )
    return row_hash


def verify_audit(conn):
    expected_previous = GENESIS_HASH
    count = 0
    for row in conn.execute("SELECT seq,timestamp,event_json,prev_hash,row_hash FROM audit_log ORDER BY seq"):
        count += 1
        if row["seq"] != count:
            return {"valid": False, "records": count - 1, "broken_at": row["seq"], "reason": "sequence_gap"}
        if row["prev_hash"] != expected_previous:
            return {"valid": False, "records": count - 1, "broken_at": row["seq"], "reason": "previous_hash_mismatch"}
        try:
            json.loads(row["event_json"])
        except json.JSONDecodeError:
            return {"valid": False, "records": count - 1, "broken_at": row["seq"], "reason": "invalid_event_json"}
        record = {
            "seq": row["seq"], "timestamp": float(row["timestamp"]),
            "event_json": row["event_json"], "prev_hash": row["prev_hash"],
        }
        calculated = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
        if calculated != row["row_hash"]:
            return {"valid": False, "records": count - 1, "broken_at": row["seq"], "reason": "row_hash_mismatch"}
        expected_previous = row["row_hash"]
    return {"valid": True, "records": count, "head": expected_previous}
