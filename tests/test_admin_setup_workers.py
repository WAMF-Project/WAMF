"""Native supervisor and Flask activation while administrator setup is pending."""

import importlib
import sys
from unittest.mock import MagicMock, Mock

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def native(flask_client, monkeypatch):
    for name in ('numpy', 'tflite_support', 'tflite_support.task', 'PIL', 'PIL.Image'):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    return importlib.import_module('speciesid')


def configuration(pending=True):
    return {
        'admin': {'auth_enabled': True, 'session_secret': 'synthetic-session',
                  'password_hash': '' if pending else generate_password_hash('synthetic-password'),
                  **({'setup_pending': True} if pending else {})},
        'frigate': {'frigate_url': 'http://frigate', 'mqtt_server': 'mqtt',
                    'main_topic': 'frigate', 'camera': ['birdcam']},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
    }


@pytest.mark.parametrize('outcome', ['success', 'pending', 'expired', 'missing-marker'])
def test_supervisor_starts_processing_only_after_authoritative_completion(native, monkeypatch, outcome):
    initial = configuration()
    final = configuration(False) if outcome == 'success' else configuration()
    if outcome == 'expired':
        final['admin']['setup'] = {'expired': True}
    if outcome == 'missing-marker':
        final['admin'].pop('setup_pending')
    monkeypatch.setattr(native, 'config', initial)
    loads = 0
    def load():
        nonlocal loads
        loads += 1
        if loads > 1:
            native.config = final
    monkeypatch.setattr(native, 'load_config', load)
    monkeypatch.setattr(native, 'setupdb', Mock())
    events = Mock()
    monkeypatch.setattr(native, 'log_system_event', events)
    web = Mock(pid=123)
    # Pending loop, completed-state check, then one ordinary supervision pass.
    web.is_alive.side_effect = [True, True, True, False, False, False]
    mqtt = Mock(pid=124)
    mqtt.is_alive.return_value = True
    factory = Mock(side_effect=[web, mqtt])
    monkeypatch.setattr(native.multiprocessing, 'Process', factory)
    native.main()
    targets = [call.kwargs['target'] for call in factory.call_args_list]
    assert targets == ([native.run_webui, native.run_mqtt_client] if outcome == 'success' else [native.run_webui])
    assert events.call_count == (1 if outcome == 'success' else 0)
    assert web.start.call_count == 1
    assert mqtt.start.call_count == (1 if outcome == 'success' else 0)


def test_web_process_starts_health_once_after_setup(native, monkeypatch):
    import webui
    pending = configuration()
    monkeypatch.setattr(native, 'config', pending)
    monkeypatch.setattr(webui, 'config', pending)
    monkeypatch.setattr(webui, 'start_runtime_services', webui.start_runtime_services)
    monkeypatch.setattr(native, 'configure_worker_signals', Mock())
    health = Mock()
    monkeypatch.setattr(native, 'start_health_monitor', health)
    detection = Mock()
    monkeypatch.setattr(native, 'set_detection_worker_enabled', detection)
    def serve(**kwargs):
        health.assert_not_called()
        webui.start_runtime_services()  # A failed/pending setup cannot activate.
        health.assert_not_called()
        webui.config = configuration(False)
        webui.start_runtime_services()
        webui.start_runtime_services()
    monkeypatch.setattr(native.app, 'run', serve)
    native.run_webui()
    health.assert_called_once_with()
    assert [call.args for call in detection.call_args_list] == [(False,), (True,)]


def test_existing_installation_keeps_web_startup_sequence(native, monkeypatch):
    import webui
    config = configuration(False)
    monkeypatch.setattr(native, 'config', config)
    monkeypatch.setattr(webui, 'start_runtime_services', webui.start_runtime_services)
    monkeypatch.setattr(native, 'configure_worker_signals', Mock())
    health = Mock()
    monkeypatch.setattr(native, 'start_health_monitor', health)
    monkeypatch.setattr(native.app, 'run', Mock())
    native.run_webui()
    webui.start_runtime_services()
    health.assert_called_once_with()
