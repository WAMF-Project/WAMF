"""Saved-only Frigate action security and non-mutation contract."""
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest
import yaml
from werkzeug.security import generate_password_hash

from app.frigate_connection import (
    DISCOVERY_MESSAGES,
    MESSAGES,
    FrigateDiscoveryResult,
    FrigateProbeResult,
)

URL = '/admin/config/frigate/test'


@pytest.fixture
def saved_frigate(flask_client, tmp_path, monkeypatch):
    import webui
    from routes import admin
    config = {
        'config_version': 2,
        'frigate': {'frigate_url': 'https://PRIVATE-USER:PRIVATE-PASSWORD@saved-frigate/prefix'},
        'mqtt': [], 'classification': None, 'camera': None,
    }
    config_path = tmp_path / 'config.yml'
    config_path.write_text(yaml.safe_dump(config))
    secrets_path = tmp_path / 'secrets.yml'
    secrets_path.write_text(yaml.safe_dump({'secrets_version': 1, 'mqtt': {'password': 'PRIVATE-SECRET'}}))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(config_path))
    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    probe = MagicMock(return_value=FrigateProbeResult(
        True, 'success', MESSAGES['success'], 12, '2026-10-19T12:00:00+00:00',
    ))
    monkeypatch.setattr(admin, 'probe_frigate_connection', probe)
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


def test_authenticated_success_requires_csrf(saved_frigate, monkeypatch):
    client, _, _, probe = saved_frigate
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


def test_api_token_alone_does_not_authorize_session_action(saved_frigate, monkeypatch):
    import webui

    client, _, _, probe = saved_frigate
    enable_auth(monkeypatch, client, authenticated=False)
    headers = {'X-WAMF-API-Key': 'api-test'}
    with webui.app.test_request_context(URL, headers=headers):
        assert webui.is_valid_api_token()
    response = client.post(URL, headers=headers)
    assert response.status_code == 302
    assert '/login?' in response.location
    assert response.headers['Cache-Control'] == 'no-store'
    probe.assert_not_called()


@pytest.mark.parametrize('code', MESSAGES)
@pytest.mark.parametrize('payload_kind', ['json', 'form'])
def test_outcomes_saved_only_and_no_side_effects(saved_frigate, monkeypatch, caplog, code, payload_kind):
    import webui
    from app import health
    from routes import admin
    client, config_path, _, probe = saved_frigate
    expected = FrigateProbeResult(code == 'success', code, MESSAGES[code], 12, '2026-10-19T12:00:00+00:00')
    probe.return_value = expected
    events = MagicMock(side_effect=AssertionError('must not publish events'))
    writer = MagicMock(side_effect=AssertionError('must not write config'))
    monkeypatch.setattr(admin, 'log_system_event', events)
    monkeypatch.setattr(webui, 'write_config_preserving_admin', writer)
    monkeypatch.setattr(health, '_previous_health_state', 'sentinel')
    runtime_before = webui.config.copy()
    before = {p.name: p.read_bytes() for p in config_path.parent.iterdir()}
    payload = {'frigate_url': 'http://unsaved', 'frigate': {'frigate_url': 'http://unsaved'}}
    response = client.post(URL, **({'json': payload} if payload_kind == 'json' else {'data': {'frigate_url': 'http://unsaved'}}))
    assert response.status_code == 200
    assert response.json == asdict(expected)
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'PRIVATE' not in response.get_data(as_text=True) + caplog.text
    probe.assert_called_once_with('https://PRIVATE-USER:PRIVATE-PASSWORD@saved-frigate/prefix')
    assert before == {p.name: p.read_bytes() for p in config_path.parent.iterdir()}
    assert webui.config == runtime_before
    assert health._previous_health_state == 'sentinel'
    events.assert_not_called()
    writer.assert_not_called()


@pytest.mark.parametrize('frigate', [None, [], {}, {'frigate_url': ''},
    {'frigate_url': 'your-frigate-server'}, {'frigate_url': []},
    {'frigate_url': 'ftp://PRIVATE'}, {'frigate_url': 'http://[PRIVATE'}])
def test_invalid_saved_settings_never_probe(saved_frigate, frigate):
    client, config_path, _, probe = saved_frigate
    config_path.write_text(yaml.safe_dump({'config_version': 2, 'frigate': frigate}))
    response = client.post(URL)
    assert response.status_code == 200
    assert response.json['code'] == 'configuration_error'
    assert response.headers['Cache-Control'] == 'no-store'
    probe.assert_not_called()


