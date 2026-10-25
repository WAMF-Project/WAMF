"""Restore preview never opens or replaces the synthetic active target database."""

import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from enum import StrEnum
from pathlib import Path

import pytest

import wamf_paths
from app import database_backup as package
from app import database_restore as restore
from app import database_restore_storage as storage
from app import db
from app.db_inspection import DatabaseCompatibility
from tests.test_database_backup import manifest_for
from tests.test_database_backup import package as write_package
from tests.test_db_migrations import add_history, current_prebaseline, legacy_database


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(wamf_paths, 'REPO_ROOT', tmp_path)


@pytest.fixture
def target(tmp_path):
    path = tmp_path / 'active.db'
    db.ensure_schema(path)
    before = path.read_bytes()
    yield path
    assert path.read_bytes() == before, 'Slice 3 must not mutate the active DB'


@pytest.fixture
def backup(tmp_path):
    path = tmp_path / 'source.db'
    db.ensure_schema(path)
    return write_package(path.read_bytes(), manifest_for(path), staged=False)


def stage(backup, target, **kwargs):
    return restore.stage_restore_from_backup(backup.backup_id, target, **kwargs)


def directory(identifier):
    return wamf_paths.get_database_restore_root() / identifier


def record(identifier):
    with storage.operation_store(identifier) as store:
        return store.read()


def test_current_managed_preview_and_confirmation(backup, target, monkeypatch):
    original = wamf_paths.get_database_backup_root() / backup.filename
    before = original.read_bytes(), original.stat().st_mtime_ns

    def forbidden(*args, **kwargs):
        pytest.fail('Staging must not migrate or resolve runtime config')

    monkeypatch.setattr(db, 'ensure_schema', forbidden)
    monkeypatch.setattr(db, 'connect_db', forbidden)
    monkeypatch.setattr(wamf_paths, 'get_database_path', forbidden)
    result = stage(backup, target)
    assert result.success and result.phase is restore.RestorePhase.READY
    plan = result.plan
    assert plan.compatibility is DatabaseCompatibility.CURRENT
    assert not plan.migration_required and not plan.automatic_upgrade_supported
    assert plan.detected_sequence == plan.latest_sequence == 6
    assert plan.detected_migration_id == plan.latest_migration_id == db.BASELINE_MIGRATION
    assert plan.source_backup_id == backup.backup_id
    assert plan.source_package_sha256 == hashlib.sha256(before[0]).hexdigest()
    assert plan.media_warning == restore.MEDIA_WARNING
    assert str(target) not in repr(plan)
    assert restore.get_restore_plan(result.operation_id) == plan
    checked = restore.revalidate_restore_plan(result.operation_id, bound_target_path=target)
    assert checked.success and checked.plan == plan
    confirmed = restore.confirm_restore_plan(result.operation_id, plan.confirmation_token, bound_target_path=target)
    assert confirmed.phase is restore.RestorePhase.CONFIRMED and confirmed.plan == plan
    assert record(result.operation_id)['confirmed_at'] is not None
    assert restore.revalidate_restore_plan(result.operation_id).phase is restore.RestorePhase.CONFIRMED
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before


@pytest.mark.parametrize('kind', ['upgradeable', 'legacy'])
def test_eligible_older_database_preview(tmp_path, target, kind):
    source = tmp_path / 'older.db'
    (current_prebaseline if kind == 'upgradeable' else legacy_database)(source)
    identity = write_package(source.read_bytes(), manifest_for(source), staged=False)
    result = stage(identity, target)
    assert result.success and result.plan.compatibility.value == kind
    assert result.plan.migration_required and result.plan.automatic_upgrade_supported
    assert restore.revalidate_restore_plan(result.operation_id).plan == result.plan
    candidate = directory(result.operation_id) / 'candidate.db'
    assert candidate.read_bytes() == source.read_bytes()
    if kind == 'legacy':
        assert result.plan.detected_sequence is result.plan.detected_migration_id is None


