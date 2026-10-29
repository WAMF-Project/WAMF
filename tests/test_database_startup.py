"""Database startup ownership, with no deployment database or live services."""
import importlib
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from app import db

ROOT = Path(__file__).resolve().parents[1]


def runtime_config(tmp_path):
    return {
        'config_version': 2,
        'frigate': {'frigate_url': 'http://fixture', 'camera': ['garden']},
        'mqtt': {'host': 'fixture', 'topic_prefix': 'frigate'},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
        'admin': {'auth_enabled': False},
        'storage': {'database_path': str(tmp_path / 'database' / 'wamf.db'),
                    'models_path': str(tmp_path / 'models')},
        'media': {'snapshots_path': str(tmp_path / 'snapshots'),
                  'clips_path': str(tmp_path / 'clips')},
        'retention': {'schedule': {'enabled': True, 'time': '03:00', 'timezone': 'UTC'}},
    }


@pytest.mark.parametrize('module_name', ['webui', 'speciesid'])
def test_import_is_inert_even_with_missing_configuration(tmp_path, module_name):
    code = '''
import importlib
from pathlib import Path
from unittest.mock import patch
with patch('app.config_loader.load_runtime_config', side_effect=AssertionError('config read')), \\
     patch('app.db.ensure_schema', side_effect=AssertionError('migration')), \\
     patch('wamf_paths.ensure_storage_paths', side_effect=AssertionError('storage')), \\
     patch('sqlite3.connect', side_effect=AssertionError('database opened')), \\
     patch('threading.Thread.start', side_effect=AssertionError('thread started')), \\
     patch.object(Path, 'mkdir', side_effect=AssertionError('directory created')):
    module = importlib.import_module(MODULE_NAME)
    importlib.reload(module)
'''.replace('MODULE_NAME', repr(module_name))
    env = dict(os.environ, WHOSATMYFEEDER_CONFIG=str(tmp_path / 'missing/config.yml'))
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'missing').exists()


def supervisor_harness(monkeypatch, candidate):
    import speciesid

    monkeypatch.setattr(speciesid, 'load_runtime_config', MagicMock(return_value=candidate))
    diagnostics = MagicMock()
    monkeypatch.setattr(speciesid, 'MqttRuntimeDiagnostics', diagnostics)
    processes = []
    def process(target, args):
        child = MagicMock(pid=100 + len(processes))
        child.is_alive.return_value = False
        processes.append((target, args, child))
        return child
    factory = MagicMock(side_effect=process)
    monkeypatch.setattr(speciesid.multiprocessing, 'Process', factory)
    health = MagicMock()
    monkeypatch.setattr(speciesid, 'start_health_monitor', health)
    metadata = MagicMock()
    monkeypatch.setattr('app.metadata_tasks.start_metadata_worker', metadata)
    return speciesid, diagnostics, factory, processes, health, metadata


def test_single_parent_barrier_closes_connection_before_event_and_children(tmp_path, monkeypatch):
    candidate = runtime_config(tmp_path)
    original = deepcopy(candidate)
    speciesid, diagnostics, factory, processes, health, metadata = supervisor_harness(monkeypatch, candidate)
    path = Path(candidate['storage']['database_path']).resolve()
    connections = []
    original_connect = db.connect_db
    def connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        connections.append(conn)
        return conn
    monkeypatch.setattr(db, 'connect_db', connect)
    migrations = MagicMock(wraps=db.ensure_schema)
    monkeypatch.setattr(speciesid, 'ensure_schema', migrations)
    order = []
    def startup_event(**kwargs):
        assert kwargs['db_path'] == path
        migrations.assert_called_once_with(path)
        assert connections
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connections[0].execute('SELECT 1')
        diagnostics.assert_not_called()
        factory.assert_not_called()
        order.append('event')
    monkeypatch.setattr(speciesid, 'log_system_event', startup_event)
    diagnostics.side_effect = lambda: order.append('diagnostics') or MagicMock()
    speciesid.main()
    assert order == ['event', 'diagnostics']
    migrations.assert_called_once_with(path)
    speciesid.load_runtime_config.assert_called_once_with()
    assert [target for target, _, _ in processes] == [
        speciesid.run_webui, speciesid.run_mqtt_client, speciesid.run_scheduler_child]
    for _, args, _ in processes[:2]:
        assert args[1]['storage']['database_path'] == str(path)
        assert args[2] == path
    scheduler_args = processes[2][1]
    assert scheduler_args[0]['storage']['database_path'] == str(path)
    assert candidate == original
    health.assert_not_called()
    metadata.assert_not_called()


