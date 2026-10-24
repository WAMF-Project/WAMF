"""Offline upgrades use synthetic databases; never open the deployment database."""
import sqlite3
from dataclasses import replace

import pytest

from app import db

# Historical DDL kept independent of the runner's initial-table implementation.
LEGACY_TABLES = (
    '''CREATE TABLE detections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, detection_time TEXT NOT NULL,
        detection_index INTEGER, score REAL, display_name TEXT, category_name TEXT,
        frigate_event TEXT, camera_name TEXT)''',
    '''CREATE TABLE system_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        severity TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL)''',
    '''CREATE TABLE species_info (
        scientific_name TEXT PRIMARY KEY, common_name TEXT, description TEXT,
        wikipedia_url TEXT, ebird_url TEXT, inaturalist_url TEXT, gbif_url TEXT,
        last_updated TEXT)''',
    '''CREATE TABLE retention_status (
        last_run TEXT, rows_scanned INTEGER, orphan_count INTEGER, missing_count INTEGER)''',
)
LEDGER = '''CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))'''
OLD_TIME = '2025-01-02 03:04:05'


def legacy_database(path):
    with sqlite3.connect(path) as conn:
        for sql in LEGACY_TABLES:
            conn.execute(sql)
        conn.execute("INSERT INTO detections VALUES (41, '2025-01-01', 3, .9, 'bird', 'bird', 'event', 'camera')")
        conn.execute("INSERT INTO detections (id, detection_time) VALUES (900, 'deleted')")
        conn.execute('DELETE FROM detections WHERE id = 900')
        conn.execute("INSERT INTO species_info VALUES ('bird', 'Bird', 'metadata', 'wiki', 'ebird', 'inat', 'gbif', 'then')")
        conn.execute("INSERT INTO system_events VALUES (23, '2025-01-01', 'INFO', 'TEST', 'preserve')")
        conn.execute('INSERT INTO retention_status VALUES (?, 7, 2, 1)', (OLD_TIME,))


