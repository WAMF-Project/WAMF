"""Authenticated database workflow; all DBs, packages and restart owners are fake/disposable."""

import io
import sqlite3
import threading
import time
from unittest.mock import Mock

import pytest
import yaml

import wamf_paths
from app import database_restore as staging
from app import database_restore_application as application
from app import database_restore_coordination as coordination
from app import db
from app.database_admin import DatabaseJobs
from app.database_backup_storage import reserve_backup
from app.database_lifecycle import exclusive_database_lifecycle, maintenance_access
from app.database_restore_storage import RestoreStorageError
from tests.test_database_backup import manifest_for
from tests.test_database_backup import package as write_package
from tests.test_database_startup import runtime_config
from tests.test_db_migrations import add_history, current_prebaseline

BASE = '/admin/database'
HEADERS = {'X-CSRF-Token': 'database-csrf'}


@pytest.fixture
def admin_db(flask_client, tmp_path, monkeypatch):
    import webui
    from routes import database_admin

    monkeypatch.setattr(wamf_paths, 'REPO_ROOT', tmp_path)
    target = tmp_path / 'active.db'
    db.ensure_schema(target)
    config = runtime_config(tmp_path)
    config['storage']['database_path'] = str(target)
    config['admin'] = {'auth_enabled': True, 'password_hash': 'unused-test-hash', 'session_secret': 'database-session'}
    saved = tmp_path / 'config.yml'
    saved.write_text(yaml.safe_dump(config))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(saved))
    monkeypatch.setattr(webui, 'config', config)
    monkeypatch.setattr(webui, 'DBPATH', target)
    monkeypatch.setattr(webui.app, 'secret_key', 'database-session')
    manager = DatabaseJobs()
    monkeypatch.setitem(webui.app.extensions, 'database_jobs', manager)
    restart = Mock()
    monkeypatch.setattr(database_admin, 'native_restart_available', lambda: True)
    monkeypatch.setattr(database_admin, 'schedule_restart', restart)
    with flask_client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'database-csrf'
    yield flask_client, target, restart, manager
    snapshot = manager.snapshot()
    if snapshot and not snapshot['done']:
        manager.cancel(snapshot['job_id'])
        deadline = time.monotonic() + 10
        while manager.active is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert manager.active is None


@pytest.fixture
def managed(admin_db, tmp_path):
    source = tmp_path / 'input.db'
    db.ensure_schema(source)
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO system_events(timestamp,severity,event_type,message) VALUES ('t','i','TEST','restored-row')")
    return write_package(source.read_bytes(), manifest_for(source), staged=False)


def wait_job(client, identifier):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f'{BASE}/jobs/{identifier}')
        assert response.status_code == 200
        if response.json['done']:
            return response.json
        time.sleep(0.02)
    pytest.fail('Database job did not finish')


def stage(client, identity):
    response = client.post(BASE + '/restore/stage', json={'backup_id': identity.backup_id}, headers=HEADERS)
    assert response.status_code == 202
    return wait_job(client, response.json['job_id'])['result']


def test_auth_disabled_blocks_every_artifact_action(admin_db, managed, monkeypatch):
    import webui

    client, _, _, _ = admin_db
    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    paths = ['/status', '/backups', f'/backups/{managed.backup_id}/download', '/restore/upload',
             '/restore/stage', '/operations/restore-123/restart', '/retention-hold/acknowledge']
    for path in paths:
        response = client.get(BASE + path) if path in paths[:3] else client.post(BASE + path, headers=HEADERS)
        assert response.status_code == 403 and response.json['code'] == 'admin_auth_required'
    html = client.get('/admin').get_data(as_text=True)
    assert 'Database tools are disabled' in html and 'id="database-create"' not in html


def test_session_auth_and_csrf_are_required(admin_db, managed):
    client, _, _, _ = admin_db
    with client.session_transaction() as session:
        session['admin_authenticated'] = False
    for path in ('/status', '/backups', f'/backups/{managed.backup_id}/download'):
        assert client.get(BASE + path, headers={'X-WAMF-API-Key': 'not-a-session'}).status_code == 401
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
    for path in ('/backups', '/restore/upload', '/restore/stage', '/retention-hold/acknowledge',
                 '/operations/restore-123/restart', '/operations/restore-123/cancel', '/jobs/job/cancel'):
        assert client.post(BASE + path, json={}).status_code == 400


