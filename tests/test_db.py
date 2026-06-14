import sqlite3

from app.db import ensure_schema


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
    assert "003_query_indexes" in migrations


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
