"""Setup UI, startup selection, and configuration activation regressions."""
import os
import re
import shutil
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app import bootstrap, health


@pytest.fixture(autouse=True)
def reset_worker_state(monkeypatch):
    monkeypatch.setattr(health, '_detection_worker_enabled', None)


def configured():
    return {
        'frigate': {'frigate_url': 'http://frigate', 'mqtt_server': 'mqtt',
                    'main_topic': 'frigate', 'camera': ['birdcam']},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
    }


@pytest.mark.parametrize('config,targets', [({}, ['run_webui']), (configured(), ['run_webui', 'run_mqtt_client'])])
def test_startup_selects_only_required_workers(config, targets, monkeypatch, caplog):
    import speciesid
    monkeypatch.setattr(speciesid, 'config', config)
    child = MagicMock()
    child.is_alive.return_value = False
    with patch('speciesid.load_config'), patch('speciesid.setupdb'), patch('speciesid.log_system_event'), patch('speciesid.multiprocessing.Process', return_value=child) as process:
        speciesid.main()
    assert [call.kwargs['target'].__name__ for call in process.call_args_list] == targets
    assert child.start.call_count == len(targets)
    if len(targets) == 1:
        assert 'setup/configuration required' in caplog.text
        assert 'MQTT/classification worker is disabled' in caplog.text


@pytest.mark.parametrize('config,expected', [({}, 'setup_required'), (configured(), 'degraded')])
def test_health_distinguishes_setup_from_real_outages(config, expected):
    with patch('app.health.requests.get', side_effect=health.requests.ConnectionError) as http, patch('app.health.mqtt.Client') as mqtt, patch('app.health.connect_db'), patch('app.health.shutil.disk_usage', return_value=(100, 10, 90)), patch('app.health.get_snapshots_path'), patch('app.health.get_clips_path'):
        mqtt.return_value.connect.side_effect = OSError('offline')
        result = health.calculate_system_health(config)
    assert result['overall_state'] == expected
    assert result['setup_required'] is (expected == 'setup_required')
    if expected == 'setup_required':
        http.assert_not_called()
        mqtt.assert_not_called()
        assert result['frigate_online'] is None
        assert result['mqtt_online'] is None
        assert 'offline' not in health._health_details(result).lower()
    else:
        http.assert_called_once()
        mqtt.return_value.connect.assert_called_once_with('mqtt', 1883, 5)
        assert result['frigate_online'] is False
        assert result['mqtt_online'] is False


def test_saved_configuration_requires_restart_of_setup_process():
    health.set_detection_worker_enabled(False)
    with patch('app.health.requests.get') as http, patch('app.health.mqtt.Client') as mqtt, patch('app.health.connect_db'), patch('app.health.shutil.disk_usage', return_value=(100, 10, 90)), patch('app.health.get_snapshots_path'), patch('app.health.get_clips_path'):
        result = health.calculate_system_health(configured())
    assert result['configuration_issues'] == []
    assert result['restart_required'] is True
    assert result['overall_state'] == 'setup_required'
    http.assert_not_called()
    mqtt.assert_not_called()


@pytest.mark.parametrize('section,value', [('webui', {'port': 'invalid'}), ('webui', {'host': '<bind-address>'}), ('storage', []), ('frigate', 'invalid')])
def test_unsafe_configuration_still_fails_fast(section, value):
    config = configured()
    config[section] = value
    with pytest.raises(ValueError, match=section):
        bootstrap.preflight(config)