def add_history(path, ids):
    with sqlite3.connect(path) as conn:
        conn.execute(LEDGER.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
        conn.executemany('INSERT INTO schema_migrations VALUES (?, ?)', [(mid, OLD_TIME) for mid in ids])


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return '\n'.join(conn.iterdump())


def history(path):
    with sqlite3.connect(path) as conn:
        return dict(conn.execute('SELECT version, applied_at FROM schema_migrations'))


def current_prebaseline(path):
    legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE detections ADD COLUMN wamf_snapshot_path TEXT')
        conn.execute('ALTER TABLE detections ADD COLUMN wamf_clip_path TEXT')
        conn.execute("UPDATE detections SET wamf_snapshot_path = 'media/wamf/snapshots/old.jpg', wamf_clip_path = '/archive/old.mp4'")
        conn.execute('ALTER TABLE species_info ADD COLUMN thumbnail_url TEXT')
        conn.execute("UPDATE species_info SET thumbnail_url = 'https://example.test/bird.jpg'")
        conn.execute('DROP TABLE retention_status')
        conn.execute(db.RETENTION_STATUS_SCHEMA)
        conn.execute('''INSERT INTO retention_status
            (id, record_version, last_attempt_outcome, rows_scanned,
             last_success_completed_at, error_summaries_json, legacy_orphan_scan_at)
            VALUES (1, 1, 'partial', 12, '2025-01-01', '["preserve"]', 'legacy')''')
        for sql in db.INDEX_SCHEMAS.values():
            conn.execute(sql)
    add_history(path, [migration.migration_id for migration in db.MIGRATIONS[:5]])


def test_fresh_replays_chain_with_exact_schema_and_constraints(tmp_path):
    path = tmp_path / 'fresh.db'
    db.ensure_schema(path)
    assert list(history(path)) == [migration.migration_id for migration in db.MIGRATIONS]
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone() == (0,)
        assert conn.execute('PRAGMA journal_mode').fetchone() == ('delete',)
        assert conn.execute('SELECT COUNT(*) FROM retention_status').fetchone() == (0,)
        assert len(conn.execute('PRAGMA table_info(retention_status)').fetchall()) == 25
        assert len(conn.execute("SELECT name FROM sqlite_schema WHERE type = 'index' AND sql IS NOT NULL").fetchall()) == 5
        for sql in (
            'INSERT INTO retention_status (id, record_version) VALUES (2, 1)',
            'INSERT INTO retention_status (id, record_version) VALUES (1, 2)',
            'INSERT INTO retention_status (id, record_version, rows_scanned) VALUES (1, 1, -1)',
            "INSERT INTO retention_status (id, record_version, last_attempt_outcome) VALUES (1, 1, 'unknown')",
            "INSERT INTO detections (id) VALUES (1)",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_current_kingfisher_preserves_every_application_row_and_sequence(tmp_path):
    path = tmp_path / 'current.db'
    current_prebaseline(path)
    before = snapshot(path)
    old_history = history(path)
    db.ensure_schema(path)
    after = snapshot(path)
    # Only the new ledger row differs from an exact pre-006 database.
    assert '\n'.join(line for line in after.splitlines() if db.BASELINE_MIGRATION not in line) == before
    assert {key: history(path)[key] for key in old_history} == old_history
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO detections (detection_time) VALUES ('next')")
        assert conn.execute('SELECT MAX(id) FROM detections').fetchone() == (901,)
    baseline = snapshot(path)
    db.ensure_schema(path)
    assert snapshot(path) == baseline


@pytest.mark.parametrize('markers', [[], ['003_query_indexes'], ['005_retention_status_v2'],
                                    [m.migration_id for m in db.MIGRATIONS[:5]]])
def test_legacy_omissions_repaired_even_when_marked_and_history_preserved(tmp_path, markers):
    path = tmp_path / 'old.db'
    legacy_database(path)
    if markers:
        add_history(path, markers)
    db.ensure_schema(path)
    assert len(history(path)) == 6
    assert all(history(path)[marker] == OLD_TIME for marker in markers)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM detections').fetchone() == (
            41, '2025-01-01', 3, .9, 'bird', 'bird', 'event', 'camera', None, None)
        assert conn.execute('SELECT * FROM species_info').fetchone() == (
            'bird', 'Bird', 'metadata', 'wiki', 'ebird', 'inat', 'gbif', 'then', None)
        assert conn.execute('SELECT * FROM system_events').fetchone() == (
            23, '2025-01-01', 'INFO', 'TEST', 'preserve')
        assert conn.execute('SELECT rows_scanned, orphan_count, missing_reference_count, legacy_orphan_scan_at FROM retention_status').fetchone() == (7, 2, 1, OLD_TIME)


@pytest.mark.parametrize('column', ['wamf_snapshot_path', 'wamf_clip_path'])
def test_single_existing_media_column_preserved(tmp_path, column):
    path = tmp_path / 'partial.db'
    legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute(f'ALTER TABLE detections ADD COLUMN {column} TEXT')
        conn.execute(f"UPDATE detections SET {column} = 'preserve'")
    db.ensure_schema(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(f'SELECT {column} FROM detections').fetchone() == ('preserve',)


def test_ordered_registry_and_pending_prefix(tmp_path, monkeypatch):
    path = tmp_path / 'ordered.db'
    calls = []
    wrapped = []
    for migration in db.MIGRATIONS:
        def upgrade(conn, migration=migration):
            assert conn.in_transaction
            calls.append(migration.sequence)
            migration.upgrade(conn)
        wrapped.append(replace(migration, upgrade=upgrade))
    # Registry order is explicit, independent of declaration/iteration order.
    monkeypatch.setattr(db, 'MIGRATIONS', tuple(reversed(wrapped)))
    db.ensure_schema(path)
    assert calls == [1, 2, 3, 4, 5, 6]
    calls.clear()
    extra = db.Migration(7, '007_test_upgrade', lambda conn: (
        calls.append(7), conn.execute("UPDATE system_events SET message = 'upgraded'")))
    monkeypatch.setattr(db, 'MIGRATIONS', tuple(wrapped) + (extra,))
    db.ensure_schema(path)
    assert calls == [7]
    db.ensure_schema(path)
    assert calls == [7]


@pytest.mark.parametrize('fresh', [False, True])
def test_failure_rolls_back_entire_batch_including_ledger(tmp_path, monkeypatch, fresh):
    path = tmp_path / 'rollback.db'
    if not fresh:
        legacy_database(path)
        add_history(path, ['001_initial_tables'])
    else:
        sqlite3.connect(path).close()
    before = snapshot(path)
    def fail(conn):
        db._adopt_baseline(conn)
        conn.execute("INSERT INTO system_events (timestamp, severity, event_type, message) VALUES ('now', 'INFO', 'TEST', 'rollback')")
        raise RuntimeError('injected failure after baseline validation')
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:5] + (replace(db.MIGRATIONS[5], upgrade=fail),))
    with pytest.raises(RuntimeError, match='injected'):
        db.ensure_schema(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('version,error,match', [
    ('007_future_schema', db.DatabaseNewerError, 'newer than this WAMF build'),
    ('999_future_schema', db.DatabaseNewerError, 'newer than this WAMF build'),
    ('003_unknown_schema', db.SchemaMigrationError, 'Unknown'),
    ('bad', db.SchemaMigrationError, 'Malformed'),
    (None, db.SchemaMigrationError, 'Malformed'),
])
def test_unknown_and_malformed_ids_rejected_without_changes(tmp_path, version, error, match):
    path = tmp_path / 'history.db'
    legacy_database(path)
    add_history(path, [version])
    before = snapshot(path)
    with pytest.raises(error, match=match):
        db.ensure_schema(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('timestamp', ['', 'not-a-date', '2025-99-99'])
def test_malformed_timestamp_rejected(tmp_path, timestamp):
    path = tmp_path / 'history.db'
    legacy_database(path)
    add_history(path, ['001_initial_tables'])
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE schema_migrations SET applied_at = ?', (timestamp,))
    before = snapshot(path)
    with pytest.raises(db.SchemaMigrationError, match='timestamp'):
        db.ensure_schema(path)
    assert snapshot(path) == before


def test_postbaseline_history_gap_rejected(tmp_path):
    path = tmp_path / 'gap.db'
    db.ensure_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = '002_detection_media_columns'")
    before = snapshot(path)
    with pytest.raises(db.SchemaMigrationError, match='missing predecessor'):
        db.ensure_schema(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('ddl', [
    'CREATE TABLE unrelated (value TEXT)',
    'CREATE TABLE detections (id INTEGER PRIMARY KEY)',
    LEGACY_TABLES[0].replace('TEXT NOT NULL', 'TEXT'),
    LEGACY_TABLES[0].replace('AUTOINCREMENT', ''),
    LEDGER,
    'CREATE TABLE schema_migrations (version TEXT, applied_at TEXT)',
])
def test_arbitrary_or_incompatible_database_rejected(tmp_path, ddl):
    path = tmp_path / 'other.db'
    with sqlite3.connect(path) as conn:
        conn.execute(ddl)
    before = snapshot(path)
    with pytest.raises(db.SchemaMigrationError):
        db.ensure_schema(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('change', [
    'CREATE INDEX idx_detections_detection_time ON detections(score)',
    'CREATE INDEX unexpected ON detections(score)',
    'CREATE VIEW unexpected AS SELECT * FROM detections',
    'CREATE TRIGGER unexpected AFTER INSERT ON detections BEGIN SELECT 1; END',
    'ALTER TABLE species_info ADD COLUMN unexpected TEXT',
])
def test_unknown_schema_objects_and_wrong_index_definitions_rejected(tmp_path, change):
    path = tmp_path / 'drift.db'
    legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute(change)
    before = snapshot(path)
    with pytest.raises(db.SchemaMigrationError, match='Unsupported'):
        db.ensure_schema(path)
    assert snapshot(path) == before


def test_native_retention_missing_constraints_rejected(tmp_path):
    path = tmp_path / 'constraints.db'
    legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE retention_status')
        conn.execute(db.RETENTION_STATUS_SCHEMA.replace('CHECK (id = 1)', ''))
    before = snapshot(path)
    with pytest.raises(db.SchemaMigrationError, match='retention_status'):
        db.ensure_schema(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('registry', [
    db.MIGRATIONS + (db.MIGRATIONS[0],),
    (replace(db.MIGRATIONS[0], migration_id='bad'),) + db.MIGRATIONS[1:],
    db.MIGRATIONS[1:],
])
def test_invalid_registry_rejected_before_open(tmp_path, monkeypatch, registry):
    path = tmp_path / 'never-created.db'
    monkeypatch.setattr(db, 'MIGRATIONS', registry)
    with pytest.raises(db.SchemaMigrationError, match='registry'):
        db.ensure_schema(path)
    assert not path.exists()


def test_lock_wait_is_bounded_and_connection_closed(tmp_path, monkeypatch):
    path = tmp_path / 'locked.db'
    db.ensure_schema(path)
    before = snapshot(path)
    monkeypatch.setattr(db, 'MIGRATION_LOCK_TIMEOUT_MS', 10)
    with sqlite3.connect(path) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            db.ensure_schema(path)
    assert snapshot(path) == before
    db.ensure_schema(path)


def test_baseline_validates_before_recording_006(tmp_path, monkeypatch):
    path = tmp_path / 'invalid-baseline.db'
    legacy_database(path)
    before = snapshot(path)
    trace = []
    real_connect = db.connect_db
    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(trace.append)
        return conn
    def invalid_baseline(conn):
        conn.execute('ALTER TABLE species_info ADD COLUMN unexpected TEXT')
        db._adopt_baseline(conn)
    monkeypatch.setattr(db, 'connect_db', traced_connect)
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:5] + (
        replace(db.MIGRATIONS[5], upgrade=invalid_baseline),))
    with pytest.raises(db.SchemaMigrationError, match='species_info'):
        db.ensure_schema(path)
    assert not any('INSERT INTO schema_migrations' in sql and db.BASELINE_MIGRATION in sql
                   for sql in trace)
    assert snapshot(path) == before


def test_commit_failure_rolls_back_and_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / 'commit-failure.db'
    legacy_database(path)
    before = snapshot(path)
    real_connect = db.connect_db
    connections = []
    class FailCommit:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def commit(self):
            raise sqlite3.OperationalError('controlled commit failure')
    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return FailCommit(connection)
    monkeypatch.setattr(db, 'connect_db', connect)
    with pytest.raises(sqlite3.OperationalError, match='commit failure'):
        db.ensure_schema(path)
    assert snapshot(path) == before
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')


def test_history_and_schema_are_read_only_after_write_lock(tmp_path, monkeypatch):
    path = tmp_path / 'trace.db'
    real_connect = db.connect_db
    trace = []
    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(trace.append)
        return conn
    monkeypatch.setattr(db, 'connect_db', connect)
    db.ensure_schema(path)
    assert trace[0] == f'PRAGMA busy_timeout = {db.MIGRATION_LOCK_TIMEOUT_MS}'
    assert trace[1] == 'BEGIN IMMEDIATE'
    assert sum(sql == 'COMMIT' for sql in trace) == 1
    assert trace[-1] == 'COMMIT'


def test_duplicate_history_rows_rejected(tmp_path, monkeypatch):
    # A healthy ledger's PK prevents duplicates. Also reject them if a damaged
    # database/connection reports duplicates despite the declared definition.
    path = tmp_path / 'duplicates.db'
    current_prebaseline(path)
    before = snapshot(path)
    real_connect = db.connect_db
    class DuplicateHistory:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def execute(self, sql, parameters=()):
            result = self.connection.execute(sql, parameters)
            if sql == 'SELECT version, applied_at FROM schema_migrations':
                rows = result.fetchall()
                return rows + rows[:1]
            return result
    monkeypatch.setattr(db, 'connect_db', lambda *args, **kwargs: DuplicateHistory(
        real_connect(*args, **kwargs)))
    with pytest.raises(db.SchemaMigrationError, match='Duplicate'):
        db.ensure_schema(path)
    assert snapshot(path) == before


def test_current_database_missing_only_thumbnail_is_reconciled(tmp_path):
    path = tmp_path / 'thumbnail.db'
    current_prebaseline(path)
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE species_info DROP COLUMN thumbnail_url')
        retention_before = conn.execute('SELECT * FROM retention_status').fetchall()
    original_history = history(path)
    db.ensure_schema(path)
    assert all(history(path)[key] == value for key, value in original_history.items())
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT thumbnail_url FROM species_info').fetchone() == (None,)
        assert conn.execute('SELECT description FROM species_info').fetchone() == ('metadata',)
        assert conn.execute('SELECT * FROM retention_status').fetchall() == retention_before


def test_sequential_pending_upgrades_rollback_to_existing_baseline(tmp_path, monkeypatch):
    path = tmp_path / 'pending.db'
    current_prebaseline(path)
    db.ensure_schema(path)
    before = snapshot(path)
    calls = []
    def seventh(conn):
        calls.append(7)
        conn.execute("UPDATE system_events SET message = 'seventh'")
    def eighth(conn):
        calls.append(8)
        assert conn.execute('SELECT message FROM system_events').fetchone() == ('seventh',)
        conn.execute("UPDATE species_info SET description = 'eighth'")
        raise RuntimeError('eighth failed')
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS + (
        db.Migration(7, '007_test_upgrade', seventh),
        db.Migration(8, '008_test_upgrade', eighth),
    ))
    with pytest.raises(RuntimeError, match='eighth failed'):
        db.ensure_schema(path)
    assert calls == [7, 8]
    assert snapshot(path) == before
    def successful_eighth(conn):
        calls.append(8)
        conn.execute("UPDATE species_info SET description = 'eighth'")
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:7] + (
        replace(db.MIGRATIONS[7], upgrade=successful_eighth),))
    db.ensure_schema(path)
    assert calls == [7, 8, 7, 8]
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT message FROM system_events').fetchone() == ('seventh',)
        assert conn.execute('SELECT description FROM species_info').fetchone() == ('eighth',)


