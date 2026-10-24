import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
        parsed = datetime.fromisoformat(value.strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).timestamp()
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
    """Rebuild legacy orphan-scan status inside the runner's transaction."""

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


TABLE_SCHEMAS = {
    'detections': """
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
    """,
    'system_events': """
        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            severity TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """,
    'species_info': """
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
    """,
    'retention_status': """
        CREATE TABLE IF NOT EXISTS retention_status (
            last_run TEXT,
            rows_scanned INTEGER,
            orphan_count INTEGER,
            missing_count INTEGER
        )
    """,
    'schema_migrations': """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
}

INDEX_SCHEMAS = {
    'idx_detections_detection_time': """
        CREATE INDEX IF NOT EXISTS idx_detections_detection_time
        ON detections(detection_time)
    """,
    'idx_detections_display_time': """
        CREATE INDEX IF NOT EXISTS idx_detections_display_time
        ON detections(display_name, detection_time)
    """,
    'idx_detections_frigate_event': """
        CREATE INDEX IF NOT EXISTS idx_detections_frigate_event
        ON detections(frigate_event)
    """,
    'idx_system_events_type_id': """
        CREATE INDEX IF NOT EXISTS idx_system_events_type_id
        ON system_events(event_type, id)
    """,
    'idx_system_events_timestamp': """
        CREATE INDEX IF NOT EXISTS idx_system_events_timestamp
        ON system_events(timestamp)
    """,
}

BASELINE_MIGRATION = '006_versioned_schema_baseline'
MIGRATION_LOCK_TIMEOUT_MS = 5000
logger = logging.getLogger(__name__)


class SchemaMigrationError(RuntimeError):
    """The database cannot safely be adopted or upgraded by this build."""


class DatabaseNewerError(SchemaMigrationError):
    """The database contains migrations beyond this build's supported version."""


class MigrationHistoryError(SchemaMigrationError):
    """Migration history is malformed or has impossible predecessors."""


class UnknownMigrationError(MigrationHistoryError):
    """A migration ID is not recognized, without proving a newer sequence."""


@dataclass(frozen=True)
class Migration:
    sequence: int
    migration_id: str
    upgrade: Callable[[sqlite3.Connection], None]


def _columns(conn, table):
    # Table names here come exclusively from the fixed schema registry.
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def _initial_tables(conn):
    for table in ('detections', 'system_events', 'species_info', 'retention_status'):
        sql = TABLE_SCHEMAS[table]
        if table == 'detections':
            sql = _without_columns(sql, ('wamf_snapshot_path', 'wamf_clip_path'))
        elif table == 'species_info':
            sql = _without_columns(sql, ('thumbnail_url',))
        conn.execute(sql)


def _media_columns(conn):
    for name in ('wamf_snapshot_path', 'wamf_clip_path'):
        if name not in _columns(conn, 'detections'):
            conn.execute(f'ALTER TABLE detections ADD COLUMN {name} TEXT')


def _query_indexes(conn):
    for name, sql in INDEX_SCHEMAS.items():
        if name != 'idx_system_events_timestamp':
            conn.execute(sql)


def _timestamp_index(conn):
    conn.execute(INDEX_SCHEMAS['idx_system_events_timestamp'])


def _adopt_baseline(conn):
    if 'thumbnail_url' not in _columns(conn, 'species_info'):
        conn.execute('ALTER TABLE species_info ADD COLUMN thumbnail_url TEXT')
    _validate_schema(conn, legacy=False)


MIGRATIONS = (
    Migration(1, '001_initial_tables', _initial_tables),
    Migration(2, '002_detection_media_columns', _media_columns),
    Migration(3, '003_query_indexes', _query_indexes),
    Migration(4, '004_system_events_timestamp_index', _timestamp_index),
    Migration(5, RETENTION_STATUS_MIGRATION, _migrate_retention_status),
    Migration(6, BASELINE_MIGRATION, _adopt_baseline),
)


def _ordered_migrations():
    if any(not isinstance(migration, Migration) or type(migration.sequence) is not int
           for migration in MIGRATIONS):
        raise SchemaMigrationError('Invalid ordered migration registry')
    migrations = tuple(sorted(MIGRATIONS, key=lambda migration: migration.sequence))
    ids = set()
    for expected, migration in enumerate(migrations, 1):
        if (type(migration.sequence) is not int or migration.sequence != expected
                or not isinstance(migration.migration_id, str)
                or not re.fullmatch(rf'{expected:03d}_[a-z][a-z0-9_]*', migration.migration_id)
                or migration.migration_id in ids or not callable(migration.upgrade)):
            raise SchemaMigrationError('Invalid ordered migration registry')
        ids.add(migration.migration_id)
    if not migrations or BASELINE_MIGRATION not in ids:
        raise SchemaMigrationError('Missing schema baseline in migration registry')
    return migrations


def _without_columns(sql, names):
    for name in names:
        sql = re.sub(rf',\s*{name}\s+TEXT\b', '', sql)
    return sql


def _normalized_sql(sql):
    # Ignore formatting and identifier quotes introduced by SQLite table rename.
    # Preserve literal values: changing a CHECK literal changes its semantics.
    tokens = re.findall(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]"
        r'|[a-zA-Z_][a-zA-Z_0-9]*|[0-9]+|\S', sql,
    )
    normalized = []
    for token in tokens:
        if token.startswith("'"):
            normalized.append(token)
        elif token[0] in ('"', '`', '['):
            identifier = token[1:-1]
            normalized.append(identifier.lower() if re.fullmatch(
                r'[a-zA-Z_][a-zA-Z_0-9]*', identifier,
            ) else token)
        else:
            normalized.append(token.lower())
    tokens = normalized
    if tokens[:5] in (['create', 'table', 'if', 'not', 'exists'],
                      ['create', 'index', 'if', 'not', 'exists']):
        del tokens[2:5]
    if tokens[:2] == ['create', 'table']:
        # ADD COLUMN can put independently introduced media columns in either
        # order. Compare definitions without changing their physical positions.
        start = tokens.index('(')
        groups, group, depth = [], [], 0
        for token in tokens[start + 1:-1]:
            if token == ',' and depth == 0:
                groups.append(tuple(group))
                group = []
                continue
            group.append(token)
            depth += (token == '(') - (token == ')')
        groups.append(tuple(group))
        return tuple(tokens[:start]), tuple(sorted(groups))
    return tuple(tokens)


