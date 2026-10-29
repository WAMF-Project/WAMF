"""Compatibility inspection uses only synthetic, explicitly selected SQLite files."""
import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app import db, db_inspection
from app.db_inspection import (
    DatabaseCompatibility as Compatibility,
)
from app.db_inspection import (
    InspectionError,
    MigrationHistoryEntry,
    inspect_database,
)
from tests.test_db_migrations import (
    OLD_TIME,
    add_history,
    current_prebaseline,
    legacy_database,
    snapshot,
)


def create_current(path):
    db.ensure_schema(path)


def create_empty(path):
    sqlite3.connect(path).close()


def create_foreign(path):
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE unrelated (value TEXT)')
        conn.execute("INSERT INTO unrelated VALUES ('preserve')")


def create_newer(path):
    create_current(path)
    add_history(path, ['008_future_schema'])


def create_unknown(path):
    legacy_database(path)
    add_history(path, ['003_unrecognized_change'])


def create_invalid(path):
    create_current(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = '002_detection_media_columns'")


@pytest.mark.parametrize('create,status,error,required,automatic', [
    (create_current, Compatibility.CURRENT, None, False, False),
    (current_prebaseline, Compatibility.UPGRADEABLE, None, True, True),
    (legacy_database, Compatibility.LEGACY, None, True, True),
    (create_newer, Compatibility.NEWER, InspectionError.NEWER_SCHEMA, None, False),
    (create_invalid, Compatibility.INVALID, InspectionError.INVALID_HISTORY, None, False),
    (create_unknown, Compatibility.UNKNOWN, InspectionError.UNKNOWN_MIGRATION, None, False),
    (create_empty, Compatibility.UNKNOWN, InspectionError.EMPTY_DATABASE, None, False),
    (create_foreign, Compatibility.UNKNOWN, InspectionError.NOT_WAMF, None, False),
])
def test_classifications_are_structured_and_leave_database_unchanged(
    tmp_path, create, status, error, required, automatic,
):
    path = tmp_path / 'staged.db'
    create(path)
    before = path.read_bytes(), snapshot(path), path.stat().st_mtime_ns
    result = inspect_database(path, check_integrity=True)
    assert result.compatibility is status
    assert result.error_category is error
    assert result.migration_required is required
    assert result.automatic_upgrade_supported is automatic
    assert result.newer_than_build is (status is Compatibility.NEWER)
    assert result.path == path.resolve()
    assert result.latest_sequence == 7
    assert result.latest_migration_id == db.MIGRATIONS[-1].migration_id
    assert result.integrity_checked
    assert (path.read_bytes(), snapshot(path), path.stat().st_mtime_ns) == before
    assert inspect_database(path, check_integrity=True) == result


def test_current_metadata_contains_complete_authoritative_history(tmp_path):
    path = tmp_path / 'current.db'
    create_current(path)
    result = inspect_database(path)
    assert result.recognized_sequence == 7
    assert result.recognized_migration_id == db.MIGRATIONS[-1].migration_id
    assert result.migration_ids == tuple(migration.migration_id for migration in db.MIGRATIONS)
    assert all(isinstance(entry, MigrationHistoryEntry) and entry.applied_at for entry in result.history)
    assert not result.unversioned
    assert not result.legacy_recognized
    assert not result.integrity_checked
    assert result.sqlite_errorcode is None


def test_exact_pre006_metadata_does_not_stamp_or_change_rows(tmp_path):
    path = tmp_path / 'kingfisher.db'
    current_prebaseline(path)
    before = snapshot(path)
    result = inspect_database(path)
    assert result.compatibility is Compatibility.UPGRADEABLE
    assert result.recognized_sequence == 5
    assert result.recognized_migration_id == db.RETENTION_STATUS_MIGRATION
    assert result.legacy_recognized
    assert not result.unversioned
    assert all(entry.applied_at == OLD_TIME for entry in result.history)
    assert snapshot(path) == before
    db.ensure_schema(path)
    upgraded = inspect_database(path, check_integrity=True)
    assert upgraded.compatibility is Compatibility.CURRENT
    assert upgraded.history[:5] == result.history


@pytest.mark.parametrize('markers', [[], ['003_query_indexes'],
                                    [migration.migration_id for migration in db.MIGRATIONS[:5]]])
def test_legacy_metadata_never_infers_version_from_unreliable_markers(tmp_path, markers):
    path = tmp_path / 'legacy.db'
    legacy_database(path)
    if markers:
        add_history(path, markers)
    result = inspect_database(path)
    assert result.compatibility is Compatibility.LEGACY
    assert result.recognized_sequence is None
    assert result.recognized_migration_id is None
    assert result.unversioned is (not markers)
    assert result.legacy_recognized
    assert result.migration_ids == tuple(sorted(markers))
    db.ensure_schema(path)
    assert inspect_database(path).compatibility is Compatibility.CURRENT


def test_missing_historical_marker_on_current_shape_remains_legacy(tmp_path):
    path = tmp_path / 'gap.db'
    current_prebaseline(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = '002_detection_media_columns'")
    result = inspect_database(path)
    assert result.compatibility is Compatibility.LEGACY
    assert result.recognized_sequence is None
    assert not result.unversioned
    db.ensure_schema(path)
    assert inspect_database(path).compatibility is Compatibility.CURRENT


def test_postbaseline_older_version_is_upgradeable(tmp_path, monkeypatch):
    path = tmp_path / 'version6.db'
    create_current(path)
    def upgrade(conn):
        conn.execute("UPDATE system_events SET message = 'version7'")
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS + (
        db.Migration(8, '008_test_upgrade', upgrade),))
    result = inspect_database(path)
    assert result.compatibility is Compatibility.UPGRADEABLE
    assert result.recognized_sequence == 7
    assert result.latest_sequence == 8
    assert not result.legacy_recognized
    db.ensure_schema(path)
    assert inspect_database(path).compatibility is Compatibility.CURRENT
    assert inspect_database(path).recognized_sequence == 8


@pytest.mark.parametrize('change,error', [
    ('ALTER TABLE detections ADD COLUMN unexpected TEXT', InspectionError.INVALID_SCHEMA),
    ('DROP INDEX idx_system_events_timestamp', InspectionError.INVALID_SCHEMA),
    ("UPDATE schema_migrations SET applied_at = 'malformed'", InspectionError.INVALID_HISTORY),
    ("UPDATE schema_migrations SET version = NULL WHERE version = '001_initial_tables'",
     InspectionError.INVALID_HISTORY),
    ("DELETE FROM schema_migrations WHERE version = '001_initial_tables'", InspectionError.INVALID_HISTORY),
])
def test_invalid_known_databases_are_rejected_by_inspection_and_runner(tmp_path, change, error):
    path = tmp_path / 'invalid.db'
    create_current(path)
    with sqlite3.connect(path) as conn:
        conn.execute(change)
    before = snapshot(path)
    result = inspect_database(path)
    assert result.compatibility is Compatibility.INVALID
    assert result.error_category is error
    assert not result.automatic_upgrade_supported
    with pytest.raises(db.SchemaMigrationError):
        db.ensure_schema(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('create', [create_newer, create_unknown, create_foreign])
def test_unsupported_databases_agree_with_runner_rejection(tmp_path, create):
    path = tmp_path / 'unsupported.db'
    create(path)
    before = snapshot(path)
    assert not inspect_database(path).automatic_upgrade_supported
    with pytest.raises(db.SchemaMigrationError):
        db.ensure_schema(path)
    assert snapshot(path) == before


def test_future_history_is_exposed_without_claiming_recognized_version(tmp_path):
    path = tmp_path / 'future.db'
    create_newer(path)
    result = inspect_database(path)
    assert result.compatibility is Compatibility.NEWER
    assert result.migration_ids[-1] == '008_future_schema'
    assert result.recognized_sequence is None
    assert result.newer_than_build
    assert result.latest_sequence == 7


@pytest.mark.parametrize('contents', [b'not a SQLite database', b'SQLite format 3\x00' + b'broken' * 30])
def test_corrupt_sqlite_is_invalid_and_not_repaired(tmp_path, contents):
    path = tmp_path / 'corrupt.db'
    path.write_bytes(contents)
    result = inspect_database(path, check_integrity=True)
    assert result.compatibility is Compatibility.INVALID
    assert result.error_category is InspectionError.CORRUPT_DATABASE
    assert result.sqlite_errorcode is not None
    assert path.read_bytes() == contents


def test_empty_file_is_not_initialized(tmp_path):
    path = tmp_path / 'empty.db'
    path.touch()
    result = inspect_database(path)
    assert result.compatibility is Compatibility.UNKNOWN
    assert result.error_category is InspectionError.EMPTY_DATABASE
    assert result.unversioned
    assert not result.automatic_upgrade_supported
    assert path.read_bytes() == b''
    # Explicit fresh startup is allowed, while restore preflight rejects empty.
    db.ensure_schema(path)
    assert inspect_database(path).compatibility is Compatibility.CURRENT


def test_missing_path_does_not_create_parents_or_call_mutating_helpers(tmp_path, monkeypatch):
    path = tmp_path / 'missing' / 'staged.db'
    monkeypatch.setattr(db, 'connect_db', lambda *a, **k: pytest.fail('mutating connection helper'))
    monkeypatch.setattr(db, 'ensure_schema', lambda *a, **k: pytest.fail('migration'))
    monkeypatch.setattr(Path, 'mkdir', lambda *a, **k: pytest.fail('directory creation'))
    result = inspect_database(path)
    assert result.compatibility is Compatibility.UNKNOWN
    assert result.error_category is InspectionError.MISSING_FILE
    assert not path.parent.exists()


@pytest.mark.parametrize('create,status', [(create_current, Compatibility.CURRENT),
                                          (legacy_database, Compatibility.LEGACY)])
def test_readonly_file_and_directory_are_inspectable(tmp_path, create, status):
    parent = tmp_path / 'readonly'
    parent.mkdir()
    path = parent / 'staged.db'
    create(path)
    before = path.read_bytes()
    before_files = set(parent.iterdir())
    path.chmod(0o444)
    parent.chmod(0o555)
    try:
        result = inspect_database(path, check_integrity=True)
        assert result.compatibility is status
        assert result.integrity_checked
        assert path.read_bytes() == before
        assert set(parent.iterdir()) == before_files
    finally:
        parent.chmod(0o755)
        path.chmod(0o644)


def test_staged_path_is_explicit_and_safely_uri_quoted(tmp_path, monkeypatch):
    parent = tmp_path / 'restore staging ? # ü'
    parent.mkdir()
    path = parent / 'snapshot #?.sqlite'
    current_prebaseline(path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(tmp_path / 'missing.yml'))
    monkeypatch.setattr(db, 'get_database_path', lambda *a, **k: pytest.fail('config path fallback'))
    result = inspect_database(path.relative_to(tmp_path))
    assert result.compatibility is Compatibility.UPGRADEABLE
    assert result.path == path.resolve()


def test_connection_is_readonly_query_only_and_always_closed(tmp_path, monkeypatch):
    path = tmp_path / 'current.db'
    create_current(path)
    real_connect = sqlite3.connect
    connections, trace, parameters = [], [], []
    def connect(*args, **kwargs):
        parameters.append((args, kwargs))
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(trace.append)
        connections.append(conn)
        return conn
    monkeypatch.setattr(db_inspection.sqlite3, 'connect', connect)
    assert inspect_database(path).compatibility is Compatibility.CURRENT
    args, kwargs = parameters[0]
    assert args[0] == path.as_uri() + '?mode=ro'
    assert kwargs['uri'] is True
    assert trace[:2] == ['PRAGMA query_only = ON', 'BEGIN']
    assert all(sql.startswith(('SELECT', 'PRAGMA', 'BEGIN', 'ROLLBACK')) for sql in trace)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')


def test_locked_file_returns_structured_unknown_error(tmp_path, monkeypatch):
    path = tmp_path / 'locked.db'
    create_current(path)
    monkeypatch.setattr(db_inspection, 'MIGRATION_LOCK_TIMEOUT_MS', 10)
    with sqlite3.connect(path) as blocker:
        blocker.execute('BEGIN EXCLUSIVE')
        result = inspect_database(path)
    assert result.compatibility is Compatibility.UNKNOWN
    assert result.error_category is InspectionError.LOCKED_DATABASE


def test_unreadable_database_returns_no_raw_error_values(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'current.db'
    create_current(path)
    def denied(*args, **kwargs):
        raise PermissionError('PRIVATE value')
    monkeypatch.setattr(db_inspection.sqlite3, 'connect', denied)
    result = inspect_database(path)
    assert result.compatibility is Compatibility.UNKNOWN
    assert result.error_category is InspectionError.UNREADABLE_DATABASE
    assert 'PRIVATE' not in repr(result) + caplog.text


@pytest.mark.parametrize('path', [None, '', ':memory:'])
def test_nonexistent_or_invalid_input_cannot_select_runtime_database(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    result = inspect_database(path)
    assert result.compatibility is Compatibility.UNKNOWN
    assert result.error_category in (InspectionError.INVALID_PATH, InspectionError.MISSING_FILE)


def test_many_inspections_are_byte_identical_and_create_no_files(tmp_path):
    path = tmp_path / 'kingfisher.db'
    current_prebaseline(path)
    before = hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns
    files = set(tmp_path.iterdir())
    results = [inspect_database(path, check_integrity=True) for _ in range(5)]
    assert all(result == results[0] for result in results)
    assert (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns) == before
    assert set(tmp_path.iterdir()) == files


def test_invalid_registry_remains_a_programming_error(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'MIGRATIONS', (replace(db.MIGRATIONS[0], sequence=0),))
    with pytest.raises(db.SchemaMigrationError, match='registry'):
        inspect_database(tmp_path / 'never_created.db')
    assert not (tmp_path / 'never_created.db').exists()


@pytest.mark.parametrize('active_writer', [False, True])
def test_wal_inspection_does_not_create_or_update_sidecars(tmp_path, monkeypatch, active_writer):
    path = tmp_path / 'wal.db'
    create_current(path)
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    if active_writer:
        connection.execute("INSERT INTO system_events (timestamp, severity, event_type, message) VALUES ('now', 'INFO', 'TEST', 'in WAL')")
        connection.commit()
    else:
        connection.close()
    before = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    # Rejection must happen before SQLite opens a file and creates sidecars.
    monkeypatch.setattr(db_inspection.sqlite3, 'connect', lambda *a, **k: pytest.fail('WAL opened'))
    try:
        result = inspect_database(path, check_integrity=True)
        assert result.compatibility is Compatibility.UNKNOWN
        assert result.error_category is InspectionError.UNSUPPORTED_JOURNAL_MODE
        assert not result.automatic_upgrade_supported
        assert not result.integrity_checked
        assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == before
    finally:
        if active_writer:
            connection.close()


def test_integrity_failure_does_not_expose_sqlite_diagnostic_values(tmp_path, monkeypatch):
    path = tmp_path / 'bad-data.db'
    create_current(path)
    before = path.read_bytes()
    assert inspect_database(path).compatibility is Compatibility.CURRENT
    real_connect = sqlite3.connect
    class FailedCheck:
        def fetchall(self):
            return [('PRIVATE SQLite corruption detail',)]
    class Connection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql):
            if sql == 'PRAGMA quick_check':
                return FailedCheck()
            return self.connection.execute(sql)
        def close(self):
            self.connection.close()
    monkeypatch.setattr(db_inspection.sqlite3, 'connect', lambda *a, **k: Connection(
        real_connect(*a, **k)))
    checked = inspect_database(path, check_integrity=True)
    assert checked.compatibility is Compatibility.INVALID
    assert checked.error_category is InspectionError.INTEGRITY_CHECK_FAILED
    assert checked.integrity_checked
    assert checked.migration_required is None
    assert 'PRIVATE' not in repr(checked)
    assert path.read_bytes() == before


def test_malformed_ledger_definition_is_invalid_history(tmp_path):
    path = tmp_path / 'malformed.db'
    legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE schema_migrations (version TEXT, applied_at TEXT)')
        conn.execute("INSERT INTO schema_migrations VALUES ('001_initial_tables', 'PRIVATE malformed')")
    result = inspect_database(path)
    assert result.compatibility is Compatibility.INVALID
    assert result.error_category is InspectionError.INVALID_HISTORY
    assert not result.history
    assert 'PRIVATE' not in repr(result)
    with pytest.raises(db.MigrationHistoryError):
        db.ensure_schema(path)


def test_history_order_is_deterministic_and_invalid_timestamps_are_retained(tmp_path):
    path = tmp_path / 'history.db'
    legacy_database(path)
    markers = [migration.migration_id for migration in reversed(db.MIGRATIONS[:5])]
    add_history(path, markers)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_migrations SET applied_at = 'bad timestamp' WHERE version = '003_query_indexes'")
    result = inspect_database(path)
    assert result.compatibility is Compatibility.INVALID
    assert result.error_category is InspectionError.INVALID_HISTORY
    assert result.migration_ids == tuple(sorted(markers))
    assert result.history[2].applied_at == 'bad timestamp'


def test_symlink_resolves_to_explicit_staged_file(tmp_path):
    path = tmp_path / 'staged.db'
    create_current(path)
    link = tmp_path / 'inspection-link.db'
    link.symlink_to(path)
    result = inspect_database(link)
    assert result.compatibility is Compatibility.CURRENT
    assert result.path == path.resolve()
