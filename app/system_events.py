from datetime import datetime
import logging
import sqlite3
from app.db import connect_db, DB_PATH as DEFAULT_DB_PATH


DB_PATH = DEFAULT_DB_PATH
logger = logging.getLogger(__name__)

VALID_SEVERITIES = {'DEBUG', 'INFO', 'WARN', 'ERROR'}
DEFAULT_SEVERITY = 'INFO'
MAX_EVENT_TYPE_LENGTH = 40
MAX_MESSAGE_LENGTH = 1000


def normalize_severity(severity):
    normalized = str(severity or DEFAULT_SEVERITY).upper()

    if normalized == 'WARNING':
        normalized = 'WARN'

    if normalized not in VALID_SEVERITIES:
        logger.warning("Unknown system event severity %s; using INFO", severity)
        return DEFAULT_SEVERITY

    return normalized


def normalize_event_type(event_type):
    normalized = str(event_type or 'SYSTEM').upper().strip() or 'SYSTEM'
    return normalized[:MAX_EVENT_TYPE_LENGTH]


def normalize_message(message):
    normalized = str(message or '').strip()

    if len(normalized) <= MAX_MESSAGE_LENGTH:
        return normalized

    return normalized[:MAX_MESSAGE_LENGTH - 3] + '...'


def log_system_event(
    severity,
    event_type,
    message
):
    severity = normalize_severity(severity)
    event_type = normalize_event_type(event_type)
    message = normalize_message(message)
    conn = None

    try:
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
        return True

    except sqlite3.Error as exc:
        logger.warning(
            "Unable to write system event %s/%s: %s",
            severity,
            event_type,
            exc,
        )
        return False

    finally:
        if conn:
            conn.close()