def _schema_objects(conn):
    return {
        name: (kind, table, sql)
        for kind, name, table, sql in conn.execute(
            'SELECT type, name, tbl_name, sql FROM sqlite_schema'
        )
        if name != 'sqlite_sequence' and not name.startswith('sqlite_autoindex_')
    }


def _validate_schema(conn, *, legacy):
    """Accept only audited table definitions and known additive omissions.

    Pre-baseline installs may contain any nonempty subset of recognized WAMF
    tables (including the historical retention-only maintenance database).
    Optional media/thumbnail columns and query indexes may be absent. All
    present definitions must match; unknown tables, views, triggers, indexes,
    constraints, or columns are never silently rewritten.
    """
    objects = _schema_objects(conn)
    expected_tables = dict(TABLE_SCHEMAS, retention_status=RETENTION_STATUS_SCHEMA)
    if legacy:
        application_tables = set(expected_tables) - {'schema_migrations'}
        if objects and not application_tables.intersection(objects):
            raise SchemaMigrationError('Nonempty database is not a recognized WAMF schema')
    elif set(objects) != set(expected_tables) | set(INDEX_SCHEMAS):
        raise SchemaMigrationError('Database schema does not match the current WAMF baseline')

    for name, (kind, table, sql) in objects.items():
        variants = []
        if name in expected_tables and kind == 'table' and table == name:
            variants = [expected_tables[name]]
            if legacy and name == 'retention_status':
                variants.append(TABLE_SCHEMAS[name])
            elif legacy and name == 'species_info':
                variants.append(_without_columns(TABLE_SCHEMAS[name], ('thumbnail_url',)))
            elif legacy and name == 'detections':
                for missing in (('wamf_snapshot_path',), ('wamf_clip_path',),
                                ('wamf_snapshot_path', 'wamf_clip_path')):
                    variants.append(_without_columns(TABLE_SCHEMAS[name], missing))
        elif name in INDEX_SCHEMAS and kind == 'index':
            variants = [INDEX_SCHEMAS[name]]
        if sql is None or _normalized_sql(sql) not in {
            _normalized_sql(variant) for variant in variants
        }:
            raise SchemaMigrationError(f'Unsupported WAMF schema object: {name}')


