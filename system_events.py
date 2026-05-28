from datetime import datetime
import sqlite3


DB_PATH = "data/speciesid.db"


def log_system_event(
    severity,
    event_type,
    message
):

    conn = sqlite3.connect(DB_PATH)

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