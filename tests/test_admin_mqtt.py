"""Saved-only Admin MQTT action: no broker or production service required."""

from dataclasses import asdict
from unittest.mock import MagicMock

import pytest
import yaml
from werkzeug.security import generate_password_hash

from app.mqtt_connection import MESSAGES, MqttProbeResult


URL = '/admin/config/mqtt/test'


@pytest.fixture
def saved_mqtt(flask_client, tmp_path, monkeypatch):
    import webui
    from routes import admin

    config = {
        'config_version': 2,
        'mqtt': {'host': 'saved-broker', 'port': 2883,
                 'authentication': {'enabled': True},
                 'tls': {'enabled': True, 'insecure': True, 'ca_certs': '/saved/ca.pem'}},
        # Unrelated malformed/incomplete setup must not block the action.
        'frigate': [], 'classification': {'model': None}, 'camera': None,
    }
    config_path = tmp_path / 'config.yml'
    config_path.write_text(yaml.safe_dump(config))
    secrets_path = tmp_path / 'secrets.yml'
    secrets_path.write_text(yaml.safe_dump({
        'secrets_version': 1,
        'mqtt': {'username': 'PRIVATE-USER', 'password': 'PRIVATE-PASSWORD'},
    }))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(config_path))
    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    probe = MagicMock(return_value=MqttProbeResult(
        True, 'success', MESSAGES['success'], 12, '2026-10-18T12:00:00+00:00',
    ))
    monkeypatch.setattr(admin, 'probe_mqtt_connection', probe)
    return webui.app.test_client(), config_path, secrets_path, probe


def enable_auth(monkeypatch, client, *, authenticated):
    import webui

    monkeypatch.setattr(webui, 'config', {
        'admin': {'auth_enabled': True, 'password_hash': generate_password_hash('admin-test'),
                  'session_secret': 'test-secret'},
        'api': {'token_auth_enabled': True, 'token_hash': generate_password_hash('api-test')},
    })
    with client.session_transaction() as session:
        session['admin_authenticated'] = authenticated
        session['csrf_token'] = 'test-csrf'


@pytest.mark.parametrize('code', MESSAGES)
def test_all_probe_outcomes_are_small_sanitized_no_store_json(saved_mqtt, code, caplog):
    client, config_path, secrets_path, probe = saved_mqtt
    expected = MqttProbeResult(code == 'success', code, MESSAGES[code], 12, '2026-10-18T12:00:00+00:00')
    probe.return_value = expected
    before = {p.name: p.read_bytes() for p in config_path.parent.iterdir()}
    response = client.post(URL, json={
        'host': 'unsaved-destination', 'mqtt': {'host': 'unsaved-destination'},
        'username': 'unsaved-user', 'password': 'unsaved-password',
    })
    assert response.status_code == 200
    assert response.json == asdict(expected)
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'PRIVATE' not in response.get_data(as_text=True) + caplog.text
    assert before == {p.name: p.read_bytes() for p in config_path.parent.iterdir()}
    probe.assert_called_once()
    settings = probe.call_args.args[0]
    assert settings.host == 'saved-broker'
    assert settings.port == 2883
    assert settings.username == 'PRIVATE-USER'
    assert settings.password == 'PRIVATE-PASSWORD'
    assert settings.authentication_enabled is True
    assert settings.tls_enabled is True
    assert settings.tls_insecure is True
    assert settings.ca_certs == '/saved/ca.pem'


def test_authenticated_success_requires_csrf(saved_mqtt, monkeypatch):
    client, _, _, probe = saved_mqtt
    enable_auth(monkeypatch, client, authenticated=True)
    for headers in ({}, {'X-CSRFToken': 'wrong'}):
        response = client.post(URL, headers=headers)
        assert response.status_code == 400
        assert response.headers['Cache-Control'] == 'no-store'
        probe.assert_not_called()
    response = client.post(URL, headers={'X-CSRFToken': 'test-csrf'})
    assert response.status_code == 200
    assert response.json['success'] is True
    probe.assert_called_once()


def test_api_token_alone_does_not_authorize_session_action(saved_mqtt, monkeypatch):
    import webui

    client, _, _, probe = saved_mqtt
    enable_auth(monkeypatch, client, authenticated=False)
    headers = {'X-WAMF-API-Key': 'api-test'}
    with webui.app.test_request_context(URL, headers=headers):
        assert webui.is_valid_api_token()
    response = client.post(URL, headers=headers)
    assert response.status_code == 302
    assert '/login?' in response.location
    assert response.headers['Cache-Control'] == 'no-store'
    probe.assert_not_called()