@pytest.mark.parametrize('kind,expected', [
    ('newer', package.ValidationCode.NEWER_DATABASE),
    ('corrupt', package.ValidationCode.INVALID_DATABASE),
    ('invalid', package.ValidationCode.INVALID_DATABASE),
    ('foreign', package.ValidationCode.UNKNOWN_DATABASE),
    ('unknown', package.ValidationCode.UNKNOWN_DATABASE),
    ('wal', package.ValidationCode.WAL_UNSUPPORTED),
])
def test_rejected_database_never_has_plan(tmp_path, target, kind, expected):
    source = tmp_path / 'input.db'
    db.ensure_schema(source)
    manifest = manifest_for(source)
    if kind == 'newer':
        add_history(source, ['007_future'])
    elif kind == 'invalid':
        with sqlite3.connect(source) as conn:
            conn.execute('DROP TABLE species_info')
    elif kind in ('foreign', 'unknown'):
        source.unlink()
        if kind == 'foreign':
            with sqlite3.connect(source) as conn:
                conn.execute('CREATE TABLE foreign_data (value TEXT)')
        else:
            legacy_database(source)
            add_history(source, ['003_unrecognized'])
    payload = source.read_bytes()
    if kind == 'corrupt':
        payload = b'not sqlite'
    elif kind == 'wal':
        payload = payload[:18] + bytes([2, 2]) + payload[20:]
    manifest = replace(manifest, database_size=len(payload), database_sha256=hashlib.sha256(payload).hexdigest())
    identity = write_package(payload, manifest, staged=False)
    result = stage(identity, target)
    assert not result.success and result.phase is restore.RestorePhase.REJECTED
    assert result.validation_code is expected and result.plan is None
    assert record(result.operation_id)['plan'] is None
    assert not (directory(result.operation_id) / 'candidate.db').exists()
    with pytest.raises(restore.RestoreError):
        restore.get_restore_plan(result.operation_id)


def test_artifact_permissions_and_record_authentication(backup, target):
    result = stage(backup, target)
    root, operation = wamf_paths.get_database_restore_root(), directory(result.operation_id)
    for path in (root, operation):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for name in ('candidate.db', 'package.zip', 'operation.json', '.lock'):
        assert stat.S_IMODE((operation / name).stat().st_mode) == 0o600
        assert (operation / name).stat().st_nlink == 1
    assert len((root / '.record.key').read_bytes()) == 32
    data = record(result.operation_id)
    assert data['target']['path'] == str(target)  # Internal authenticated record only.
    assert data['plan'] == asdict(result.plan)
    assert data['source_package_sha256'] == result.plan.source_package_sha256


def test_upload_stream_and_file_handle_share_validation_boundary(backup, target):
    path = wamf_paths.get_database_backup_root() / backup.filename
    stream = io.BytesIO(path.read_bytes())
    stream.name = '../../untrusted-upload.zip'
    uploaded = restore.stage_restore_from_stream(stream, target)
    assert uploaded.success and uploaded.plan.source_backup_id is None
    assert uploaded.plan.source_package_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    with path.open('rb') as file:
        assert restore.stage_restore_from_stream(file, target).success
    assert not restore.stage_restore_from_stream(path, target).success
    assert not restore.stage_restore_from_stream(str(path), target).success


@pytest.mark.parametrize('which', ['candidate.db', 'package.zip'])
def test_tampering_revokes_confirmation(backup, target, which):
    ready = stage(backup, target)
    assert restore.confirm_restore_plan(ready.operation_id, ready.plan.confirmation_token,
                                        bound_target_path=target).success
    artifact = directory(ready.operation_id) / which
    with artifact.open('ab') as file:
        file.write(b'tampered')
    result = restore.revalidate_restore_plan(ready.operation_id, bound_target_path=target)
    assert result.code is restore.RestoreCode.ARTIFACT_CHANGED
    assert result.plan is None and result.phase is restore.RestorePhase.REJECTED
    assert record(ready.operation_id)['confirmed_at'] is None
    assert not restore.confirm_restore_plan(ready.operation_id, ready.plan.confirmation_token,
                                           bound_target_path=target).success


def test_tokens_bind_different_candidates_and_operations(backup, target, tmp_path):
    first = stage(backup, target)
    source = tmp_path / 'changed.db'
    db.ensure_schema(source)
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO system_events(timestamp,severity,event_type,message) VALUES ('t','i','test','different')")
    identity = write_package(source.read_bytes(), manifest_for(source), staged=False)
    second = stage(identity, target)
    assert second.plan.candidate_sha256 != first.plan.candidate_sha256
    assert second.plan.confirmation_token != first.plan.confirmation_token
    assert restore.confirm_restore_plan(second.operation_id, first.plan.confirmation_token,
                                        bound_target_path=target).code is restore.RestoreCode.CONFIRMATION_INVALID