@pytest.mark.parametrize('stage,code', [('loader', 'configuration_error'), ('probe', 'internal_error')])
def test_exception_leakage(saved_frigate, monkeypatch, caplog, stage, code):
    from routes import admin
    client, _, _, probe = saved_frigate
    if stage == 'loader':
        monkeypatch.setattr(admin, 'load_runtime_config', MagicMock(side_effect=ValueError('https://PRIVATE:PRIVATE@host')))
    else:
        probe.side_effect = RuntimeError('https://PRIVATE:PRIVATE@host')
    response = client.post(URL)
    assert response.json['code'] == code
    assert 'PRIVATE' not in response.get_data(as_text=True) + caplog.text


@pytest.mark.parametrize("discovery_mode", [False, True])
def test_route_to_client_boundary(saved_frigate, monkeypatch, discovery_mode):
    import io
    import json
    import os

    from app import frigate_connection
    from app.frigate_probe_worker import main
    from routes import admin
    client, _, _, _ = saved_frigate
    api = MagicMock()
    api.check_api_connection.return_value = 'success'
    api.get_camera_inventory.return_value = [
        {'id': 'Exact_ID', 'display_name': 'Friendly name', 'enabled': None, 'detection_enabled': False},
    ]
    monkeypatch.setattr('app.frigate_probe_worker.FrigateClient', MagicMock(return_value=api))
    process = MagicMock(returncode=0)
    process.poll.return_value = 0
    def communicate(payload=None, **kwargs):
        if payload is None:
            return '', None
        assert json.loads(payload)['base_url'].startswith('https://PRIVATE')
        with monkeypatch.context() as patcher:
            patcher.setattr('app.frigate_probe_worker.sys.stdin', io.StringIO(payload))
            patcher.setattr('app.frigate_probe_worker.sys.argv', ['worker', str(os.getppid())])
            # Do not install a parent-death signal on the test runner itself.
            patcher.setattr('app.frigate_probe_worker.sys.platform', 'test')
            return main(), None
    process.communicate.side_effect = communicate
    monkeypatch.setattr(frigate_connection.subprocess, 'Popen', MagicMock(return_value=process))
    monkeypatch.setattr(admin, 'probe_frigate_connection', frigate_connection.probe_frigate_connection)
    monkeypatch.setattr(admin, 'discover_frigate_cameras', frigate_connection.discover_frigate_cameras)
    result = client.post(DISCOVER_URL if discovery_mode else URL).json
    assert result['success'] is True
    if discovery_mode:
        assert result['cameras'] == api.get_camera_inventory.return_value
        api.get_camera_inventory.assert_called_once_with()
        api.check_api_connection.assert_not_called()
    else:
        api.check_api_connection.assert_called_once_with()
        api.get_camera_inventory.assert_not_called()


def test_settings_action_markup(saved_frigate):
    client, config_path, _, _ = saved_frigate
    config_path.write_text(yaml.safe_dump({'config_version': 2, 'frigate': {'frigate_url': 'http://fixture'}}))
    html = client.get('/admin/config').get_data(as_text=True)
    assert 'id="frigate-test-button" type="button"' in html
    assert 'Uses the saved Frigate settings. Save changes before testing.' in html
    assert 'This checks the Frigate API connection only, not camera availability.' in html
    assert 'id="frigate-test-result" role="status" aria-live="polite"' in html


DISCOVER_URL = '/admin/config/frigate/cameras/discover'


@pytest.fixture
def saved_discovery(saved_frigate, monkeypatch):
    from app.frigate_connection import DISCOVERY_MESSAGES, FrigateDiscoveryResult
    from routes import admin
    client, config_path, secrets_path, _ = saved_frigate
    discovery = MagicMock(return_value=FrigateDiscoveryResult(
        True, 'success', DISCOVERY_MESSAGES['success'], 10, '2026-10-19T12:00:00+00:00', [],
    ))
    monkeypatch.setattr(admin, 'discover_frigate_cameras', discovery)
    return client, config_path, secrets_path, discovery


def test_discovery_authenticated_success_requires_csrf(saved_discovery, monkeypatch):
    client, _, _, probe = saved_discovery
    enable_auth(monkeypatch, client, authenticated=True)
    for headers in ({}, {'X-CSRFToken': 'wrong'}):
        response = client.post(DISCOVER_URL, headers=headers)
        assert response.status_code == 400
        assert response.headers['Cache-Control'] == 'no-store'
        probe.assert_not_called()
    response = client.post(DISCOVER_URL, headers={'X-CSRFToken': 'test-csrf'})
    assert response.status_code == 200
    assert response.json['success'] is True
    probe.assert_called_once()


def test_discovery_api_token_alone_does_not_authorize_session_action(saved_discovery, monkeypatch):
    import webui

    client, _, _, probe = saved_discovery
    enable_auth(monkeypatch, client, authenticated=False)
    headers = {'X-WAMF-API-Key': 'api-test'}
    with webui.app.test_request_context(DISCOVER_URL, headers=headers):
        assert webui.is_valid_api_token()
    response = client.post(DISCOVER_URL, headers=headers)
    assert response.status_code == 302
    assert '/login?' in response.location
    assert response.headers['Cache-Control'] == 'no-store'
    probe.assert_not_called()


