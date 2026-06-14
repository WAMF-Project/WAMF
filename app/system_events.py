from datetime import datetime
from app.db import connect_db, DB_PATH as DEFAULT_DB_PATH


DB_PATH = DEFAULT_DB_PATH


def log_system_event(
    severity,
    event_type,
    message
):

    conn = connect_db(DB_PATH, row_factory=False)

    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO system_events (
            timestamp,
            severity,
            event_type,
            message
        )
        VALUES (?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        severity,
        event_type,
        message
    ))

    conn.commit()

    conn.close()