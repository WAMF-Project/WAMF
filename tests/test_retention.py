from datetime import datetime, timedelta
import sqlite3
from types import SimpleNamespace

import pytest

import retention
from app.db import ensure_schema
from app.retention_service import run_retention


def _create_system_events_db(path):
    ensure_schema(path)


def _config(db_path, tmp_path, **retention):
    snapshots = tmp_path / "snapshots"
    clips = tmp_path / "clips"
    snapshots.mkdir()
    clips.mkdir()
    values = {
        "enabled": True,
        "snapshots_days": 90,
        "clips_days": 90,
        "delete_media": False,
        "orphan_scan_enabled": False,
        "delete_orphaned_media": False,
        "system_events_days": 90,
        "system_events_min_rows": 0,
        "species_overrides": {},
    }
    values.update(retention)
    return {
        "config_version": 2,
        "storage": {"database_path": str(db_path)},
        "media": {
            "snapshots_path": str(snapshots),
            "clips_path": str(clips),
        },
        "retention": values,
    }


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
    monkeypatch.setattr("app.retention_service._emit_event", lambda *args: None)

    now = datetime.now()
    conn = sqlite3.connect(db_path)
    _insert_event(conn, now - timedelta(days=120), "old-1")
    _insert_event(conn, now - timedelta(days=100), "old-2")
    _insert_event(conn, now - timedelta(days=5), "recent")
    conn.commit()
    conn.close()

    result = run_retention(
        "test",
        config=_config(
            db_path,
            tmp_path,
            system_events_days=90,
            system_events_min_rows=1,
        ),
        now=now,
    )

    conn = sqlite3.connect(db_path)
    messages = [
        row[0]
        for row in conn.execute(
            "SELECT message FROM system_events ORDER BY id"
        ).fetchall()
    ]
    conn.close()

    assert result.system_events_pruned == 2
    assert messages == ["recent"]


def test_prune_system_events_keeps_newest_minimum_rows(monkeypatch, tmp_path):
    db_path = tmp_path / "events.db"
    _create_system_events_db(db_path)
    monkeypatch.setattr("app.retention_service._emit_event", lambda *args: None)

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

    result = run_retention(
        "test",
        config=_config(
            db_path,
            tmp_path,
            system_events_days=90,
            system_events_min_rows=3,
        ),
        now=now,
    )

    conn = sqlite3.connect(db_path)
    messages = [
        row[0]
        for row in conn.execute(
            "SELECT message FROM system_events ORDER BY id"
        ).fetchall()
    ]
    conn.close()

    assert result.system_events_pruned == 2
    assert messages == ["old-2", "old-3", "old-4"]


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [("success", 0), ("partial", 1), ("failed", 1)],
)
def test_standalone_entry_point_delegates_to_safe_service(
    monkeypatch, outcome, exit_code
):
    calls = []

    def run(trigger):
        calls.append(trigger)
        return SimpleNamespace(outcome=outcome, error_count=0)

    monkeypatch.setattr(retention, "run_retention", run)

    assert retention.main() == exit_code
    assert calls == ["standalone"]