def test_backup_creation_progress_listing_and_pinned_download(admin_db):
    client, _, _, _ = admin_db
    response = client.post(BASE + '/backups', json={}, headers=HEADERS)
    assert response.status_code == 202
    job = wait_job(client, response.json['job_id'])
    assert job['result']['success'] and job['phase'] == 'complete'
    listed = client.get(BASE + '/backups')
    assert listed.status_code == 200 and len(listed.json['backups']) == 1
    entry = listed.json['backups'][0]
    assert entry['valid'] and entry['compatibility'] == 'current'
    response = client.get(f"{BASE}/backups/{entry['backup_id']}/download")
    try:
        assert response.status_code == 200 and response.data.startswith(b'PK')
        assert response.mimetype == 'application/zip'
        assert entry['backup_id'] + '.zip' in response.headers['Content-Disposition']
        assert response.headers['Cache-Control'] == 'no-store'
    finally:
        response.close()


def test_corrupt_artifact_isolated_and_refused_at_download(admin_db, managed):
    client, _, _, _ = admin_db
    with reserve_backup(staged=False) as (bad, file):
        file.write(b'bad ZIP')
    entries = client.get(BASE + '/backups').json['backups']
    assert len(entries) == 2
    assert any(row['valid'] for row in entries)
    assert any(not row['valid'] for row in entries)
    assert client.get(f'{BASE}/backups/{bad.backup_id}/download').status_code == 404
    assert client.get(BASE + '/backups/not-a-managed-id/download').status_code == 404


@pytest.mark.parametrize('kind', ['current', 'upgradeable', 'newer', 'invalid'])
def test_preview_compatibility_and_package_only_upload(admin_db, tmp_path, kind):
    client, target, _, _ = admin_db
    source = tmp_path / 'input.db'
    if kind == 'upgradeable':
        current_prebaseline(source)
    else:
        db.ensure_schema(source)
    manifest = manifest_for(source)
    if kind == 'newer':
        add_history(source, ['008_future'])
    payload = source.read_bytes() if kind != 'invalid' else b'not sqlite'
    import hashlib
    from dataclasses import replace

    manifest = replace(manifest, database_size=len(payload), database_sha256=hashlib.sha256(payload).hexdigest())
    backup = write_package(payload, manifest, staged=False)
    before = target.read_bytes()
    result = stage(client, backup)
    if kind in ('current', 'upgradeable'):
        assert result['success'] and result['plan']['compatibility'] == kind
        assert result['plan']['migration_required'] == (kind == 'upgradeable')
        assert client.get(BASE + '/operations/' + result['operation_id']).json['plan'] == result['plan']
    else:
        assert not result['success'] and result['phase'] == 'rejected' and result['plan'] is None
    assert target.read_bytes() == before


def test_upload_ignores_name_limits_bytes_and_preserves_source(admin_db, managed, monkeypatch):
    import webui
    from app import database_admin

    client, target, _, _ = admin_db
    path = wamf_paths.get_database_backup_root() / managed.filename
    before = path.read_bytes(), target.read_bytes()
    response = client.post(BASE + '/restore/upload', data={'package': (io.BytesIO(before[0]), '../../evil.zip')},
                           headers=HEADERS, content_type='multipart/form-data')
    assert response.status_code == 202
    assert wait_job(client, response.json['job_id'])['result']['success']
    assert (path.read_bytes(), target.read_bytes()) == before
    monkeypatch.setattr(database_admin, 'MAX_UPLOAD_REQUEST_BYTES', 64)
    response = client.post(BASE + '/restore/upload', data=b'x' * 65, content_type='application/zip', headers=HEADERS)
    assert response.status_code == 413
    assert client.post(BASE + '/restore/stage', json={'backup_id': '/arbitrary/path'}, headers=HEADERS).status_code == 202
    assert webui.DBPATH == target


def test_confirm_queues_one_restart_no_application_before_restart(admin_db, managed):
    client, target, restart, _ = admin_db
    preview = stage(client, managed)
    identifier, plan = preview['operation_id'], preview['plan']
    before = target.stat().st_ino
    body = {'confirmation_token': plan['confirmation_token'], 'destructive_confirmation': True}
    for _ in range(2):
        response = client.post(f'{BASE}/operations/{identifier}/restart', json=body, headers=HEADERS)
        assert response.status_code == 200 and response.json['phase'] == 'pending_restart'
    assert restart.call_count == 1 and target.stat().st_ino == before
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM system_events WHERE message='restored-row'").fetchone()[0] == 0
    assert client.get(BASE + '/status').json['operation']['phase'] == 'pending_restart'
    assert client.post(BASE + '/backups', json={}, headers=HEADERS).status_code == 409
    with application.database_startup(target) as startup:
        with maintenance_access(startup.lease):
            db.ensure_schema(target)
        startup.allow_consumers()
        startup.mark_succeeded()
    assert client.get(BASE + '/status').json['operation']['phase'] == 'complete'
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM system_events WHERE message='restored-row'").fetchone()[0] == 1
    with application.database_startup(target) as startup:
        startup.allow_consumers()
        startup.mark_succeeded()
    assert coordination.read_restore_request()['status'] == 'consumed'


