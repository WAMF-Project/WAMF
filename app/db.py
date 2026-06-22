from pathlib import Path
import sqlite3

DB_PATH = './data/speciesid.db'
NAMES_DB_PATH = './birdnames.db'

DETECTION_COLUMNS = (
    'id',
    'detection_time',
    'detection_index',
    'score',
    'display_name',
    'category_name',
    'frigate_event',
    'camera_name',
    'wamf_snapshot_path',
    'wamf_clip_path',
)

DETECTION_SELECT = ', '.join(f'detections.{column}' for column in DETECTION_COLUMNS)


def connect_db(db_path=None, row_factory=True):
    target_path = db_path or DB_PATH

    # SQLite creates missing database files, but not missing parent directories.
    if target_path != ':memory:':
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target_path)

    if row_factory:
        conn.row_factory = sqlite3.Row

    return conn


def connect_names_db(names_db_path=None, row_factory=True):
    conn = sqlite3.connect(names_db_path or NAMES_DB_PATH)

    if row_factory:
        conn.row_factory = sqlite3.Row

    return conn


def quote_sqlite_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def attach_names_db(conn, names_db_path=None, alias='birdnames_db'):
    # SQLite cannot bind database names in ATTACH, so quote a trusted local path.
    conn.execute(
        f"ATTACH DATABASE {quote_sqlite_string(names_db_path or NAMES_DB_PATH)} AS {alias}"
    )


def ensure_schema(db_path=None):
    conn = connect_db(db_path, row_factory=False)
    cursor = conn.cursor()

    # Lightweight migrations are intentionally idempotent so old installs can
    # start safely even when a newer route expects a table, column, or index.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS detections (
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            severity TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS species_info (
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS retention_status (
            last_run TEXT,
            rows_scanned INTEGER,
            orphan_count INTEGER,
            missing_count INTEGER
        )
    """)

    existing_columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(detections)").fetchall()
    }

    for column_name, column_sql in {
        'wamf_snapshot_path': 'wamf_snapshot_path TEXT',
        'wamf_clip_path': 'wamf_clip_path TEXT',
    }.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE detections ADD COLUMN {column_sql}")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    for version in (
        '001_initial_tables',
        '002_detection_media_columns',
        '003_query_indexes',
        '004_system_events_timestamp_index',
    ):
        cursor.execute(
            "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
            (version,)
        )

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_detections_detection_time
        ON detections(detection_time)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_detections_display_time
        ON detections(display_name, detection_time)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_detections_frigate_event
        ON detections(frigate_event)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_system_events_type_id
        ON system_events(event_type, id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_system_events_timestamp
        ON system_events(timestamp)
    """)

    conn.commit()
    conn.close()
