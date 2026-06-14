"""
Shared fixtures for whosatmyfeeder tests.

Both app.queries and webui.py keep DB paths as module-level globals
(DBPATH / NAMEDBPATH). We patch those so tests never touch the real on-disk
databases.
"""
import os
import sqlite3
import pytest
import yaml


def _ensure_werkzeug_version_attr():
    """Flask 2.3's test client expects this; Werkzeug 3 removed it."""
    import importlib.metadata
    import werkzeug

    if not hasattr(werkzeug, "__version__"):
        werkzeug.__version__ = importlib.metadata.version("werkzeug")



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_det_db(path: str) -> None:
    """Create a minimal detections database."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_time TEXT NOT NULL,
            detection_index INTEGER,
            score REAL,
            display_name TEXT,
            category_name TEXT,
            frigate_event TEXT,
            camera_name TEXT,
            wamf_snapshot_path TEXT,
            wamf_clip_path TEXT
        )
    """)
    conn.execute("""
        INSERT INTO detections
            (detection_time, detection_index, score, display_name, category_name, frigate_event, camera_name, wamf_snapshot_path, wamf_clip_path)
        VALUES
            ('2024-06-01 08:30:00.000000', 1, 0.92, 'Turdus migratorius', 'bird', 'evt-001', 'birdcam', NULL, NULL),
            ('2024-06-01 09:15:00.000000', 2, 0.85, 'Cyanocitta cristata',  'bird', 'evt-002', 'birdcam', NULL, NULL),
            ('2024-06-01 09:45:00.000000', 3, 0.78, 'Turdus migratorius', 'bird', 'evt-003', 'birdcam', NULL, NULL)
    """)
    conn.execute("""
        CREATE TABLE system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            severity TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE species_info (
            scientific_name TEXT PRIMARY KEY,
            common_name TEXT,
            description TEXT,
            wikipedia_url TEXT,
            ebird_url TEXT,
            inaturalist_url TEXT,
            gbif_url TEXT,
            last_updated TEXT,
            thumbnail_url TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE retention_status (
            last_run TEXT,
            rows_scanned INTEGER,
            orphan_count INTEGER,
            missing_count INTEGER
        )
    """)
    conn.execute("""
        INSERT INTO species_info
            (scientific_name, common_name, description, wikipedia_url, last_updated, thumbnail_url)
        VALUES
            ('Turdus migratorius', 'American Robin', 'A familiar thrush.', 'https://example.com/robin', '2024-06-01 00:00:00', 'https://example.com/robin.jpg'),
            ('Cyanocitta cristata', 'Blue Jay', 'A blue corvid.', 'https://example.com/blue-jay', '2024-06-01 00:00:00', 'https://example.com/blue-jay.jpg')
    """)
    conn.commit()
    conn.close()


def _create_name_db(path: str) -> None:
    """Create a minimal birdnames database."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE birdnames (
            scientific_name TEXT PRIMARY KEY,
            common_name TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT INTO birdnames VALUES
            ('Turdus migratorius', 'American Robin'),
            ('Cyanocitta cristata', 'Blue Jay')
    """)
    conn.commit()
    conn.close()


def _create_config(path: str, frigate_url: str = "http://localhost:5000") -> None:
    """Write a minimal config.yml."""
    cfg = {
        "frigate": {
            "frigate_url": frigate_url,
            "mqtt_server": "localhost",
            "mqtt_auth": False,
            "main_topic": "frigate",
            "camera": ["birdcam"],
            "object": "bird",
        },
        "classification": {
            "model": "model.tflite",
            "threshold": 0.55,
        },
        "webui": {
            "port": 7766,
            "host": "0.0.0.0",
        },
    }
    with open(path, "w") as f:
        yaml.dump(cfg, f)


# ---------------------------------------------------------------------------
# Session-scoped temp directory (faster than per-test)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def tmp_dbs(tmp_path_factory):
    """Create temp det_db, name_db and config once per session."""
    base = tmp_path_factory.mktemp("dbs")
    det_db = str(base / "speciesid.db")
    name_db = str(base / "birdnames.db")
    config_path = str(base / "config.yml")
    _create_det_db(det_db)
    _create_name_db(name_db)
    _create_config(config_path)
    return {"det_db": det_db, "name_db": name_db, "config": config_path}


# ---------------------------------------------------------------------------
# Patch module globals before importing the modules under test
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def patched_queries(tmp_dbs):
    """Import app.queries with DB paths pointing at temp databases."""
    import app.queries as queries
    queries.DBPATH = tmp_dbs["det_db"]
    queries.NAMEDBPATH = tmp_dbs["name_db"]
    return queries


@pytest.fixture(scope="session")
def flask_client(tmp_dbs, patched_queries):
    """
    Return a Flask test client with webui's globals patched to use temp DBs.
    Set WHOSATMYFEEDER_CONFIG before importing webui so load_config() (which
    runs at module level) reads the temp config instead of the missing default.
    """
    import os
    os.environ['WHOSATMYFEEDER_CONFIG'] = tmp_dbs["config"]
    _ensure_werkzeug_version_attr()

    import webui
    webui.load_config()
    webui.DBPATH = tmp_dbs["det_db"]
    webui.NAMEDBPATH = tmp_dbs["name_db"]

    webui.app.config["TESTING"] = True
    webui.app.config["SECRET_KEY"] = "test-secret"
    with webui.app.test_client() as client:
        yield client