def test_bad_confirmation_and_standalone_owner_never_queue(admin_db, managed, monkeypatch):
    from routes import database_admin

    client, _target, restart, _ = admin_db
    preview = stage(client, managed)
    url = f"{BASE}/operations/{preview['operation_id']}/restart"
    assert client.post(url, json={'confirmation_token': '0' * 64, 'destructive_confirmation': True}, headers=HEADERS).status_code == 409
    assert coordination.read_restore_request() is None
    monkeypatch.setattr(database_admin, 'native_restart_available', lambda: False)
    assert client.post(url, json={}, headers=HEADERS).status_code == 409
    restart.assert_not_called()


def test_acknowledgement_is_bound_durable_and_does_not_change_retention_history(admin_db, managed):
    from app.retention_service import run_retention
    from tests.test_retention_service import retention_config

    client, target, _, _ = admin_db
    preview = stage(client, managed)
    identifier = preview['operation_id']
    confirmed = staging.confirm_restore_plan(identifier, preview['plan']['confirmation_token'], bound_target_path=target)
    assert confirmed.success
    with exclusive_database_lifecycle(target) as lease:
        assert application.apply_restore(lease, identifier, preview['plan']['confirmation_token']).success
    with application.database_startup(target) as startup:
        startup.allow_consumers()
        startup.mark_succeeded()
    status = client.get(BASE + '/status').json
    assert status['retention_hold'] and status['hold_operation_id'] == identifier
    with sqlite3.connect(target) as conn:
        history = conn.execute('SELECT * FROM retention_status').fetchall()
    body = {'operation_id': identifier, 'acknowledge_media_mismatch': True}
    assert client.post(BASE + '/retention-hold/acknowledge', json=body).status_code == 400
    assert client.post(BASE + '/retention-hold/acknowledge', json={**body, 'operation_id': 'restore-' + '0' * 32}, headers=HEADERS).status_code == 409
    assert client.post(BASE + '/retention-hold/acknowledge', json=body, headers=HEADERS).status_code == 200
    assert not application.retention_hold_active(target)
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT * FROM retention_status').fetchall() == history
    cfg = retention_config(target.parent)
    cfg['storage']['database_path'] = str(target)
    assert run_retention(config=cfg).outcome != 'held'


def test_claimed_request_without_apply_never_retries(admin_db, managed):
    from app.database_restore_storage import restore_request_store

    client, target, _, _ = admin_db
    preview = stage(client, managed)
    staging.confirm_restore_plan(preview['operation_id'], preview['plan']['confirmation_token'], bound_target_path=target)
    coordination.queue_restore_request(preview['operation_id'], preview['plan']['confirmation_token'], target)
    with restore_request_store() as store:
        data = store.read()
        data['status'] = 'claimed'
        store.write(data)
    before = target.read_bytes()
    with application.database_startup(target) as startup:
        startup.allow_consumers()
        startup.mark_succeeded()
    assert target.read_bytes() == before and coordination.read_restore_request()['status'] == 'failed'


def test_different_restart_target_fails_closed(admin_db, managed, tmp_path):
    client, target, _, _ = admin_db
    preview = stage(client, managed)
    staging.confirm_restore_plan(preview['operation_id'], preview['plan']['confirmation_token'], bound_target_path=target)
    coordination.queue_restore_request(preview['operation_id'], preview['plan']['confirmation_token'], target)
    other = tmp_path / 'other.db'
    db.ensure_schema(other)
    with pytest.raises(RestoreStorageError), application.database_startup(other):
        pytest.fail('A pending restore cannot target a different runtime DB')
    assert coordination.read_restore_request()['code'] == 'target_changed'