def test_target_inode_change_revokes_confirmation(backup, target, tmp_path):
    result = stage(backup, target)
    original = target.read_bytes()
    target.rename(tmp_path / 'old-active.db')
    target.write_bytes(original)
    checked = restore.confirm_restore_plan(result.operation_id, result.plan.confirmation_token,
                                           bound_target_path=target)
    assert checked.code is restore.RestoreCode.TARGET_CHANGED and checked.plan is None


def test_current_runtime_binding_must_match_preview(backup, target, tmp_path):
    result = stage(backup, target)
    other = tmp_path / 'other.db'
    db.ensure_schema(other)
    assert restore.confirm_restore_plan(result.operation_id, result.plan.confirmation_token,
                                        bound_target_path=other).code is restore.RestoreCode.TARGET_CHANGED


@pytest.mark.parametrize('kind', ['json', 'field', 'duplicate', 'key'])
def test_record_tampering_or_corruption_fails_closed(backup, target, kind):
    result = stage(backup, target)
    path = directory(result.operation_id) / 'operation.json'
    if kind == 'json':
        path.write_bytes(b'{')
    elif kind == 'field':
        data = json.loads(path.read_bytes())
        data['record']['target']['path'] = '/arbitrary/server/file'
        path.write_text(json.dumps(data))
    elif kind == 'duplicate':
        path.write_bytes(b'{"record":{},"record":{},"mac":"x"}')
    else:
        (wamf_paths.get_database_restore_root() / '.record.key').write_bytes(b'changed key' * 3)
    with pytest.raises(restore.RestoreError):
        restore.get_restore_plan(result.operation_id)
    assert not restore.revalidate_restore_plan(result.operation_id).success
    assert not restore.cancel_restore(result.operation_id).success


def test_cancellation_preserves_backup_and_revokes_before_cleanup(backup, target):
    result = stage(backup, target)
    assert restore.confirm_restore_plan(result.operation_id, result.plan.confirmation_token,
                                        bound_target_path=target).success
    cancelled = restore.cancel_restore(result.operation_id)
    assert cancelled.success and cancelled.phase is restore.RestorePhase.CANCELLED
    operation = directory(result.operation_id)
    assert not (operation / 'candidate.db').exists() and not (operation / 'package.zip').exists()
    assert record(result.operation_id)['plan'] is None
    assert (wamf_paths.get_database_backup_root() / backup.filename).exists()
    assert restore.cancel_restore(result.operation_id).success


def test_future_application_owned_phase_cannot_be_cancelled(backup, target, monkeypatch):
    result = stage(backup, target)
    with storage.operation_store(result.operation_id) as store:
        data = store.read()
        data['phase'] = 'applying'
        store.write(data)
    future_phases = {phase.name: phase.value for phase in restore.RestorePhase}
    future_phases['APPLYING'] = 'applying'
    monkeypatch.setattr(restore, 'RestorePhase', StrEnum('FutureRestorePhase', future_phases))
    assert restore.cancel_restore(result.operation_id).code is restore.RestoreCode.INVALID_STATE
    assert (directory(result.operation_id) / 'candidate.db').exists()


@pytest.mark.parametrize('name', ['../escape', '/tmp/arbitrary', 'restore-nope', None])
def test_unsafe_operation_ids(name):
    with pytest.raises(restore.RestoreError):
        restore.get_restore_plan(name)
    assert not restore.cancel_restore(name).success


@pytest.mark.parametrize('name', ['../escape', '/tmp/arbitrary.zip', 'file.zip', None])
def test_unsafe_backup_ids_rejected_before_staging(name, target):
    result = restore.stage_restore_from_backup(name, target)
    assert result.code is restore.RestoreCode.INVALID_INPUT and result.operation_id is None


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory'])
@pytest.mark.parametrize('name', ['package.zip', 'candidate.db', 'operation.json'])
def test_staged_artifact_alias_and_special_file_rejection(backup, target, tmp_path, name, kind):
    result = stage(backup, target)
    path = directory(result.operation_id) / name
    outside = tmp_path / 'outside'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    if kind == 'symlink':
        path.symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, path)
    elif kind == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        path.mkdir()
    before = outside.read_bytes()
    assert not restore.revalidate_restore_plan(result.operation_id).success
    assert outside.read_bytes() == before


