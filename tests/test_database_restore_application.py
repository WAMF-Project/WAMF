"""Offline application/recovery use synthetic databases and fake lifecycle owners."""

import os
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import wamf_paths
from app import database_restore as staging
from app import database_restore_application as application
from app import db
from app.database_backup import validate_package
from app.database_backup_service import create_backup, resolve_backup_download
from app.database_backup_storage import BackupIdentity
from app.database_lifecycle import (
    DatabaseLifecycleError,
    database_consumer,
    exclusive_database_lifecycle,
    maintenance_access,
)
from app.database_restore_storage import operation_store
from app.db_inspection import DatabaseCompatibility, inspect_database
from tests.test_database_backup import manifest_for
from tests.test_database_backup import package as write_package
from tests.test_db_migrations import current_prebaseline, legacy_database


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(wamf_paths, 'REPO_ROOT', tmp_path)


def put_event(path, message):
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO system_events(timestamp,severity,event_type,message) VALUES (?,?,?,?)',
                     ('t', 'INFO', 'TEST', message))


def messages(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        return [row[0] for row in conn.execute('SELECT message FROM system_events ORDER BY id')]


@pytest.fixture
def prepared(tmp_path):
    target, candidate = tmp_path / 'active.db', tmp_path / 'input.db'
    db.ensure_schema(target)
    put_event(target, 'current active')
    db.ensure_schema(candidate)
    put_event(candidate, 'restored data')
    backup = write_package(candidate.read_bytes(), manifest_for(candidate), staged=False)
    result = staging.stage_restore_from_backup(backup.backup_id, target)
    confirmed = staging.confirm_restore_plan(result.operation_id, result.plan.confirmation_token,
                                             bound_target_path=target)
    assert confirmed.success
    return target, result.operation_id, result.plan.confirmation_token


def read_record(identifier):
    with operation_store(identifier) as store:
        return store.read()


def apply(prepared):
    target, identifier, token = prepared
    with exclusive_database_lifecycle(target) as lease:
        return application.apply_restore(lease, identifier, token)


def finish_startup(target):
    with application.database_startup(target) as startup:
        with maintenance_access(startup.lease):
            db.ensure_schema(target)
        startup.allow_consumers()
        startup.mark_succeeded()


def test_current_restore_and_verified_rollback(prepared, tmp_path):
    target, identifier, _token = prepared
    permissions = stat.S_IMODE(target.stat().st_mode)
    result = apply(prepared)
    assert result.success, result
    assert result.phase is staging.RestorePhase.STARTUP_PENDING
    assert result.replacement_occurred and result.retention_hold_active
    assert not result.migration_performed and not result.recovery_required
    assert messages(target) == ['restored data']
    assert validate_package(BackupIdentity(result.rollback_artifact_id)).valid
    with resolve_backup_download(result.rollback_artifact_id, wamf_paths.get_database_backup_root()) as download:
        assert download.package_sha256 == result.rollback_package_sha256
        assert download.validation.facts.database_sha256 == read_record(identifier)['application']['rollback_db_sha256']
        with zipfile.ZipFile(download.file) as archive:
            preserved = tmp_path / 'rollback-verified.db'
            preserved.write_bytes(archive.read('database.db'))
        assert messages(preserved) == ['current active']
    with pytest.raises(DatabaseLifecycleError):
        db.connect_db(target)
    finish_startup(target)
    assert application.get_application_status(identifier).phase is staging.RestorePhase.COMPLETE
    assert application.retention_hold_active(target)
    with db.connect_db(target) as conn:
        assert conn.execute('SELECT count(*) FROM system_events').fetchone()[0] == 1
    assert stat.S_IMODE(target.stat().st_mode) == permissions
    assert not staging.cancel_restore(identifier).success


@pytest.mark.parametrize('kind', ['upgradeable', 'legacy'])
def test_migrates_working_copy_only(tmp_path, monkeypatch, kind):
    target, candidate = tmp_path / 'active.db', tmp_path / 'older.db'
    db.ensure_schema(target)
    (current_prebaseline if kind == 'upgradeable' else legacy_database)(candidate)
    original_candidate = candidate.read_bytes()
    backup = write_package(original_candidate, manifest_for(candidate), staged=False)
    staged = staging.stage_restore_from_backup(backup.backup_id, target)
    staging.confirm_restore_plan(staged.operation_id, staged.plan.confirmation_token, bound_target_path=target)
    calls = []
    original = application.ensure_schema

    def migrate(path):
        calls.append(path)
        assert path != target and path.name == 'working.db'
        assert path.parent.parent == target.parent
        original(path)

    monkeypatch.setattr(application, 'ensure_schema', migrate)
    result = apply((target, staged.operation_id, staged.plan.confirmation_token))
    assert result.success and result.migration_performed and len(calls) == 1
    assert inspect_database(target).compatibility is DatabaseCompatibility.CURRENT
    preserved = wamf_paths.get_database_restore_root() / staged.operation_id / 'candidate.db'
    assert preserved.read_bytes() == original_candidate


def test_migration_failure_preserves_every_original(tmp_path, monkeypatch):
    target, candidate = tmp_path / 'active.db', tmp_path / 'older.db'
    db.ensure_schema(target)
    legacy_database(candidate)
    backup = write_package(candidate.read_bytes(), manifest_for(candidate), staged=False)
    staged = staging.stage_restore_from_backup(backup.backup_id, target)
    staging.confirm_restore_plan(staged.operation_id, staged.plan.confirmation_token, bound_target_path=target)
    before = target.read_bytes()

    def fail(path):
        raise db.SchemaMigrationError('private migration error')

    monkeypatch.setattr(application, 'ensure_schema', fail)
    result = apply((target, staged.operation_id, staged.plan.confirmation_token))
    assert not result.success and not result.replacement_occurred
    assert target.read_bytes() == before
    assert not list(tmp_path.glob('.wamf-restore-*'))
    assert (wamf_paths.get_database_restore_root() / staged.operation_id / 'candidate.db').read_bytes() == candidate.read_bytes()
    assert (wamf_paths.get_database_backup_root() / backup.filename).exists()


def test_lifecycle_excludes_consumers_connections_and_backup(prepared):
    target, identifier, token = prepared
    with database_consumer(target), pytest.raises(DatabaseLifecycleError), exclusive_database_lifecycle(target):
        pytest.fail('Live consumer must exclude maintenance')
    conn = db.connect_db(target)
    try:
        with pytest.raises(DatabaseLifecycleError), exclusive_database_lifecycle(target):
            pytest.fail('Open SQLite connection must exclude maintenance')
    finally:
        conn.close()
    with exclusive_database_lifecycle(target) as lease:
        with pytest.raises(DatabaseLifecycleError), database_consumer(target):
            pytest.fail('Offline ownership must exclude consumers')
        assert not create_backup(target, wamf_paths.get_database_backup_root()).success
        result = application.apply_restore(lease, identifier, token)
        assert result.success
    with pytest.raises(DatabaseLifecycleError):
        application.apply_restore(lease, identifier, token)


def test_rollback_creation_failure_blocks_swap(prepared, monkeypatch):
    target, _, _ = prepared
    before = target.read_bytes()

    def fail(*args):
        raise application.RestoreStorageError('preservation failed')

    monkeypatch.setattr(application, '_preserve_active', fail)
    result = apply(prepared)
    assert result.code is application.ApplyCode.ROLLBACK_FAILED
    assert not result.replacement_occurred and target.read_bytes() == before


@pytest.mark.parametrize('moment', ['before_apply', 'before_swap'])
def test_target_identity_change_refuses_replacement(prepared, monkeypatch, tmp_path, moment):
    target, _, _ = prepared
    before = target.read_bytes()

    def swap_target():
        target.rename(tmp_path / 'old.db')
        target.write_bytes(before)

    if moment == 'before_apply':
        swap_target()
    else:
        original = application._replace_active

        def changed(*args):
            swap_target()
            original(*args)

        monkeypatch.setattr(application, '_replace_active', changed)
    result = apply(prepared)
    assert not result.success and target.read_bytes() == before


def test_atomic_replace_failure_leaves_active_untouched(prepared, monkeypatch):
    target, _, _ = prepared
    before = target.read_bytes()
    original = os.replace

    def replace_file(source, destination, **kwargs):
        if source == 'working.db':
            raise OSError('replace failed')
        return original(source, destination, **kwargs)

    monkeypatch.setattr(application.os, 'replace', replace_file)
    result = apply(prepared)
    assert result.code is application.ApplyCode.REPLACEMENT_FAILED
    assert not result.replacement_occurred and target.read_bytes() == before
    assert validate_package(BackupIdentity(result.rollback_artifact_id)).valid


class Crash(BaseException):
    pass


@pytest.mark.parametrize('moment', ['before_swap', 'after_swap'])
def test_crash_reconciliation_never_reapplies(prepared, monkeypatch, moment):
    target, identifier, token = prepared
    original = application._replace_active

    def crash(*args):
        if moment == 'after_swap':
            original(*args)
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(application, '_replace_active', crash)
        with pytest.raises(Crash):
            apply(prepared)
    assert read_record(identifier)['phase'] == 'replacing'
    with exclusive_database_lifecycle(target) as lease:
        recovered = application.reconcile_restore_startup(lease)
        if moment == 'before_swap':
            assert recovered.phase is staging.RestorePhase.FAILED
            assert messages(target) == ['current active']
        else:
            assert recovered.phase is staging.RestorePhase.STARTUP_PENDING
            assert recovered.replacement_occurred and messages(target) == ['restored data']
        duplicate = application.apply_restore(lease, identifier, token)
        assert not duplicate.success


def test_uncertain_crash_blocks_startup_and_preserves_evidence(prepared, monkeypatch):
    target, identifier, _ = prepared

    def crash(*args):
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(application, '_replace_active', crash)
        with pytest.raises(Crash):
            apply(prepared)
    put_event(target, 'unexpected external change')
    with exclusive_database_lifecycle(target) as lease, pytest.raises(application.RestoreStorageError):
        application.reconcile_restore_startup(lease)
    assert read_record(identifier)['phase'] == 'recovery_required'
    assert (wamf_paths.get_database_restore_root() / identifier / 'candidate.db').exists()
    with pytest.raises(application.RestoreStorageError), application.database_startup(target):
        pytest.fail('Uncertain recovery must block startup')


def test_failed_startup_preserves_restored_and_rollback_then_explicit_rollback(prepared, tmp_path):
    target, identifier, _ = prepared
    result = apply(prepared)
    restored = target.read_bytes()
    with exclusive_database_lifecycle(target) as lease:
        failed = application.finish_restore_startup(lease, succeeded=False)
        assert failed.phase is staging.RestorePhase.RECOVERY_REQUIRED
        assert failed.rollback_artifact_id == result.rollback_artifact_id
        assert target.read_bytes() == restored
        rolled_back = application.rollback_restore(lease, identifier,
                                                   expected_target_identity=application.offline_target_identity(lease))
    assert rolled_back.success and rolled_back.replacement_occurred
    assert messages(target) == ['current active']
    assert validate_package(BackupIdentity(rolled_back.recovery_artifact_id)).valid
    with resolve_backup_download(rolled_back.recovery_artifact_id, wamf_paths.get_database_backup_root()) as recovery:
        assert recovery.package_sha256 == rolled_back.recovery_package_sha256
        with zipfile.ZipFile(recovery.file) as archive:
            recovered = tmp_path / 'recovery-verified.db'
            recovered.write_bytes(archive.read('database.db'))
        assert messages(recovered) == ['restored data']
    finish_startup(target)
    assert application.retention_hold_active(target)


def test_no_sidecar_deletion_or_empty_database_initialization(prepared):
    target, identifier, _ = prepared
    journal = Path(f'{target}-journal')
    journal.write_bytes(b'preserve unknown journal')
    before = target.read_bytes()
    assert not apply(prepared).success
    assert journal.read_bytes() == b'preserve unknown journal' and target.read_bytes() == before
    journal.unlink()
    # Use a new plan because the rejected operation cannot be applied twice.
    with operation_store(identifier) as store:
        record = store.read()
        record['application'] = None
        record['phase'] = 'confirmed'
        store.write(record)
    assert apply(prepared).success
    target.unlink()
    with pytest.raises(application.RestoreStorageError), application.database_startup(target):
        pytest.fail('Missing restored DB must not initialize an empty DB')
    assert not target.exists()


def test_file_fsync_failure_is_pre_replacement(prepared, monkeypatch):
    target, _, _ = prepared
    original = os.fsync
    before = target.read_bytes()

    def sync(fd):
        if os.readlink(f'/proc/self/fd/{fd}').endswith('/working.db'):
            raise OSError('working file fsync failed')
        return original(fd)

    monkeypatch.setattr(application.os, 'fsync', sync)
    result = apply(prepared)
    assert not result.success and not result.replacement_occurred
    assert target.read_bytes() == before


def test_directory_fsync_uncertainty_requires_explicit_recovery(prepared, monkeypatch):
    target, identifier, _ = prepared
    parent = target.parent.stat()
    original = os.fsync

    def sync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and info.st_ino == parent.st_ino and info.st_dev == parent.st_dev:
            raise OSError('directory fsync unsupported')
        return original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(application.os, 'fsync', sync)
        result = apply(prepared)
    assert result.replacement_occurred and result.durability_warning and result.recovery_required
    assert messages(target) == ['restored data']
    with exclusive_database_lifecycle(target) as lease:
        with pytest.raises(application.RestoreStorageError):
            application.reconcile_restore_startup(lease)
        recovered = application.reconcile_restore_startup(lease, explicit_recovery=True)
        assert recovered.phase is staging.RestorePhase.STARTUP_PENDING
    assert not staging.cancel_restore(identifier).success


def test_crash_during_working_preparation_is_clearly_pre_replacement(prepared, monkeypatch):
    target, identifier, _ = prepared

    def crash(*args, **kwargs):
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(application, '_prepare', crash)
        with pytest.raises(Crash):
            apply(prepared)
    assert read_record(identifier)['phase'] == 'preparing'
    with exclusive_database_lifecycle(target) as lease:
        result = application.reconcile_restore_startup(lease)
    assert result.phase is staging.RestorePhase.FAILED and not result.replacement_occurred
    assert messages(target) == ['current active']


def test_failed_startup_is_never_automatically_rolled_back(prepared):
    target, identifier, _ = prepared
    assert apply(prepared).success
    with pytest.raises(RuntimeError), application.database_startup(target):
        raise RuntimeError('fake startup validation failure')
    assert read_record(identifier)['phase'] == 'recovery_required'
    assert messages(target) == ['restored data']
    with pytest.raises(application.RestoreStorageError), application.database_startup(target):
        pytest.fail('Failed startup must require an explicit operator action')


def test_retention_hold_survives_startup_and_skips_deletion(prepared, tmp_path):
    from app.retention_service import run_retention
    from tests.test_retention_service import retention_config

    target, _, _ = prepared
    assert apply(prepared).success
    finish_startup(target)
    config = retention_config(tmp_path, delete_orphaned_media=True)
    config['storage']['database_path'] = str(target)
    orphan = Path(config['media']['snapshots_path']) / 'orphan.jpg'
    orphan.write_bytes(b'keep this media')
    result = run_retention(config=config)
    assert result.outcome == 'held'
    assert all(phase.outcome == 'skipped' for phase in result.phases.values())
    assert orphan.read_bytes() == b'keep this media'
    assert any('restore hold' in summary for summary in result.error_summaries)


def test_complete_restore_missing_database_is_not_initialized(prepared):
    target, _, _ = prepared
    assert apply(prepared).success
    finish_startup(target)
    target.unlink()
    with pytest.raises(application.RestoreStorageError), application.database_startup(target):
        pytest.fail('Missing database requires recovery')
    assert not target.exists()


def test_post_replace_record_io_failure_is_recovery_required(prepared, monkeypatch):
    from app.database_restore_storage import OperationStore

    target, identifier, _ = prepared
    original = OperationStore.write

    def fail_post_swap(store, record):
        if record.get('phase') in ('replaced', 'startup_pending', 'recovery_required'):
            raise OSError('metadata disk failure after rename')
        original(store, record)

    with monkeypatch.context() as patch:
        patch.setattr(OperationStore, 'write', fail_post_swap)
        result = apply(prepared)
    assert result.recovery_required and result.replacement_occurred
    assert messages(target) == ['restored data']
    assert read_record(identifier)['phase'] == 'replacing'
    with exclusive_database_lifecycle(target) as lease:
        assert application.reconcile_restore_startup(lease).phase is staging.RestorePhase.STARTUP_PENDING


def test_repeated_apply_does_not_mutate_startup_pending_record(prepared):
    target, identifier, token = prepared
    assert apply(prepared).success
    before = read_record(identifier)
    with exclusive_database_lifecycle(target) as lease:
        result = application.apply_restore(lease, identifier, token)
    assert not result.success and result.replacement_occurred
    assert read_record(identifier) == before


def test_offline_rollback_checks_explicit_target_identity(prepared):
    target, identifier, _ = prepared
    assert apply(prepared).success
    before = read_record(identifier)
    with exclusive_database_lifecycle(target) as lease:
        identity = application.offline_target_identity(lease)
        identity['inode'] += 1
        result = application.rollback_restore(lease, identifier, expected_target_identity=identity)
    assert not result.success and read_record(identifier) == before
    assert messages(target) == ['restored data']


def test_lifecycle_exclusion_crosses_process_boundaries(prepared):
    target, _, _ = prepared
    script = '''
import sys
from app.database_lifecycle import database_consumer, DatabaseLifecycleError
try:
    with database_consumer(sys.argv[1]):
        print('unsafe')
except DatabaseLifecycleError:
    print('excluded')
'''
    with exclusive_database_lifecycle(target):
        child = subprocess.run([sys.executable, '-B', '-c', script, str(target)],
                               capture_output=True, text=True, timeout=5, check=True)
    assert child.stdout.strip() == 'excluded'


def test_backup_observer_cannot_recursively_create_another_backup(prepared):
    target, _, _ = prepared
    nested = []

    def observer(snapshot):
        if not nested:
            nested.append(create_backup(target, wamf_paths.get_database_backup_root()))

    result = create_backup(target, wamf_paths.get_database_backup_root(), progress=observer)
    assert result.success and len(nested) == 1 and not nested[0].success


def test_completed_restore_does_not_block_future_schema_migrations(prepared, monkeypatch):
    target, _, _ = prepared
    assert apply(prepared).success
    finish_startup(target)
    next_migration = db.Migration(8, '008_future', lambda conn: None)
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS + (next_migration,))
    finish_startup(target)
    assert inspect_database(target).recognized_sequence == 8