def _history_rows(conn):
    """Read the existing ledger without creating it or accepting another shape."""
    objects = _schema_objects(conn)
    if 'schema_migrations' not in objects:
        return []
    kind, _, sql = objects['schema_migrations']
    if (kind != 'table' or sql is None
            or _normalized_sql(sql) != _normalized_sql(TABLE_SCHEMAS['schema_migrations'])):
        raise MigrationHistoryError('Malformed schema_migrations table')
    return list(conn.execute('SELECT version, applied_at FROM schema_migrations'))


def _read_history(conn, migrations, *, rows=None):
    if rows is None:
        rows = _history_rows(conn)
    known = {migration.migration_id for migration in migrations}
    history = set()
    for version, applied_at in rows:
        if not isinstance(version, str) or not re.fullmatch(r'[0-9]{3}_[a-z][a-z0-9_]*', version):
            raise MigrationHistoryError('Malformed migration history ID')
        if version in history:
            raise MigrationHistoryError('Duplicate migration history ID')
        if version not in known:
            if int(version[:3]) > migrations[-1].sequence:
                raise DatabaseNewerError(
                    f'Database newer than this WAMF build: {version}; '
                    f'latest supported is {migrations[-1].migration_id}'
                )
            raise UnknownMigrationError(f'Unknown migration history ID: {version}')
        if not isinstance(applied_at, str) or not applied_at.strip():
            raise MigrationHistoryError('Malformed migration history timestamp')
        try:
            datetime.fromisoformat(applied_at)
        except ValueError as exc:
            raise MigrationHistoryError('Malformed migration history timestamp') from exc
        history.add(version)
    if any(migration.sequence >= 6 and migration.migration_id in history
           for migration in migrations):
        expected = {migration.migration_id for migration in migrations[:len(history)]}
        if history != expected:
            raise MigrationHistoryError('Impossible migration history: missing predecessor')
    return history


def ensure_schema(db_path=None):
    """Atomically adopt/upgrade a WAMF database; callers own startup exclusion.

    Historical 001-005 labels were observational, so before 006 we reconcile
    their actual postconditions even when marked, retaining original timestamps.
    After 006 the ledger must be a contiguous registry prefix and only pending
    migrations execute. No migration function owns transaction boundaries.
    """
    migrations = _ordered_migrations()
    conn = connect_db(db_path, row_factory=False)
    try:
        conn.execute(f'PRAGMA busy_timeout = {MIGRATION_LOCK_TIMEOUT_MS}')
        conn.execute('BEGIN IMMEDIATE')
        history = _read_history(conn, migrations)
        legacy = BASELINE_MIGRATION not in history
        _validate_schema(conn, legacy=legacy)
        conn.execute(TABLE_SCHEMAS['schema_migrations'])
        for migration in migrations:
            if migration.migration_id in history and not legacy:
                continue
            logger.info('Checking database migration %s', migration.migration_id)
            migration.upgrade(conn)
            if migration.migration_id not in history:
                conn.execute(
                    'INSERT INTO schema_migrations (version) VALUES (?)',
                    (migration.migration_id,),
                )
        _validate_schema(conn, legacy=False)
        _read_history(conn, migrations)
        conn.commit()
        logger.info('Database schema ready: %s', migrations[-1].migration_id)
    except BaseException as exc:
        logger.error('Database schema migration failed (%s)', type(exc).__name__)
        conn.rollback()
        raise
    finally:
        conn.close()