@pytest.mark.parametrize('failure', [
    db.DatabaseNewerError('PRIVATE newer schema'),
    db.SchemaMigrationError('PRIVATE invalid history'),
    sqlite3.OperationalError('PRIVATE locked'),
    sqlite3.DatabaseError('PRIVATE invalid database'),
    OSError('PRIVATE I/O error'),
    RuntimeError('PRIVATE unexpected migration error'),
])
def test_migration_failure_starts_nothing_and_logs_no_exception_values(
    tmp_path, monkeypatch, caplog, failure,
):
    speciesid, diagnostics, factory, _, health, metadata = supervisor_harness(
        monkeypatch, runtime_config(tmp_path))
    migrate = MagicMock(side_effect=failure)
    monkeypatch.setattr(speciesid, 'ensure_schema', migrate)
    event = MagicMock()
    monkeypatch.setattr(speciesid, 'log_system_event', event)
    bind = MagicMock()
    monkeypatch.setattr(speciesid, 'configure_worker_runtime', bind)
    storage = MagicMock()
    monkeypatch.setattr(speciesid, 'ensure_storage_paths', storage)
    with pytest.raises(SystemExit) as exc:
        speciesid.main()
    assert exc.value.code == 1
    assert 'startup refused' in caplog.text
    assert type(failure).__name__ in caplog.text
    assert 'PRIVATE' not in caplog.text
    for mock in (diagnostics, factory, health, metadata, event, bind, storage):
        mock.assert_not_called()
    assert migrate.call_count == 1


@pytest.mark.parametrize('stage', ['load', 'validate', 'path'])
def test_config_or_path_failure_never_initializes_default_database(tmp_path, monkeypatch, stage):
    speciesid, diagnostics, factory, _, _, _ = supervisor_harness(monkeypatch, runtime_config(tmp_path))
    if stage == 'load':
        speciesid.load_runtime_config.side_effect = ValueError('cannot load')
    elif stage == 'validate':
        monkeypatch.setattr(speciesid, 'preflight', MagicMock(side_effect=ValueError('invalid')))
    else:
        monkeypatch.setattr(speciesid, 'get_database_path', MagicMock(side_effect=OSError('path failed')))
    migrate = MagicMock()
    event = MagicMock()
    monkeypatch.setattr(speciesid, 'ensure_schema', migrate)
    monkeypatch.setattr(speciesid, 'log_system_event', event)
    with pytest.raises(SystemExit) as exc:
        speciesid.main()
    assert exc.value.code != 0
    for mock in (migrate, event, diagnostics, factory):
        mock.assert_not_called()


def test_consumers_remain_bound_when_saved_config_changes_or_disappears(tmp_path, monkeypatch):
    import speciesid
    import webui
    from app import health, queries, system_events

    candidate = runtime_config(tmp_path)
    candidate['retention']['schedule']['enabled'] = False
    config_path = tmp_path / 'config.yml'
    config_path.write_text(yaml.safe_dump(candidate))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(config_path))
    _, _, _, _, _, _ = supervisor_harness(monkeypatch, candidate)
    speciesid.main()
    path = Path(candidate['storage']['database_path'])
    config_path.write_text('storage: {database_path: /not/the/initialized.db}')
    for target in (speciesid.DBPATH, webui.DBPATH, queries.DBPATH, health.DB_PATH, system_events.DB_PATH):
        assert target == path
        with db.connect_db(target) as conn:
            assert conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 7
    config_path.unlink()
    # These APIs previously resolved paths dynamically; production now pins them.
    assert queries.get_detection_count() == 0
    assert system_events.log_system_event('INFO', 'TEST', 'same database')
    assert not Path('/not/the/initialized.db').exists()


def test_standalone_flask_explicit_initialization(tmp_path, monkeypatch):
    import webui

    candidate = runtime_config(tmp_path)
    monkeypatch.setattr(webui, 'load_runtime_config', MagicMock(return_value=candidate))
    migrations = MagicMock(wraps=db.ensure_schema)
    monkeypatch.setattr(webui, 'ensure_schema', migrations)
    assert webui.initialize_app() is webui.app
    path = Path(candidate['storage']['database_path'])
    migrations.assert_called_once_with(path)
    assert webui.DBPATH == path
    with webui.app.test_client() as client:
        assert client.get('/login').status_code == 302  # Authentication disabled.


def _spawn_import_check(send):
    try:
        from unittest.mock import patch
        with patch('app.db.ensure_schema', side_effect=AssertionError('child migration')):
            for name in ('webui', 'speciesid'):
                importlib.reload(importlib.import_module(name))
        send.send('ok')
    finally:
        send.close()


