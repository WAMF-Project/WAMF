"""Online backup execution uses only isolated, synthetic databases."""

import errno
import hashlib
import io
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import wamf_paths
from app import database_backup as package
from app import database_backup_service as service
from app import database_backup_storage as storage
from app import db
from app.db_inspection import (
    DatabaseCompatibility,
    inspect_database,
    sqlite_read_checkpoint,
)
from tests.test_db_migrations import add_history, legacy_database


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(wamf_paths, 'REPO_ROOT', tmp_path)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'synthetic.db'
    db.ensure_schema(path)
    return path


def create(source, **kwargs):
    return service.create_backup(source, wamf_paths.get_database_backup_root(), **kwargs)


def published():
    root = wamf_paths.get_database_backup_root()
    return list(root.glob('*.zip')) if root.exists() else []


def assert_clean_failure(result, code):
    assert not result.success and result.code is code
    assert result.phase in (service.BackupPhase.FAILED, service.BackupPhase.CANCELLED)
    assert result.backup_id is None
    assert published() == []
    staging = wamf_paths.get_database_backup_staging_root()
    assert not staging.exists() or list(staging.iterdir()) == []


def test_create_verify_enumerate_download_nonmutating(source, monkeypatch):
    before = source.read_bytes(), source.stat().st_mtime_ns
    snapshots = []

    def forbidden(*args, **kwargs):
        pytest.fail('Backup must not load configuration or migrate')

    monkeypatch.setattr(db, 'ensure_schema', forbidden)
    monkeypatch.setattr(wamf_paths, 'get_database_path', forbidden)
    result = create(source, progress=snapshots.append)
    assert result.success and result.code is service.BackupCode.COMPLETE
    assert result.phase is service.BackupPhase.COMPLETE
    assert len(result.operation_id) == 32
    assert result.started_at <= result.completed_at
    assert result.manifest.source_database == source.name
    assert result.manifest.history == inspect_database(source).history
    assert result.facts.database_sha256 == result.manifest.database_sha256
    assert {snapshot.operation_id for snapshot in snapshots} == {result.operation_id}
    assert {snapshot.phase for snapshot in snapshots} >= {
        service.BackupPhase.PREFLIGHT, service.BackupPhase.BACKING_UP,
        service.BackupPhase.VALIDATING, service.BackupPhase.PACKAGING,
        service.BackupPhase.PUBLISHING, service.BackupPhase.COMPLETE,
    }
    assert any(snapshot.percent == 100 for snapshot in snapshots)
    with service.resolve_backup_download(result.backup_id, wamf_paths.get_database_backup_root()) as download:
        payload = download.file.read()
        assert download.validation.valid
        assert download.package_sha256 == result.package_sha256 == hashlib.sha256(payload).hexdigest()
        assert download.size == result.final_size == len(payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert set(archive.namelist()) == {'database.db', 'manifest.json'}
            assert all(member.compress_type == zipfile.ZIP_STORED for member in archive.infolist())
    assert download.file.closed
    entries = service.list_backups(wamf_paths.get_database_backup_root())
    assert len(entries) == 1 and entries[0].backup_id == result.backup_id
    assert entries[0].validation.valid and entries[0].compatibility is DatabaseCompatibility.CURRENT
    assert entries[0].timestamp == result.manifest.completed_at
    assert entries[0].checksum == result.facts.database_sha256
    assert entries[0].package_sha256 == result.package_sha256
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert list(wamf_paths.get_database_backup_staging_root().iterdir()) == []
    assert stat.S_IMODE(published()[0].stat().st_mode) == 0o600


def test_backup_uses_dedicated_readonly_main_connections(source, monkeypatch):
    original = service._copy_database

    def copy(src, dst, *args):
        assert src is not dst
        assert src.execute('PRAGMA query_only').fetchone() == (1,)
        assert src.execute('PRAGMA database_list').fetchall() == [(0, 'main', str(source))]
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            src.execute("INSERT INTO system_events (timestamp, severity, event_type, message) VALUES ('t','i','t','x')")
        original(src, dst, *args)

    monkeypatch.setattr(service, '_copy_database', copy)
    assert create(source).success


@pytest.mark.parametrize('kind,code', [
    ('missing', service.BackupCode.SOURCE_MISSING),
    ('corrupt', service.BackupCode.SOURCE_UNHEALTHY),
    ('newer', service.BackupCode.SOURCE_NOT_CURRENT),
    ('legacy', service.BackupCode.SOURCE_NOT_CURRENT),
    ('unknown', service.BackupCode.SOURCE_NOT_CURRENT),
    ('invalid', service.BackupCode.SOURCE_NOT_CURRENT),
    ('wal', service.BackupCode.WAL_UNSUPPORTED),
    ('symlink', service.BackupCode.SOURCE_UNSAFE),
    ('symlink_parent', service.BackupCode.SOURCE_UNSAFE),
    ('hardlink', service.BackupCode.SOURCE_UNSAFE),
    ('fifo', service.BackupCode.SOURCE_UNSAFE),
    ('directory', service.BackupCode.SOURCE_UNSAFE),
])
def test_source_preflight_refusals(source, tmp_path, kind, code):
    if kind == 'missing':
        source.unlink()
    elif kind == 'corrupt':
        source.write_bytes(b'not sqlite')
    elif kind == 'newer':
        add_history(source, ['007_future'])
    elif kind == 'legacy':
        source.unlink()
        legacy_database(source)
    elif kind == 'unknown':
        source.unlink()
        with sqlite3.connect(source) as conn:
            conn.execute('CREATE TABLE unrelated (value TEXT)')
    elif kind == 'invalid':
        with sqlite3.connect(source) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = '002_detection_media_columns'")
    elif kind == 'wal':
        payload = source.read_bytes()
        source.write_bytes(payload[:18] + bytes([2, 2]) + payload[20:])
    elif kind in ('symlink', 'hardlink'):
        other = tmp_path / 'alias.db'
        if kind == 'symlink':
            other.symlink_to(source)
        else:
            os.link(source, other)
        source = other
    elif kind == 'symlink_parent':
        parent = tmp_path / 'alias'
        parent.symlink_to(tmp_path, target_is_directory=True)
        source = parent / source.name
    elif kind in ('fifo', 'directory'):
        source.unlink()
        os.mkfifo(source) if kind == 'fifo' else source.mkdir()
    assert_clean_failure(create(source), code)


def test_source_full_integrity_failure(source, monkeypatch):
    monkeypatch.setattr(service, '_full_integrity', lambda *args: False)
    assert_clean_failure(create(source), service.BackupCode.SOURCE_UNHEALTHY)


def test_concurrent_committed_writes_produce_consistent_snapshot(source, tmp_path):
    with sqlite3.connect(source) as conn:
        conn.executemany("INSERT INTO system_events (timestamp,severity,event_type,message) VALUES ('t','i','seed',?)",
                         [('x' * 2048,) for _ in range(300)])
    begin, done = threading.Event(), threading.Event()
    errors = []

    def writer():
        if not begin.wait(5):
            return
        try:
            with sqlite3.connect(source, timeout=2) as conn:
                conn.executemany("INSERT INTO system_events (timestamp,severity,event_type,message) VALUES ('t','i','pair',?)",
                                 [('first',), ('second',)])
        except sqlite3.Error as exc:
            errors.append(type(exc).__name__)
        finally:
            done.set()

    def progress(snapshot):
        if snapshot.phase is service.BackupPhase.BACKING_UP and snapshot.pages_copied and not begin.is_set():
            begin.set()
            assert done.wait(3)

    worker = threading.Thread(target=writer)
    worker.start()
    try:
        result = create(source, page_batch=1, progress=progress)
    finally:
        begin.set()
        worker.join(timeout=5)
    assert result.success and done.is_set() and not errors and not worker.is_alive()
    with (service.resolve_backup_download(result.backup_id, wamf_paths.get_database_backup_root()) as download,
          zipfile.ZipFile(download.file) as archive):
        staged = tmp_path / 'snapshot.db'
        staged.write_bytes(archive.read('database.db'))
    with sqlite3.connect(staged.as_uri() + '?mode=ro', uri=True) as conn:
        assert conn.execute("SELECT count(*) FROM system_events WHERE event_type='seed'").fetchone() == (300,)
        assert conn.execute("SELECT count(*) FROM system_events WHERE event_type='pair'").fetchone()[0] in (0, 2)
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT count(*) FROM system_events WHERE event_type='pair'").fetchone() == (2,)


@pytest.mark.parametrize('phase', [service.BackupPhase.PREFLIGHT, service.BackupPhase.BACKING_UP,
                                  service.BackupPhase.VALIDATING, service.BackupPhase.PACKAGING,
                                  service.BackupPhase.PUBLISHING])
def test_cancel_before_publication(source, phase):
    cancel = threading.Event()

    def progress(snapshot):
        if snapshot.phase is phase:
            cancel.set()

    assert_clean_failure(create(source, progress=progress, cancelled=cancel.is_set), service.BackupCode.CANCELLED)


def test_deadline_during_copy(source, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(service.time, 'monotonic', lambda: clock[0])

    def progress(snapshot):
        if snapshot.pages_copied:
            clock[0] = 20

    assert_clean_failure(create(source, timeout_seconds=10, page_batch=1, progress=progress), service.BackupCode.DEADLINE)


def test_deadline_before_preflight(source):
    assert_clean_failure(create(source, timeout_seconds=0), service.BackupCode.DEADLINE)


def test_cancellation_interrupts_sqlite_scan_and_preserves_exception(source):
    class Cancelled(Exception):
        pass

    checks = []

    def checkpoint():
        checks.append(True)
        if len(checks) > 2:
            raise Cancelled('cancelled')

    with (sqlite3.connect(source) as conn, pytest.raises(Cancelled),
          sqlite_read_checkpoint(conn, checkpoint)):
        conn.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n').fetchone()
    assert inspect_database(source).compatibility is DatabaseCompatibility.CURRENT


def test_disk_space_preflight(source, monkeypatch):
    monkeypatch.setattr(service.os, 'fstatvfs', lambda fd: SimpleNamespace(f_bavail=0, f_frsize=4096))
    assert_clean_failure(create(source), service.BackupCode.INSUFFICIENT_SPACE)


@pytest.mark.parametrize('code', [errno.ENOSPC, errno.EACCES])
def test_staging_failure(source, monkeypatch, code):
    @contextmanager
    def failed_workspace():
        raise OSError(code, 'sensitive filesystem path')
        yield  # Make this a context manager without touching storage.

    monkeypatch.setattr(service, 'backup_workspace', failed_workspace)
    result = create(source)
    expected = service.BackupCode.INSUFFICIENT_SPACE if code == errno.ENOSPC else service.BackupCode.FILESYSTEM_ERROR
    assert_clean_failure(result, expected)
    assert 'sensitive' not in result.summary


def test_backup_api_failure(source, monkeypatch):
    def failed_copy(*args):
        raise sqlite3.OperationalError('secret SQL data')

    monkeypatch.setattr(service, '_copy_database', failed_copy)
    result = create(source)
    assert_clean_failure(result, service.BackupCode.DATABASE_ERROR)
    assert 'secret' not in result.summary


@pytest.mark.parametrize('kind', ['schema', 'integrity'])
def test_destination_verification_failure(source, monkeypatch, kind):
    if kind == 'schema':
        original = service._copy_database

        def corrupt(src, dst, *args):
            original(src, dst, *args)
            dst.execute('DROP TABLE species_info')
            dst.commit()

        monkeypatch.setattr(service, '_copy_database', corrupt)
    else:
        monkeypatch.setattr(service, '_full_integrity', lambda path, operation: path == source)
    assert_clean_failure(create(source), service.BackupCode.DESTINATION_INVALID)


def test_package_self_validation_failure(source, monkeypatch):
    original = package.validate_package

    def invalid(*args, **kwargs):
        return replace(original(*args, **kwargs), code=package.ValidationCode.CHECKSUM_MISMATCH)

    monkeypatch.setattr(package, 'validate_package', invalid)
    assert_clean_failure(create(source), service.BackupCode.PACKAGE_INVALID)


def test_package_file_fsync_failure(source, monkeypatch):
    def failed_sync(fd):
        raise OSError(errno.EIO, 'secret disk details')

    monkeypatch.setattr(service.os, 'fsync', failed_sync)
    assert_clean_failure(create(source), service.BackupCode.FILE_SYNC_FAILED)


def test_directory_fsync_uncertainty_keeps_valid_published_artifact(source, monkeypatch):
    original = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'directory fsync unsupported')
        original(fd)

    monkeypatch.setattr(service.os, 'fsync', sync)
    result = create(source)
    assert result.success and result.durability_warning
    assert result.code is service.BackupCode.DURABILITY_UNCERTAIN
    assert package.validate_package(storage.BackupIdentity(result.backup_id)).valid


@pytest.mark.parametrize('kind', ['replace', 'collision'])
def test_failed_publication_preserves_existing_backup(source, monkeypatch, kind):
    first = create(source)
    assert first.success
    path = published()[0]
    before = path.read_bytes(), path.stat().st_mtime_ns
    if kind == 'replace':
        def fail(*args, **kwargs):
            raise OSError(errno.EIO, 'replace failed')
        monkeypatch.setattr(service.os, 'replace', fail)
        code = service.BackupCode.PUBLICATION_FAILED
    else:
        identity = storage.BackupIdentity(first.backup_id)
        monkeypatch.setattr(storage.BackupIdentity, 'generate', lambda: identity)
        code = service.BackupCode.COLLISION
    second = create(source)
    assert not second.success and second.code is code
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert published() == [path]
    assert list(wamf_paths.get_database_backup_staging_root().iterdir()) == []


def test_two_simultaneous_creators_use_dedicated_root_guard(source):
    entered, release = threading.Event(), threading.Event()
    results = []

    def progress(snapshot):
        if snapshot.phase is service.BackupPhase.BACKING_UP and not entered.is_set():
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=lambda: results.append(create(source, progress=progress)))
    thread.start()
    try:
        assert entered.wait(5)
        second = create(source)
        assert second.code is service.BackupCode.BUSY
        # The backup guard must not exclude ordinary SQLite writes.
        with sqlite3.connect(source, timeout=1) as conn:
            conn.execute("INSERT INTO system_events(timestamp,severity,event_type,message) VALUES ('t','i','test','normal write')")
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and len(results) == 1 and results[0].success