def test_competing_initializer_rechecks_history_after_acquiring_lock(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    path = tmp_path / 'competing.db'
    reached_baseline, release, second_attempt = Event(), Event(), Event()
    guard = Lock()
    starts = []
    baseline_calls = []
    real_connect = db.connect_db
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            with guard:
                starts.append(sql)
                if len(starts) == 2:
                    second_attempt.set()
    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(trace)
        return conn
    def baseline(conn):
        baseline_calls.append(6)
        reached_baseline.set()
        assert release.wait(5)
        db._adopt_baseline(conn)
    monkeypatch.setattr(db, 'connect_db', connect)
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:5] + (
        replace(db.MIGRATIONS[5], upgrade=baseline),))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(db.ensure_schema, path)
        try:
            assert reached_baseline.wait(5)
            second = executor.submit(db.ensure_schema, path)
            assert second_attempt.wait(5)
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert baseline_calls == [6]
    assert len(history(path)) == 6


def test_quoted_column_cannot_masquerade_as_a_column_definition(tmp_path):
    path = tmp_path / 'quoted.db'
    # Token normalization must preserve whitespace inside quoted identifiers.
    sql = LEGACY_TABLES[2].replace('description TEXT', '"description TEXT"')
    with sqlite3.connect(path) as conn:
        conn.execute(sql)
    before = snapshot(path)
    with pytest.raises(db.SchemaMigrationError, match='species_info'):
        db.ensure_schema(path)
    assert snapshot(path) == before