@pytest.mark.parametrize('mqtt', [
    {}, None, [], {'host': ''}, {'host': 'your-mqtt-server'},
    {'host': 'broker', 'port': True}, {'host': 'broker', 'port': 65536},
    {'host': 'broker', 'authentication': {'enabled': True}},
    {'host': 'broker', 'tls': {'enabled': 'yes'}},
    {'host': 'broker', 'tls': {'ca_certs': []}},
])
def test_invalid_saved_mqtt_never_probes(saved_mqtt, mqtt):
    client, config_path, secrets_path, probe = saved_mqtt
    config_path.write_text(yaml.safe_dump({'config_version': 2, 'mqtt': mqtt}))
    secrets_path.write_text('secrets_version: 1\n')
    response = client.post(URL)
    assert response.status_code == 200
    assert response.json['code'] == 'configuration_error'
    assert response.json['success'] is False
    probe.assert_not_called()


def test_topic_prefix_is_not_connection_readiness(saved_mqtt):
    client, config_path, _, probe = saved_mqtt
    config = yaml.safe_load(config_path.read_text())
    config['mqtt']['topic_prefix'] = []
    config_path.write_text(yaml.safe_dump(config))
    assert client.post(URL).json['success'] is True
    probe.assert_called_once()


@pytest.mark.parametrize('stage,code', [('loader', 'configuration_error'), ('probe', 'internal_error')])
def test_exceptions_never_escape_or_log_secrets(saved_mqtt, monkeypatch, caplog, stage, code):
    from routes import admin

    client, _, _, probe = saved_mqtt
    if stage == 'loader':
        monkeypatch.setattr(admin, 'load_runtime_config', MagicMock(side_effect=ValueError('PRIVATE-PASSWORD')))
    else:
        probe.side_effect = RuntimeError('PRIVATE-PASSWORD')
    response = client.post(URL)
    assert response.json['code'] == code
    assert 'PRIVATE' not in response.get_data(as_text=True) + caplog.text


def test_settings_html_contains_action_but_no_saved_credentials(saved_mqtt):
    client, config_path, _, _ = saved_mqtt
    # Settings itself uses full normalization, unlike the MQTT-only action.
    config = yaml.safe_load(config_path.read_text())
    for key in ('frigate', 'camera'):
        config.pop(key)
    config_path.write_text(yaml.safe_dump(config))
    html = client.get('/admin/config').get_data(as_text=True)
    assert 'id="mqtt-test-button" type="button"' in html
    assert 'Tests saved MQTT settings. Save changes before testing.' in html
    assert 'Subscription permissions and event delivery were not tested.' in html
    assert 'PRIVATE' not in html
    assert 'id="mqtt-test-result" role="status" aria-live="polite"' in html


def test_route_uses_m1_probe_without_event_operations(saved_mqtt, monkeypatch):
    from app import mqtt_connection
    from routes import admin

    client, _, _, _ = saved_mqtt
    worker = MagicMock()
    worker.connect.return_value = 0
    def accepted(**kwargs):
        worker.on_connect(worker, None, mqtt_connection.mqtt.ConnectFlags(False),
                          mqtt_connection.mqtt.convert_connack_rc_to_reason_code(0), None)
        return 0
    worker.loop.side_effect = accepted
    monkeypatch.setattr(mqtt_connection.mqtt, 'Client', MagicMock(return_value=worker))
    # Keep the route -> M1 parent result path, replacing only the process boundary.
    process = MagicMock(returncode=0)
    process.poll.return_value = 0
    def communicate(payload=None, **kwargs):
        if payload is None:
            return '', None
        import json
        from app.mqtt_settings import MqttSettings
        saved = json.loads(payload)
        return mqtt_connection._probe_in_worker(MqttSettings(**saved['settings']), saved['timeout']), None
    process.communicate.side_effect = communicate
    monkeypatch.setattr(mqtt_connection.subprocess, 'Popen', MagicMock(return_value=process))
    monkeypatch.setattr(admin, 'probe_mqtt_connection', mqtt_connection.probe_mqtt_connection)
    assert client.post(URL).json['success'] is True
    worker.publish.assert_not_called()
    worker.subscribe.assert_not_called()
    worker.reconnect.assert_not_called()
    worker.disconnect.assert_called_once()