def test_enumeration_isolates_corrupt_artifact_and_ignores_reservations(source):
    result = create(source)
    with storage.reserve_backup(staged=False) as (corrupt, file):
        file.write(b'corrupt package')
    with storage.reserve_backup(staged=False):
        pass  # Unpublished empty reservation.
    with storage.reserve_backup():
        pass  # Private staging artifact.
    entries = service.list_backups(wamf_paths.get_database_backup_root())
    assert len(entries) == 2
    by_id = {entry.backup_id: entry for entry in entries}
    assert by_id[result.backup_id].validation.valid
    assert by_id[corrupt.backup_id].validation.code is package.ValidationCode.INVALID_PACKAGE


@pytest.mark.parametrize('identifier', ['../escape.zip', '/tmp/arbitrary', 'a.zip', ''])
def test_download_rejects_arbitrary_identifiers(identifier):
    with (pytest.raises(storage.BackupStorageError),
          service.resolve_backup_download(identifier, wamf_paths.get_database_backup_root())):
        pytest.fail('Unsafe ID accepted')


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'swap'])
def test_download_rejects_aliases_and_path_swaps(source, monkeypatch, tmp_path, kind):
    result = create(source)
    path = published()[0]
    outside = tmp_path / 'outside.zip'
    outside.write_bytes(b'arbitrary external content')
    if kind == 'swap':
        original = package.validate_open_package

        def swapped(*args, **kwargs):
            validation = original(*args, **kwargs)
            path.unlink()
            path.symlink_to(outside)
            return validation

        monkeypatch.setattr(package, 'validate_open_package', swapped)
    else:
        if kind == 'hardlink':
            os.link(path, tmp_path / 'alias.zip')
        else:
            path.unlink()
            path.symlink_to(outside) if kind == 'symlink' else os.mkfifo(path)
    with (pytest.raises((storage.BackupStorageError, OSError)),
          service.resolve_backup_download(result.backup_id, wamf_paths.get_database_backup_root())):
        pytest.fail('Unsafe download accepted')