def _web_worker_check(candidate, path, send):
    try:
        from unittest.mock import patch

        import speciesid
        import webui
        from app import health, queries, system_events

        def monitor(**kwargs):
            assert speciesid.DBPATH == path
            assert webui.DBPATH == queries.DBPATH == health.DB_PATH == system_events.DB_PATH == path
            with db.connect_db(queries.DBPATH) as conn:
                assert conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 7
            send.send('health')
        def serve(**kwargs):
            send.send('flask')
        with patch('speciesid.ensure_schema', side_effect=AssertionError('child migration')), \
             patch('webui.ensure_schema', side_effect=AssertionError('child migration')), \
             patch('speciesid.load_runtime_config', side_effect=AssertionError('child reload')), \
             patch('webui.load_runtime_config', side_effect=AssertionError('child reload')), \
             patch('speciesid.start_health_monitor', side_effect=monitor), \
             patch.object(webui.app, 'run', side_effect=serve):
            speciesid.run_webui(None, candidate, path)
    finally:
        send.close()


def _mqtt_worker_check(candidate, path, send):
    try:
        from unittest.mock import patch

        import speciesid
        from app import queries, system_events

        def classifier(writer):
            assert speciesid.DBPATH == queries.DBPATH == system_events.DB_PATH == path
            send.send('classifier')
        client = MagicMock()
        client.loop_forever.side_effect = lambda: send.send('mqtt')
        with patch('speciesid.ensure_schema', side_effect=AssertionError('child migration')), \
             patch('speciesid.load_runtime_config', side_effect=AssertionError('child reload')), \
             patch('speciesid.initialize_worker_classifier', side_effect=classifier), \
             patch('app.frigate_client.FrigateClient._review_request', side_effect=RuntimeError('network disabled')), \
             patch('speciesid.mqtt.Client', return_value=client):
            speciesid.run_mqtt_client(None, candidate, path)
    finally:
        send.close()


@pytest.mark.parametrize('method', ['fork', 'spawn'])
@pytest.mark.parametrize('worker', ['import', 'flask', 'mqtt'])
def test_fork_and_spawn_do_not_migrate_or_reload(tmp_path, monkeypatch, method, worker):
    context = multiprocessing.get_context(method)
    read, send = context.Pipe(duplex=False)
    if worker == 'import':
        monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(tmp_path / 'missing.yml'))
        target, args, expected = _spawn_import_check, (send,), ['ok']
    else:
        candidate = runtime_config(tmp_path)
        path = Path(candidate['storage']['database_path'])
        db.ensure_schema(path)
        monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(tmp_path / 'missing.yml'))
        target = _web_worker_check if worker == 'flask' else _mqtt_worker_check
        expected = ['health', 'flask'] if worker == 'flask' else ['classifier', 'mqtt']
        args = (candidate, path, send)
    process = context.Process(target=target, args=args)
    process.start()
    send.close()
    try:
        for message in expected:
            assert read.poll(15)
            assert read.recv() == message
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        read.close()


def test_snapshot_survives_configuration_disappearing_before_migration(tmp_path, monkeypatch):
    import wamf_paths
    from app.config_loader import load_runtime_config

    candidate = runtime_config(tmp_path)
    config_path = tmp_path / 'config.yml'
    config_path.write_text(yaml.safe_dump(candidate))
    (tmp_path / 'secrets.yml').write_text('secrets_version: 1\n')
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(config_path))
    speciesid, _, _, _, _, _ = supervisor_harness(monkeypatch, candidate)
    def load_then_remove():
        loaded = load_runtime_config()
        config_path.unlink()
        return loaded
    monkeypatch.setattr(speciesid, 'load_runtime_config', load_then_remove)
    monkeypatch.setattr(wamf_paths, '_load_config', MagicMock(side_effect=AssertionError('path reload')))
    speciesid.main()
    assert not config_path.exists()
    assert Path(candidate['storage']['database_path']).is_file()
    wamf_paths._load_config.assert_not_called()


def test_cli_migration_error_exits_nonzero_without_sensitive_traceback(tmp_path):
    candidate = runtime_config(tmp_path)
    code = '''
from unittest.mock import patch
import speciesid
with patch('speciesid.load_runtime_config', return_value=CANDIDATE), \\
     patch('speciesid.ensure_schema', side_effect=RuntimeError('PRIVATE SQL parameter')), \\
     patch('speciesid.MqttRuntimeDiagnostics', side_effect=AssertionError('child created')):
    speciesid.main()
'''.replace('CANDIDATE', repr(candidate))
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 1
    assert 'startup refused' in result.stderr
    assert 'RuntimeError' in result.stderr
    assert 'PRIVATE' not in result.stderr + result.stdout
    assert 'Traceback' not in result.stderr
    assert not Path(candidate['storage']['database_path']).exists()