@pytest.mark.parametrize('code', DISCOVERY_MESSAGES)
@pytest.mark.parametrize('payload_kind', ['json', 'form'])
def test_discovery_outcomes_saved_only_and_no_side_effects(saved_discovery, monkeypatch, caplog, code, payload_kind):
    import webui
    from app import health
    from routes import admin
    client, config_path, _, probe = saved_discovery
    expected = FrigateDiscoveryResult(code == 'success', code, DISCOVERY_MESSAGES[code], 12, '2026-10-19T12:00:00+00:00', [] if code == 'success' else None)
    probe.return_value = expected
    events = MagicMock(side_effect=AssertionError('must not publish events'))
    writer = MagicMock(side_effect=AssertionError('must not write config'))
    monkeypatch.setattr(admin, 'log_system_event', events)
    monkeypatch.setattr(webui, 'write_config_preserving_admin', writer)
    monkeypatch.setattr(health, '_previous_health_state', 'sentinel')
    runtime_before = webui.config.copy()
    before = {p.name: p.read_bytes() for p in config_path.parent.iterdir()}
    payload = {'frigate_url': 'http://unsaved', 'frigate': {'frigate_url': 'http://unsaved'}}
    response = client.post(DISCOVER_URL, **({'json': payload} if payload_kind == 'json' else {'data': {'frigate_url': 'http://unsaved'}}))
    assert response.status_code == 200
    assert response.json == asdict(expected)
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'PRIVATE' not in response.get_data(as_text=True) + caplog.text
    probe.assert_called_once_with('https://PRIVATE-USER:PRIVATE-PASSWORD@saved-frigate/prefix')
    assert before == {p.name: p.read_bytes() for p in config_path.parent.iterdir()}
    assert webui.config == runtime_before
    assert health._previous_health_state == 'sentinel'
    events.assert_not_called()
    writer.assert_not_called()


@pytest.mark.parametrize('frigate', [None, [], {}, {'frigate_url': ''},
    {'frigate_url': 'your-frigate-server'}, {'frigate_url': []},
    {'frigate_url': 'ftp://PRIVATE'}, {'frigate_url': 'http://[PRIVATE'}])
def test_discovery_invalid_saved_settings_never_probe(saved_discovery, frigate):
    client, config_path, _, probe = saved_discovery
    config_path.write_text(yaml.safe_dump({'config_version': 2, 'frigate': frigate}))
    response = client.post(DISCOVER_URL)
    assert response.status_code == 200
    assert response.json['code'] == 'configuration_error'
    assert response.headers['Cache-Control'] == 'no-store'
    probe.assert_not_called()


@pytest.mark.parametrize('stage,code', [('loader', 'configuration_error'), ('probe', 'internal_error')])
def test_discovery_exception_leakage(saved_discovery, monkeypatch, caplog, stage, code):
    from routes import admin
    client, _, _, probe = saved_discovery
    if stage == 'loader':
        monkeypatch.setattr(admin, 'load_runtime_config', MagicMock(side_effect=ValueError('https://PRIVATE:PRIVATE@host')))
    else:
        probe.side_effect = RuntimeError('https://PRIVATE:PRIVATE@host')
    response = client.post(DISCOVER_URL)
    assert response.json['code'] == code
    assert 'PRIVATE' not in response.get_data(as_text=True) + caplog.text


def test_discovery_empty_success_differs_from_failure(saved_discovery):
    from app.frigate_connection import DISCOVERY_MESSAGES, FrigateDiscoveryResult
    client, _, _, discovery = saved_discovery
    assert client.post(DISCOVER_URL).json['cameras'] == []
    discovery.return_value = FrigateDiscoveryResult(
        False, 'auth_rejected', DISCOVERY_MESSAGES['auth_rejected'], 0,
        '2026-10-19T12:00:00+00:00', None,
    )
    result = client.post(DISCOVER_URL).json
    assert result['success'] is False
    assert result['cameras'] is None


def test_discovery_action_markup_and_explicit_empty_field(saved_discovery):
    client, config_path, _, _ = saved_discovery
    config_path.write_text(yaml.safe_dump({'config_version': 2, 'frigate': {'camera': ['Garden']}}))
    html = client.get('/admin/config').get_data(as_text=True)
    assert 'id="frigate-discover-button" type="button"' in html
    assert 'id="frigate-discovery-result" role="status" aria-live="polite"' in html
    assert 'name="frigate_cameras" rows="4" aria-describedby=' in html
    assert 'Discovery does not save changes.' in html
