from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from wamf_paths import get_database_path


RETENTION_STATUS_MIGRATION = '005_retention_status_v2'

RETENTION_STATUS_SCHEMA = """
    CREATE TABLE retention_status (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        record_version INTEGER NOT NULL CHECK (record_version = 1),
        last_attempt_started_at TEXT,
        last_attempt_completed_at TEXT,
        last_attempt_duration_ms INTEGER CHECK (
            last_attempt_duration_ms IS NULL OR last_attempt_duration_ms >= 0
        ),
        last_attempt_trigger TEXT,
        last_attempt_outcome TEXT CHECK (
            last_attempt_outcome IS NULL OR
            last_attempt_outcome IN ('success', 'partial', 'failed')
        ),
        last_success_completed_at TEXT,
        expired_media_outcome TEXT CHECK (
            expired_media_outcome IS NULL OR
            expired_media_outcome IN ('success', 'partial', 'failed', 'skipped')
        ),
        orphan_scan_outcome TEXT CHECK (
            orphan_scan_outcome IS NULL OR
            orphan_scan_outcome IN ('success', 'partial', 'failed', 'skipped')
        ),
        system_events_outcome TEXT CHECK (
            system_events_outcome IS NULL OR
            system_events_outcome IN ('success', 'partial', 'failed', 'skipped')
        ),
        expired_media_action TEXT CHECK (
            expired_media_action IS NULL OR
            expired_media_action IN ('delete', 'report_only', 'disabled')
        ),
        orphan_media_action TEXT CHECK (
            orphan_media_action IS NULL OR
            orphan_media_action IN ('delete', 'report_only', 'disabled')
        ),
        rows_scanned INTEGER CHECK (rows_scanned IS NULL OR rows_scanned >= 0),
        expired_snapshot_count INTEGER CHECK (
            expired_snapshot_count IS NULL OR expired_snapshot_count >= 0
        ),
        expired_clip_count INTEGER CHECK (
            expired_clip_count IS NULL OR expired_clip_count >= 0
        ),
        deleted_snapshot_count INTEGER CHECK (
            deleted_snapshot_count IS NULL OR deleted_snapshot_count >= 0
        ),
        deleted_clip_count INTEGER CHECK (
            deleted_clip_count IS NULL OR deleted_clip_count >= 0
        ),
        orphan_count INTEGER CHECK (orphan_count IS NULL OR orphan_count >= 0),
        orphan_deletion_count INTEGER CHECK (
            orphan_deletion_count IS NULL OR orphan_deletion_count >= 0
        ),
        missing_reference_count INTEGER CHECK (
            missing_reference_count IS NULL OR missing_reference_count >= 0
        ),
        system_events_pruned INTEGER CHECK (
            system_events_pruned IS NULL OR system_events_pruned >= 0
        ),
        error_count INTEGER CHECK (error_count IS NULL OR error_count >= 0),
        error_summaries_json TEXT,
        legacy_orphan_scan_at TEXT
    )
"""

RETENTION_STATUS_COLUMNS = {
    'id', 'record_version', 'last_attempt_started_at',
    'last_attempt_completed_at', 'last_attempt_duration_ms',
    'last_attempt_trigger', 'last_attempt_outcome',
    'last_success_completed_at', 'expired_media_outcome',
    'orphan_scan_outcome', 'system_events_outcome',
    'expired_media_action', 'orphan_media_action', 'rows_scanned',
    'expired_snapshot_count', 'expired_clip_count',
    'deleted_snapshot_count', 'deleted_clip_count', 'orphan_count',
    'orphan_deletion_count', 'missing_reference_count',
    'system_events_pruned', 'error_count', 'error_summaries_json',
    'legacy_orphan_scan_at',
}
LEGACY_RETENTION_STATUS_COLUMNS = {
    'last_run', 'rows_scanned', 'orphan_count', 'missing_count'
}


def _legacy_timestamp_sort_key(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _selected_legacy_retention_row(rows):
    """Prefer newest valid ISO timestamp, then use rowid deterministically."""

    valid = [
        (timestamp, row)
        for row in rows
        if (timestamp := _legacy_timestamp_sort_key(row[1])) is not None
    ]
    if valid:
        return max(valid, key=lambda item: (item[0], item[1][0]))[1]
    return max(rows, key=lambda row: row[0]) if rows else None


def _migrate_retention_status(conn):
    """Transactionally rebuild legacy orphan-scan status as A2 state."""

    conn.execute('BEGIN IMMEDIATE')
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ('retention_status',),
        ).fetchone()
        if table is None:
            conn.execute(RETENTION_STATUS_SCHEMA)
        else:
            table_info = conn.execute(
                'PRAGMA table_info(retention_status)'
            ).fetchall()
            columns = {row[1] for row in table_info}
            id_is_primary_key = any(
                row[1] == 'id' and row[5] == 1 for row in table_info
            )
            if columns == RETENTION_STATUS_COLUMNS and id_is_primary_key:
                pass
            elif columns == LEGACY_RETENTION_STATUS_COLUMNS:
                rows = conn.execute(
                    """
                    SELECT rowid, last_run, rows_scanned,
                           orphan_count, missing_count
                    FROM retention_status
                    """
                ).fetchall()
                selected = _selected_legacy_retention_row(rows)
                replacement_schema = RETENTION_STATUS_SCHEMA.replace(
                    'CREATE TABLE retention_status',
                    'CREATE TABLE retention_status_a2',
                    1,
                )
                conn.execute(replacement_schema)
                if selected is not None:
                    conn.execute(
                        """
                        INSERT INTO retention_status_a2 (
                            id, record_version, rows_scanned, orphan_count,
                            missing_reference_count, legacy_orphan_scan_at
                        ) VALUES (1, 1, ?, ?, ?, ?)
                        """,
                        (selected[2], selected[3], selected[4], selected[1]),
                    )
                conn.execute('DROP TABLE retention_status')
                conn.execute(
                    'ALTER TABLE retention_status_a2 RENAME TO retention_status'
                )
            else:
                raise RuntimeError(
                    'unsupported retention_status schema; refusing destructive migration'
                )

        conn.execute(
            'INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)',
            (RETENTION_STATUS_MIGRATION,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

NAMES_DB_PATH = str(Path(__file__).resolve().parent.parent / 'birdnames.db')

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
    target_path = get_database_path() if db_path is None else db_path

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
    try:
        _migrate_retention_status(conn)
    finally:
        conn.close()