def test_native_startup_applies_before_schema_event_and_workers(admin_db, managed, tmp_path, monkeypatch):
    from tests.test_database_startup import supervisor_harness

    client, target, _, _ = admin_db
    preview = stage(client, managed)
    staging.confirm_restore_plan(preview['operation_id'], preview['plan']['confirmation_token'], bound_target_path=target)
    coordination.queue_restore_request(preview['operation_id'], preview['plan']['confirmation_token'], target)
    cfg = runtime_config(tmp_path)
    cfg['storage']['database_path'] = str(target)
    core, diagnostics, factory, _, _, _ = supervisor_harness(monkeypatch, cfg)
    order = []
    original_apply = coordination.apply_restore

    def apply(*args):
        order.append('apply')
        return original_apply(*args)

    def schema(path):
        order.append('schema')
        db.ensure_schema(path)

    monkeypatch.setattr(coordination, 'apply_restore', apply)
    monkeypatch.setattr(core, 'ensure_schema', schema)
    monkeypatch.setattr(core, 'log_system_event', lambda **kwargs: order.append('event'))
    diagnostics.side_effect = lambda: order.append('diagnostics') or Mock()
    original_factory = factory.side_effect
    factory.side_effect = lambda *args, **kwargs: order.append('worker') or original_factory(*args, **kwargs)
    core.main()
    assert order[:4] == ['apply', 'schema', 'event', 'diagnostics']
    assert order[4:] and all(item == 'worker' for item in order[4:])
    assert application.get_application_status(preview['operation_id']).phase is staging.RestorePhase.COMPLETE


def test_old_consumer_excludes_startup_application(admin_db, managed, monkeypatch):
    from app.database_lifecycle import DatabaseLifecycleError, database_consumer

    client, target, _, _ = admin_db
    preview = stage(client, managed)
    staging.confirm_restore_plan(preview['operation_id'], preview['plan']['confirmation_token'], bound_target_path=target)
    coordination.queue_restore_request(preview['operation_id'], preview['plan']['confirmation_token'], target)
    apply = Mock(wraps=coordination.apply_restore)
    monkeypatch.setattr(coordination, 'apply_restore', apply)
    with database_consumer(target), pytest.raises(DatabaseLifecycleError), application.database_startup(target):
        pytest.fail('Old workers must release their lifetime leases first')
    apply.assert_not_called()
    with application.database_startup(target) as startup:
        startup.allow_consumers()
        startup.mark_succeeded()
    assert apply.call_count == 1


def test_failed_startup_after_request_is_durable_and_never_reapplied(admin_db, managed, monkeypatch):
    client, target, _, _ = admin_db
    preview = stage(client, managed)
    staging.confirm_restore_plan(preview['operation_id'], preview['plan']['confirmation_token'], bound_target_path=target)
    coordination.queue_restore_request(preview['operation_id'], preview['plan']['confirmation_token'], target)
    apply = Mock(wraps=coordination.apply_restore)
    monkeypatch.setattr(coordination, 'apply_restore', apply)
    with pytest.raises(RuntimeError), application.database_startup(target):
        raise RuntimeError('fake normal startup validation failure')
    with pytest.raises(RestoreStorageError), application.database_startup(target):
        pytest.fail('A failed startup must not reapply or start workers')
    assert apply.call_count == 1
    status = client.get(BASE + '/status').json['operation']
    assert status['phase'] == 'recovery_required' and status['application']['rollback_artifact_id']


def test_two_requests_cannot_race(admin_db, managed):
    client, target, _, _ = admin_db
    previews = [stage(client, managed) for _ in range(2)]
    for preview in previews:
        assert staging.confirm_restore_plan(preview['operation_id'], preview['plan']['confirmation_token'], bound_target_path=target).success
    results = []
    barrier = threading.Barrier(2)

    def queue(preview):
        barrier.wait(timeout=5)
        try:
            results.append(coordination.queue_restore_request(preview['operation_id'], preview['plan']['confirmation_token'], target))
        except ValueError:
            results.append('rejected')

    threads = [threading.Thread(target=queue, args=(preview,)) for preview in previews]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert results.count(True) == 1 and results.count('rejected') == 1


def test_progress_and_cancellation_endpoint(admin_db, monkeypatch):
    from types import SimpleNamespace

    from app import database_admin
    from app.database_backup_service import BackupPhase

    client, _, _, _ = admin_db
    entered = threading.Event()

    def backup(path, root, *, progress, cancelled):
        progress(SimpleNamespace(phase=BackupPhase.BACKING_UP, percent=50))
        entered.set()
        while not cancelled():
            time.sleep(0.01)
        return SimpleNamespace(phase=BackupPhase.CANCELLED, success=False, code=BackupPhase.CANCELLED,
                               summary='Backup cancelled.', backup_id=None, final_size=None)

    monkeypatch.setattr(database_admin, 'create_backup', backup)
    response = client.post(BASE + '/backups', json={}, headers=HEADERS)
    identifier = response.json['job_id']
    assert entered.wait(5)
    assert client.get(f'{BASE}/jobs/{identifier}').json['percent'] == 50
    assert client.post(f'{BASE}/jobs/{identifier}/cancel', json={}, headers=HEADERS).json['success']
    assert wait_job(client, identifier)['phase'] == 'cancelled'
