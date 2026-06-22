import sqlite3

import yaml

from app.db import connect_db, ensure_schema


def test_ensure_schema_adds_missing_media_columns_and_indexes(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_time TEXT NOT NULL,
            detection_index INTEGER,
            score REAL,
            display_name TEXT,
            category_name TEXT,
            frigate_event TEXT,
            camera_name TEXT
        )
    """)
    conn.commit()
    conn.close()

    ensure_schema(str(db_path))

    conn = sqlite3.connect(db_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(detections)").fetchall()
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(detections)").fetchall()
    }
    system_event_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(system_events)").fetchall()
    }
    migrations = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    conn.close()

    assert "wamf_snapshot_path" in columns
    assert "wamf_clip_path" in columns
    assert "idx_detections_detection_time" in indexes
    assert "idx_detections_display_time" in indexes
    assert "idx_detections_frigate_event" in indexes
    assert "idx_system_events_timestamp" in system_event_indexes
    assert "003_query_indexes" in migrations
    assert "004_system_events_timestamp_index" in migrations


def test_ensure_schema_creates_missing_parent_directory(tmp_path):
    db_path = tmp_path / "nested" / "data" / "speciesid.db"

    ensure_schema(str(db_path))

    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    conn.close()

    assert "detections" in tables
    assert "schema_migrations" in tables


def test_connect_db_resolves_current_config_on_each_call(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    first_db = tmp_path / "first.db"
    second_db = tmp_path / "second.db"
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    config_path.write_text(
        yaml.safe_dump({"storage": {"database_path": str(first_db)}}),
        encoding="utf-8",
    )
    connect_db().close()

    config_path.write_text(
        yaml.safe_dump({"storage": {"database_path": str(second_db)}}),
        encoding="utf-8",
    )
    connect_db().close()

    assert first_db.exists()
    assert second_db.exists()
