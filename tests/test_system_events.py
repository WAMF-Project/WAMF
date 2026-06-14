import sqlite3

from app import system_events
from app.system_events import (
    MAX_EVENT_TYPE_LENGTH,
    MAX_MESSAGE_LENGTH,
    log_system_event,
)


def _create_events_table(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            severity TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def test_log_system_event_normalizes_and_truncates(monkeypatch, tmp_path):
    db_path = tmp_path / "events.db"
    _create_events_table(db_path)
    monkeypatch.setattr(system_events, "DB_PATH", str(db_path))

    assert log_system_event(
        "warning",
        "very-long-event-type-name-that-should-be-truncated",
        "x" * (MAX_MESSAGE_LENGTH + 20),
    )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT severity, event_type, message FROM system_events"
    ).fetchone()
    conn.close()

    assert row[0] == "WARN"
    assert len(row[1]) == MAX_EVENT_TYPE_LENGTH
    assert len(row[2]) == MAX_MESSAGE_LENGTH
    assert row[2].endswith("...")


def test_log_system_event_returns_false_on_db_error(monkeypatch, tmp_path, caplog):
    db_path = tmp_path / "missing-table.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(system_events, "DB_PATH", str(db_path))

    assert log_system_event("INFO", "TEST", "message") is False
    assert "Unable to write system event" in caplog.text