def test_standalone_flask_holds_a_lifetime_consumer_lease(prepared, tmp_path):
    import webui
    from tests.test_database_startup import runtime_config

    target, _, _ = prepared
    config = runtime_config(tmp_path)
    config['storage']['database_path'] = str(target)
    webui.initialize_app(config)
    with pytest.raises(DatabaseLifecycleError), exclusive_database_lifecycle(target):
        pytest.fail('An initialized Flask app must exclude offline restore')


def test_crash_after_startup_validation_preserves_partial_startup_writes(prepared):
    target, identifier, _ = prepared
    assert apply(prepared).success
    # Model abrupt process loss after validation/downgrade, before startup's
    # success/failure hook could run. No Python finally failure hook is invoked.
    with exclusive_database_lifecycle(target) as lease:
        application.reconcile_restore_startup(lease)
        startup = application.DatabaseStartup(lease)
        startup.allow_consumers()
        put_event(target, 'committed during partial startup')
    assert read_record(identifier)['phase'] == 'startup_pending'
    with exclusive_database_lifecycle(target) as lease:
        recovered = application.reconcile_restore_startup(lease)
    assert recovered.replacement_occurred and recovered.phase is staging.RestorePhase.STARTUP_PENDING
    assert messages(target) == ['restored data', 'committed during partial startup']
    finish_startup(target)


