from .database import connect, verify_audit


def inspect_audit(db_path):
    conn = connect(db_path)
    try:
        return verify_audit(conn)
    finally:
        conn.close()
