from datetime import datetime, timedelta
import sqlite3

import retention
from retention import prune_system_events


def _create_system_events_db(path):
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


def _insert_event(conn, timestamp, message):
    conn.execute(
        """
        INSERT INTO system_events (
            timestamp,
            severity,
            event_type,
            message
        )
        VALUES (?, 'INFO', 'TEST', ?)
        """,
        (timestamp.isoformat(), message),
    )


def test_prune_system_events_removes_old_rows_and_logs_summary(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "events.db"
    _create_system_events_db(db_path)
    monkeypatch.setattr(retention, "DB_PATH", str(db_path))

    log_events = []
    monkeypatch.setattr(
        retention,
        "log_system_event",
        lambda severity, event_type, message: log_events.append(
            (severity, event_type, message)
        ),
    )

    now = datetime.now()
    conn = sqlite3.connect(db_path)
    _insert_event(conn, now - timedelta(days=120), "old-1")
    _insert_event(conn, now - timedelta(days=100), "old-2")
    _insert_event(conn, now - timedelta(days=5), "recent")
    conn.commit()
    conn.close()

    deleted_count = prune_system_events({
        "retention": {
            "enabled": True,
            "system_events_days": 90,
            "system_events_min_rows": 1,
        },
    })

    conn = sqlite3.connect(db_path)
    messages = [
        row[0]
        for row in conn.execute(
            "SELECT message FROM system_events ORDER BY id"
        ).fetchall()
    ]
    conn.close()

    assert deleted_count == 2
    assert messages == ["recent"]
    assert log_events == [(
        "INFO",
        "RETENTION",
        "Pruned 2 system events older than 90 days; kept newest 1 rows",
    )]


def test_prune_system_events_keeps_newest_minimum_rows(monkeypatch, tmp_path):
    db_path = tmp_path / "events.db"
    _create_system_events_db(db_path)
    monkeypatch.setattr(retention, "DB_PATH", str(db_path))
    monkeypatch.setattr(retention, "log_system_event", lambda *args: None)

    now = datetime.now()
    conn = sqlite3.connect(db_path)

    for event_number in range(5):
        _insert_event(
            conn,
            now - timedelta(days=120 - event_number),
            f"old-{event_number}",
        )

    conn.commit()
    conn.close()

    deleted_count = prune_system_events({
        "retention": {
            "enabled": True,
            "system_events_days": 90,
            "system_events_min_rows": 3,
        },
    })

    conn = sqlite3.connect(db_path)
    messages = [
        row[0]
        for row in conn.execute(
            "SELECT message FROM system_events ORDER BY id"
        ).fetchall()
    ]
    conn.close()

    assert deleted_count == 2
    assert messages == ["old-2", "old-3", "old-4"]