def test_exclusive_owner_cannot_apply_with_an_open_maintenance_connection(prepared):
    target, identifier, token = prepared
    with exclusive_database_lifecycle(target) as lease:
        with maintenance_access(lease):
            connection = db.connect_db(target)
        try:
            with pytest.raises(DatabaseLifecycleError):
                application.apply_restore(lease, identifier, token)
        finally:
            connection.close()
        assert application.apply_restore(lease, identifier, token).success


def test_escaped_maintenance_connection_keeps_kernel_exclusion(prepared):
    target, _, _ = prepared
    with exclusive_database_lifecycle(target) as lease, maintenance_access(lease):
        connection = db.connect_db(target)
    try:
        with pytest.raises(DatabaseLifecycleError), exclusive_database_lifecycle(target):
            pytest.fail('Escaped connection must retain lifecycle ownership')
    finally:
        connection.close()
    with exclusive_database_lifecycle(target):
        pass


def test_pre_replacement_failure_keeps_old_database_authoritative_after_new_writes(prepared, monkeypatch):
    target, _, _ = prepared

    def fail(*args):
        raise application.RestoreStorageError('rollback storage unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(application, '_preserve_active', fail)
        result = apply(prepared)
    assert not result.replacement_occurred
    put_event(target, 'new observation after failed restore')
    finish_startup(target)
    assert messages(target) == ['current active', 'new observation after failed restore']