def test_download_holds_validated_inode_after_later_path_replacement(source, tmp_path):
    result = create(source)
    with service.resolve_backup_download(result.backup_id, wamf_paths.get_database_backup_root()) as download:
        path = published()[0]
        path.unlink()
        path.write_bytes(b'replacement must not be streamed')
        assert hashlib.sha256(download.file.read()).hexdigest() == result.package_sha256


def test_empty_listing_does_not_create_storage(tmp_path):
    assert service.list_backups(wamf_paths.get_database_backup_root()) == ()
    assert not (tmp_path / 'data').exists()


def test_alternative_root_refused(source, tmp_path):
    result = service.create_backup(source, tmp_path / 'elsewhere')
    assert_clean_failure(result, service.BackupCode.UNSAFE_STORAGE)


def test_guard_excludes_a_separate_process(source, tmp_path):
    script = '''
import sys
from pathlib import Path
import wamf_paths
from app.database_backup_service import create_backup
wamf_paths.REPO_ROOT = Path(sys.argv[1])
result = create_backup(Path(sys.argv[2]), wamf_paths.get_database_backup_root())
print(result.code.value)
'''
    with storage.backup_operation_guard():
        child = subprocess.run([sys.executable, '-B', '-c', script, str(tmp_path), str(source)],
                               cwd=Path(service.__file__).resolve().parent.parent,
                               capture_output=True, text=True, timeout=10, check=True)
    assert child.stdout.strip() == service.BackupCode.BUSY.value
    assert published() == []