# Run the real native entry point and real Flask routes in an isolated checkout.
# Only ML dependencies, socket serving, and the process-stopping restart hook are
# replaced. Flask still starts in its real child process; no broker is contacted.
CLI_RUNNER = r'''
import builtins
import os
import runpy
import sys
from unittest.mock import MagicMock, patch

for name in ('PIL', 'PIL.Image', 'PIL.ImageOps'):
    sys.modules[name] = MagicMock()
from flask import Flask
passwords = []
original_print = builtins.print

def capture_password(*args, **kwargs):
    if args and str(args[0]).startswith('WAMF temporary admin password: '):
        passwords.append(str(args[0]).splitlines()[0].split(': ', 1)[1])
    original_print(*args, **kwargs)
builtins.print = capture_password

def serve_admin(self, **kwargs):
    assert kwargs['port'] == 7767
    import webui
    from app import bootstrap, health
    from app.config_editor import get_config_path
    import yaml
    from pathlib import Path
    # This flag is set by the real run_webui before Flask.run.
    assert health._detection_worker_enabled is False
    password = passwords[0] if passwords else os.environ['TEST_ADMIN_PASSWORD']
    self.config['TESTING'] = True
    with self.test_client() as client:
        assert client.get('/login').status_code == 200
        response = client.post('/login', data={'password': password})
        assert response.status_code == 302
        with client.session_transaction() as session:
            assert session['admin_authenticated']
            csrf = session['csrf_token']
        for route in ('/admin', '/admin/config'):
            response = client.get(route)
            assert response.status_code == 200, response.data
            assert b'Setup / configuration required' in response.data
            assert b'Offline' not in response.data
        status = health.get_system_health()
        assert status['overall_state'] == 'setup_required'
        assert status['frigate_online'] is None
        assert status['mqtt_online'] is None
        config = yaml.safe_load(Path(get_config_path()).read_text())
        if os.environ.get('TEST_APPLY_CONFIG'):
            original_admin = dict(config['admin'])
            config['frigate'].update(frigate_url='http://frigate', mqtt_server='mqtt', camera=['birdcam'])
            config.pop('admin')
            content = yaml.safe_dump(config)
            response = client.post('/admin/config/save', json={'config_content': content}, headers={'X-CSRFToken': csrf})
            assert response.json['success']
            status = health.get_system_health()
            assert status['restart_required'] is True
            assert b'Configuration saved. Restart WAMF' in client.get('/admin/config').data
            with patch('routes.admin.schedule_restart') as restart:
                response = client.post('/admin/config/save-and-restart', json={'config_content': content}, headers={'X-CSRFToken': csrf})
                assert response.json['success']
                restart.assert_called_once()
            saved = yaml.safe_load(Path(get_config_path()).read_text())
            assert saved['admin'] == original_admin
            assert bootstrap.preflight(saved) == []
    original_print('SETUP_UI_VERIFIED', flush=True)
Flask.run = serve_admin
if os.environ.get('TEST_NORMAL_START'):
    with patch('multiprocessing.Process') as process:
        process.return_value.is_alive.return_value = False
        runpy.run_path('speciesid.py', run_name='__main__')
        assert [call.kwargs['target'].__name__ for call in process.call_args_list] == ['run_webui', 'run_mqtt_client']
        assert process.return_value.start.call_count == 2
    original_print('NORMAL_WORKERS_VERIFIED', flush=True)
else:
    runpy.run_path('speciesid.py', run_name='__main__')
'''


def test_fresh_native_start_serves_admin_then_accepts_configuration(tmp_path):
    root = tmp_path / 'fresh-clone'
    root.mkdir()
    for directory in ('app', 'routes', 'templates', 'static', 'integrations'):
        shutil.copytree(bootstrap.REPO_ROOT / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
    (root / 'config').mkdir()
    for name in ('speciesid.py', 'webui.py', 'wamf_paths.py', 'version.py', 'birdnames.db', 'config/config.yml.example'):
        shutil.copy2(bootstrap.REPO_ROOT / name, root / name)
    env = {key: value for key, value in os.environ.items() if key not in ('WHOSATMYFEEDER_CONFIG', 'WAMF_SECRET_KEY', 'PYTHONPATH')}
    command = [sys.executable, '-c', CLI_RUNNER]
    first = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stderr
    assert 'SETUP_UI_VERIFIED' in first.stdout, first.stderr
    assert 'Created ' in first.stdout
    assert first.stdout.count('WAMF temporary admin password:') == 1
    assert 'MQTT/classification worker is disabled' in first.stderr
    assert (root / 'data/speciesid.db').is_file()
    password = re.search(r'temporary admin password: (\S+)', first.stdout).group(1)
    env.update(TEST_ADMIN_PASSWORD=password, TEST_APPLY_CONFIG='1')
    second = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stderr
    assert 'SETUP_UI_VERIFIED' in second.stdout, second.stderr
    assert 'temporary admin password:' not in second.stdout
    assert bootstrap.preflight(yaml.safe_load((root / 'config/config.yml').read_text())) == []

    env['TEST_NORMAL_START'] = '1'
    third = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert third.returncode == 0, third.stderr
    assert 'NORMAL_WORKERS_VERIFIED' in third.stdout
    assert 'temporary admin password:' not in third.stdout
    assert 'MQTT/classification worker is disabled' not in third.stderr