def test_symlinked_restore_root_rejected(backup, target, tmp_path):
    root = wamf_paths.get_database_restore_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    assert not stage(backup, target).success
    assert list(outside.iterdir()) == []


def test_two_simultaneous_staging_operations(backup, target):
    results = []
    gate = threading.Barrier(2)

    def run():
        gate.wait(timeout=5)
        results.append(stage(backup, target))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert len(results) == 2 and all(result.success for result in results)
    assert results[0].operation_id != results[1].operation_id


def test_upload_size_and_cooperative_read_deadline(backup, target, monkeypatch):
    monkeypatch.setattr(package, 'MAX_PACKAGE_BYTES', 32)
    oversized = restore.stage_restore_from_stream(io.BytesIO(b'x' * 33), target)
    assert oversized.code is restore.RestoreCode.INPUT_TOO_LARGE
    assert not (directory(oversized.operation_id) / 'package.zip').exists()

    class SlowStream:
        def read(self, count):
            time.sleep(0.05)
            return b'x'

    expired = restore.stage_restore_from_stream(SlowStream(), target, timeout_seconds=0.02)
    assert expired.code is restore.RestoreCode.DEADLINE


def test_worker_cancellation_is_reaped_and_durable(backup, target, monkeypatch):
    original = subprocess.Popen
    children = []

    def start(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(restore.subprocess, 'Popen', start)
    result = stage(backup, target, cancelled=lambda: bool(children))
    assert result.code is restore.RestoreCode.CANCELLED
    assert result.phase is restore.RestorePhase.CANCELLED
    assert record(result.operation_id)['phase'] == 'cancelled'
    assert all(child.poll() is not None for child in children)
    assert not (directory(result.operation_id) / 'candidate.db').exists()


def test_ready_plan_survives_fresh_interpreter(backup, target, tmp_path):
    result = stage(backup, target)
    script = '''
import json, sys
from pathlib import Path
from dataclasses import asdict
import wamf_paths
from app.database_restore import get_restore_plan
wamf_paths.REPO_ROOT = Path(sys.argv[1])
print(json.dumps(asdict(get_restore_plan(sys.argv[2]))))
'''
    process = subprocess.run([sys.executable, '-B', '-c', script, str(tmp_path), result.operation_id],
                             cwd=Path(restore.__file__).resolve().parent.parent,
                             capture_output=True, text=True, check=True, timeout=5)
    assert json.loads(process.stdout) == asdict(result.plan)


def test_staging_never_opens_target_contents(backup, target, monkeypatch):
    original_open = Path.open
    original_sqlite = sqlite3.connect

    def opened(path, *args, **kwargs):
        assert path != target
        return original_open(path, *args, **kwargs)

    def connected(path, *args, **kwargs):
        assert str(target) not in str(path)
        return original_sqlite(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', opened)
        patch.setattr(sqlite3, 'connect', connected)
        result = stage(backup, target)
        assert result.success
        assert restore.confirm_restore_plan(result.operation_id, result.plan.confirmation_token,
                                            bound_target_path=target).success
        assert restore.cancel_restore(result.operation_id).success


def test_worker_deadline_kills_and_reaps_child(backup, target, monkeypatch):
    original = subprocess.Popen
    children = []

    def slow(*args, **kwargs):
        process = original([sys.executable, '-B', '-c', 'import time; time.sleep(20)'], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(restore.subprocess, 'Popen', slow)
    result = stage(backup, target, timeout_seconds=0.2)
    assert result.code is restore.RestoreCode.DEADLINE
    assert children and all(child.poll() is not None for child in children)
    assert record(result.operation_id)['phase'] == 'failed'
    assert not (directory(result.operation_id) / 'candidate.db').exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory', 'parent_symlink'])
def test_unsafe_target_identity_rejected_without_reading(backup, target, tmp_path, kind):
    alias = tmp_path / 'alias.db'
    if kind == 'symlink':
        alias.symlink_to(target)
    elif kind == 'hardlink':
        os.link(target, alias)
    elif kind == 'fifo':
        os.mkfifo(alias)
    elif kind == 'directory':
        alias.mkdir()
    else:
        parent = tmp_path / 'alias'
        parent.symlink_to(tmp_path, target_is_directory=True)
        alias = parent / target.name
    assert not stage(backup, alias).success


def test_symlinked_operation_directory_is_not_followed(backup, target, tmp_path):
    result = stage(backup, target)
    path = directory(result.operation_id)
    outside = tmp_path / 'moved'
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=True)
    assert not restore.revalidate_restore_plan(result.operation_id).success
    assert not restore.cancel_restore(result.operation_id).success
    assert (outside / 'candidate.db').exists()


def test_incomplete_staging_record_is_not_a_preview(target):
    with storage.operation_store(create=True) as store:
        identifier = store.identifier
        store.write({'record_version': 1, 'operation_id': identifier, 'phase': 'staging'})
    with pytest.raises(restore.RestoreError):
        restore.get_restore_plan(identifier)
    assert not restore.revalidate_restore_plan(identifier).success


def test_plan_build_changes_invalidate_confirmation(backup, target, monkeypatch):
    result = stage(backup, target)
    original = restore._make_plan

    def changed(*args):
        return replace(original(*args), latest_sequence=7, latest_migration_id='007_future')

    monkeypatch.setattr(restore, '_make_plan', changed)
    checked = restore.revalidate_restore_plan(result.operation_id)
    assert checked.code is restore.RestoreCode.ARTIFACT_CHANGED and checked.plan is None


@pytest.mark.parametrize('flag', ['automatic_upgrade_supported', 'legacy_recognized'])
def test_legacy_without_upgrade_eligibility_is_rejected(target, tmp_path, monkeypatch, flag):
    source = tmp_path / 'legacy.db'
    legacy_database(source)
    identity = write_package(source.read_bytes(), manifest_for(source), staged=False)
    original = restore._validate

    def unsupported(*args):
        validated = original(*args)
        return replace(validated, inspection=replace(validated.inspection, **{flag: False}))

    monkeypatch.setattr(restore, '_validate', unsupported)
    result = stage(identity, target)
    assert result.code is restore.RestoreCode.PACKAGE_REJECTED
    assert result.phase is restore.RestorePhase.REJECTED


def test_missing_confirmation_token_is_rejected(backup, target):
    result = stage(backup, target)
    assert restore.confirm_restore_plan(result.operation_id, None, bound_target_path=target).code is restore.RestoreCode.CONFIRMATION_INVALID


@pytest.mark.parametrize('name', ['candidate.db', 'package.zip'])
def test_byte_identical_inode_replacement_invalidates_plan(backup, target, name):
    result = stage(backup, target)
    path = directory(result.operation_id) / name
    payload = path.read_bytes()
    path.rename(path.with_name('previous-' + name))
    path.write_bytes(payload)
    path.chmod(0o600)
    assert restore.revalidate_restore_plan(result.operation_id).code is restore.RestoreCode.ARTIFACT_CHANGED


def test_change_during_revalidation_is_not_confirmed(backup, target, monkeypatch):
    result = stage(backup, target)
    original = restore._validate

    def tamper(*args):
        validation = original(*args)
        with (directory(result.operation_id) / 'candidate.db').open('ab') as file:
            file.write(b'changed during worker validation')
        return validation

    monkeypatch.setattr(restore, '_validate', tamper)
    confirmed = restore.confirm_restore_plan(result.operation_id, result.plan.confirmation_token,
                                             bound_target_path=target)
    assert confirmed.code is restore.RestoreCode.ARTIFACT_CHANGED
    assert record(result.operation_id)['confirmed_at'] is None


def test_cancel_cleans_operation_owned_abandoned_scratch(backup, target):
    result = stage(backup, target)
    scratch = directory(result.operation_id) / '.recheck'
    scratch.mkdir(mode=0o700)
    (scratch / 'database.db').write_bytes(b'partial extraction')
    assert restore.cancel_restore(result.operation_id).success
    assert not scratch.exists()


def test_restore_identifier_collision_does_not_overwrite(backup, target, monkeypatch):
    result = stage(backup, target)
    saved = (directory(result.operation_id) / 'operation.json').read_bytes()
    monkeypatch.setattr(storage.secrets, 'token_hex', lambda count: result.operation_id.removeprefix('restore-'))
    collided = stage(backup, target)
    assert not collided.success
    assert (directory(result.operation_id) / 'operation.json').read_bytes() == saved


def test_cancel_active_operation_is_busy(backup, target):
    result = stage(backup, target)
    with storage.operation_store(result.operation_id):
        assert restore.cancel_restore(result.operation_id).code is restore.RestoreCode.BUSY
    assert restore.get_restore_plan(result.operation_id) == result.plan