def test_enumeration_preserves_rejected_schema_classification(source):
    from tests.test_database_backup import manifest_for
    from tests.test_database_backup import package as write_package

    manifest = manifest_for(source)
    add_history(source, ['007_future'])
    payload = source.read_bytes()
    manifest = replace(manifest, database_size=len(payload),
                       database_sha256=hashlib.sha256(payload).hexdigest())
    identity = write_package(payload, manifest, staged=False)
    entries = service.list_backups(wamf_paths.get_database_backup_root())
    assert len(entries) == 1 and entries[0].backup_id == identity.backup_id
    assert entries[0].compatibility is DatabaseCompatibility.NEWER
    assert entries[0].validation.code is package.ValidationCode.NEWER_DATABASE
    assert not entries[0].validation.valid


def test_inspection_cancellation_propagates_before_sql(source):
    class Cancelled(Exception):
        pass

    def stop():
        raise Cancelled('cancelled')

    before = source.read_bytes()
    with pytest.raises(Cancelled):
        inspect_database(source, check_integrity=True, checkpoint=stop)
    assert source.read_bytes() == before


def test_busy_backup_api_retry_is_bounded_by_deadline(source, monkeypatch):
    original = service._copy_database

    def blocked_copy(src, dst, operation, *args):
        with sqlite3.connect(source, isolation_level=None) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            try:
                operation.deadline = service.time.monotonic() + 0.05
                original(src, dst, operation, *args)
            finally:
                writer.rollback()

    monkeypatch.setattr(service, '_copy_database', blocked_copy)
    assert_clean_failure(create(source, timeout_seconds=5), service.BackupCode.DEADLINE)


def test_failed_partial_copy_rolls_back_and_cleans_staging(source, monkeypatch):
    before = source.read_bytes()

    def failed_copy(src, dst, *args):
        dst.execute('CREATE TABLE partial (value TEXT)')
        dst.execute("INSERT INTO partial VALUES ('unfinished')")
        raise sqlite3.OperationalError('copy failed')

    monkeypatch.setattr(service, '_copy_database', failed_copy)
    assert_clean_failure(create(source), service.BackupCode.DATABASE_ERROR)
    assert source.read_bytes() == before


def test_inaccessible_source(source, monkeypatch):
    original = os.stat

    def denied(path, *args, **kwargs):
        if path == source.name:
            raise PermissionError(errno.EACCES, 'private source path')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(service.os, 'stat', denied)
    assert_clean_failure(create(source), service.BackupCode.SOURCE_UNAVAILABLE)


def test_source_replacement_during_operation_refuses_publication(source, tmp_path):
    replaced = []

    def progress(snapshot):
        if snapshot.phase is service.BackupPhase.BACKING_UP and not replaced:
            source.rename(tmp_path / 'preserved.db')
            db.ensure_schema(source)
            replaced.append(True)

    assert_clean_failure(create(source, progress=progress), service.BackupCode.SOURCE_UNSAFE)


def test_online_backup_does_not_release_another_connections_posix_locks(source):
    # A raw open/close of source in this process would drop the SQLite writer's
    # process-owned POSIX locks, even if that raw FD was opened read-only.
    script = '''
import sqlite3, sys
with sqlite3.connect(sys.argv[1], timeout=0.1) as conn:
    try:
        conn.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError:
        print('locked')
    else:
        conn.rollback()
        print('acquired')
'''
    with sqlite3.connect(source) as writer:
        writer.execute('BEGIN IMMEDIATE')
        result = create(source)
        assert result.success
        probe = subprocess.run([sys.executable, '-B', '-c', script, str(source)],
                               capture_output=True, text=True, check=True, timeout=5)
        assert probe.stdout.strip() == 'locked'
        writer.rollback()


def test_source_inspection_child_is_reaped_on_cancellation(source, monkeypatch):
    started, children = threading.Event(), []
    original = subprocess.Popen

    def start(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(service.subprocess, 'Popen', start)
    result = create(source, cancelled=started.is_set)
    assert_clean_failure(result, service.BackupCode.CANCELLED)
    assert len(children) == 1 and children[0].poll() is not None


def test_parent_never_reads_source_with_raw_file_io(source, monkeypatch):
    original = Path.open

    def opened(path, *args, **kwargs):
        if path == source:
            pytest.fail('Raw source open/close is unsafe with sibling SQLite connections')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', opened)
    assert create(source).success


def test_source_probe_refuses_unrelated_parent(source):
    child = subprocess.run([sys.executable, '-B', '-m', 'app.database_backup_probe',
                            str(os.getpid() + 1000000), str(source)],
                           cwd=Path(service.__file__).resolve().parent.parent,
                           capture_output=True, text=True, timeout=5, check=False)
    assert child.returncode == 2 and child.stdout == ''
