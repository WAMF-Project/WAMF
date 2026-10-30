"""Original-image vectors and read-only credential compatibility."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app import bootstrap, credentials, security
from app.config_loader import load_runtime_config
from app.config_secrets import get_secrets_path

VECTORS = json.loads((Path(__file__).parent / 'fixtures/credentials-v040.json').read_text())['vectors']
ADMIN_VECTORS = [v for v in VECTORS if not v['for_api_only']]


@pytest.mark.parametrize('vector', VECTORS, ids=lambda v: v['id'])
def test_original_image_vectors(vector):
    stored, secret = vector['stored'], vector['secret']
    assert credentials.valid_credential_hash(stored, for_api=True)
    assert credentials.verify_credential(stored, secret, for_api=True)
    assert not credentials.verify_credential(stored, secret + '-incorrect', for_api=True)
    if vector['for_api_only']:
        assert not credentials.valid_credential_hash(stored)
        assert not credentials.verify_credential(stored, secret)
    else:
        assert credentials.valid_credential_hash(stored)
        assert credentials.verify_credential(stored, secret)
        assert not credentials.verify_credential(stored, secret + '-incorrect')


@pytest.mark.parametrize('stored', [
    None, 7, False, {}, [], b'sha256$salt$bad', '', 'not-a-hash',
    'sha256$salt$bad', 'sha256$salt$' + 'A'*64, 'sha256$salt$' + '0'*63,
    'unsupported$salt$' + '0'*64, 'sha256:extra$salt$' + '0'*64,
    'pbkdf2:unknown:600000$salt$' + '0'*64,
    'pbkdf2:sha256:0$salt$' + '0'*64, 'pbkdf2:sha256:-1$salt$' + '0'*64,
    'pbkdf2:sha256:bad$salt$' + '0'*64,
    'pbkdf2:sha256:2147483648$salt$' + '0'*64,
    'pbkdf2:sha256:1:extra$salt$' + '0'*64,
    'scrypt:0:8:1$salt$' + '0'*128, 'scrypt:3:8:1$salt$' + '0'*128,
    'scrypt:32768:0:1$salt$' + '0'*128, 'scrypt:32768:8:0$salt$' + '0'*128,
    'scrypt:bad:8:1$salt$' + '0'*128, 'scrypt:32768:8$salt$' + '0'*128,
    'scrypt:1073741824:8:1$salt$' + '0'*128,
    hashlib.sha256(b'synthetic-bare-digest').hexdigest(),
])
def test_invalid_formats_fail_closed(stored):
    for for_api in (False, True):
        assert not credentials.valid_credential_hash(stored, for_api=for_api)
        assert not credentials.verify_credential(stored, 'synthetic-input', for_api=for_api)


@pytest.mark.parametrize('supplied', [None, 1, False, {}, [], b'bytes', '\ud800'])
def test_invalid_supplied_values_fail_closed(supplied):
    assert not credentials.verify_credential(ADMIN_VECTORS[0]['stored'], supplied)


def test_modern_failure_never_falls_back(monkeypatch):
    vector = next(v for v in VECTORS if v['id'] == 'pbkdf2:sha256:600000-ascii')
    calls = []
    monkeypatch.setattr(credentials, 'check_password_hash', lambda stored, secret: calls.append(stored) or False)
    monkeypatch.setattr(credentials.hmac, 'new', lambda *args: pytest.fail('Modern check fell back to HMAC'))
    assert not credentials.verify_credential(vector['stored'], vector['secret'])
    assert calls == [vector['stored']]


def test_shortened_pbkdf2_freezes_historical_default(monkeypatch):
    vector = next(v for v in VECTORS if v['id'] == 'pbkdf2:sha256-short-ascii')
    original = credentials.check_password_hash
    calls = []
    def check(stored, secret):
        calls.append(stored.split('$')[0])
        return original(stored, secret)
    monkeypatch.setattr(credentials, 'check_password_hash', check)
    assert credentials.verify_credential(vector['stored'], vector['secret'])
    assert calls == ['pbkdf2:sha256:600000']




def _config(stored, token):
    return {
        'config_version': 2,
        'admin': {'auth_enabled': True, 'session_secret': 'synthetic-existing-secret',
                  'password_hash': stored},
        'api': {'token_auth_enabled': True, 'token_hash': token},
    }


def _snapshot(path):
    return path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode


@pytest.mark.parametrize('vector', ADMIN_VECTORS, ids=lambda v: v['id'])
def test_startup_extracts_then_preserves_existing_vectors(vector, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.yml'
    token = next(v for v in VECTORS if v['id'] == 'sha256-empty-salt-ascii')
    config = _config(vector['stored'], token['stored'])
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY', raising=False)
    monkeypatch.setattr(bootstrap, 'generate_password_hash',
                        lambda *a, **k: pytest.fail('Startup rehashed a credential'))
    assert bootstrap.bootstrap_config() == config
    secrets_path = get_secrets_path(path)
    assert load_runtime_config() == config
    before = _snapshot(path), _snapshot(secrets_path)
    backups = sorted(tmp_path.glob('*.bak'))
    for _ in range(2):
        assert bootstrap.bootstrap_config() == config
        assert credentials.verify_credential(vector['stored'], vector['secret'])
    assert before == (_snapshot(path), _snapshot(secrets_path))
    assert sorted(tmp_path.glob('*.bak')) == backups
    assert secrets_path.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('vector', [v for v in VECTORS if v['for_api_only']], ids=lambda v: v['id'])
def test_startup_preserves_api_only_wrappers(vector, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.yml'
    config = _config(ADMIN_VECTORS[0]['stored'], vector['stored'])
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    for _ in range(2):
        assert bootstrap.bootstrap_config() == config
    assert credentials.verify_credential(load_runtime_config()['api']['token_hash'],
                                         vector['secret'], for_api=True)
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('separated', [False, True])
@pytest.mark.parametrize('field,value', [
    ('password_hash', 'unsupported$salt$' + 'a' * 64),
    ('password_hash', 'broken'),
    ('password_hash', 'pbkdf2:sha256:1$salt$bad'), ('password_hash', {'invalid': 'synthetic-value'}),
    ('password_hash', 'your-existing-secret'), ('password_hash', '<unknown-placeholder>'),
    ('password_hash', 'plain$$synthetic-input'),
    ('token_hash', 'unsupported$salt$' + 'a' * 64),
    ('token_hash', 'broken'), ('token_hash', ['invalid']),
])
def test_invalid_existing_credentials_never_replaced(field, value, separated, tmp_path,
                                                     monkeypatch, capsys, caplog):
    path = tmp_path / 'config.yml'
    config = _config(ADMIN_VECTORS[0]['stored'], ADMIN_VECTORS[0]['stored'])
    section = 'api' if field == 'token_hash' else 'admin'
    config[section][field] = value
    if separated:
        secrets = {'secrets_version': 1,
                   'admin': {key: config['admin'].pop(key) for key in ('password_hash', 'session_secret')},
                   'api': {'token_hash': config['api'].pop('token_hash')}}
        secrets_path = get_secrets_path(path)
        secrets_path.write_text(yaml.safe_dump(secrets))
        secrets_path.chmod(0o640)
    path.write_text(yaml.safe_dump(config))
    path.chmod(0o640)
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY', raising=False)
    before = {file.name: _snapshot(file) for file in tmp_path.glob('*.yml')}
    monkeypatch.setattr(bootstrap, 'generate_password_hash',
                        lambda *a, **k: pytest.fail('Invalid credential triggered provisioning'))
    for _ in range(2):
        with pytest.raises(ValueError, match=field) as error:
            bootstrap.bootstrap_config()
        assert str(value) not in str(error.value)
    assert before == {file.name: _snapshot(file) for file in tmp_path.glob('*.yml')}
    assert list(tmp_path.glob('*.bak')) == []
    assert capsys.readouterr().out == '' and caplog.text == ''


ROUTE_VECTORS = [v for v in VECTORS if v['id'] in {
    'sha256-ascii', 'sha1-unicode', 'md5-ascii', 'sha512-ascii',
    'pbkdf2:sha256-short-ascii', 'pbkdf2:sha256:600000-ascii', 'scrypt:32768:8:1-ascii'}]


@pytest.mark.parametrize('vector', ROUTE_VECTORS, ids=lambda v: v['id'])
def test_login_api_password_change_share_verifier(vector, flask_client, tmp_path, monkeypatch):
    import webui
    config = copy.deepcopy(webui.config)
    config.update(_config(vector['stored'], vector['stored']))
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    bootstrap.bootstrap_config()
    monkeypatch.setattr(webui, 'config', load_runtime_config())
    monkeypatch.setitem(webui.app.config, 'SECRET_KEY', 'synthetic-route-secret')
    monkeypatch.setattr(security, 'LOGIN_ATTEMPTS', {})
    monkeypatch.setattr(security, '_login_overflow_attempts', [])
    monkeypatch.setattr(security, 'LOGIN_FAILURE_DELAY_SECONDS', 0)
    monkeypatch.setattr(webui, 'get_system_health', lambda: {
        'frigate_online': False, 'mqtt_online': False, 'database_healthy': True,
        'archive_writable': True, 'disk_used_percent': 0,
    })
    monkeypatch.setattr(webui, 'get_retention_status', lambda: None)
    before = _snapshot(path), _snapshot(get_secrets_path(path))
    with flask_client.session_transaction() as session:
        session.clear()
    assert flask_client.post('/login', data={'password': vector['secret'] + '-wrong'}).status_code == 200
    assert flask_client.get('/admin/api/health', headers={'X-WAMF-API-Key': vector['secret'] + '-wrong'}).status_code == 401
    assert flask_client.get('/admin/api/health', headers={'X-WAMF-API-Key': vector['secret']}).status_code == 200
    assert flask_client.post('/login', data={'password': vector['secret']}).status_code == 302
    assert before == (_snapshot(path), _snapshot(get_secrets_path(path)))
    with flask_client.session_transaction() as session:
        csrf = session['csrf_token']
    response = flask_client.post('/admin/password', data={
        'current_password': vector['secret'], 'new_password': 'synthetic-new-password',
        'confirm_password': 'synthetic-new-password', 'csrf_token': csrf,
    })
    assert response.status_code == 302
    saved = load_runtime_config()
    assert credentials.verify_credential(saved['admin']['password_hash'], 'synthetic-new-password')
    assert saved['api']['token_hash'] == vector['stored']
    assert saved['admin']['session_secret'] == 'synthetic-existing-secret'
    with flask_client.session_transaction() as session:
        session.clear()


@pytest.mark.parametrize('stored', ['unknown$salt$' + 'a' * 64, 'broken', {'invalid': 'value'}, ['bad']])
def test_invalid_hashes_fail_closed_in_routes(stored, flask_client, monkeypatch):
    import webui
    config = copy.deepcopy(webui.config)
    config.update(_config(stored, stored))
    monkeypatch.setattr(webui, 'config', config)
    monkeypatch.setitem(webui.app.config, 'SECRET_KEY', 'synthetic-secret')
    monkeypatch.setattr(security, 'LOGIN_ATTEMPTS', {})
    monkeypatch.setattr(security, '_login_overflow_attempts', [])
    monkeypatch.setattr(security, 'LOGIN_FAILURE_DELAY_SECONDS', 0)
    with flask_client.session_transaction() as session:
        session.clear()
    assert flask_client.get('/admin/api/health', headers={'X-WAMF-API-Key': 'synthetic-input'}).status_code == 401
    assert flask_client.post('/login', data={'password': 'synthetic-input'}).status_code == 200
    with flask_client.session_transaction() as session:
        assert not session.get('admin_authenticated')
        session['admin_authenticated'] = True
        session['csrf_token'] = 'synthetic-csrf'
    response = flask_client.post('/admin/password', data={
        'current_password': 'synthetic-input', 'csrf_token': 'synthetic-csrf',
    })
    assert response.status_code == 200 and b'Password change failed.' in response.data
    with flask_client.session_transaction() as session:
        session.clear()


@pytest.mark.parametrize('vector', [v for v in VECTORS if v['for_api_only']], ids=lambda v: v['id'])
def test_api_only_wrappers_reach_authenticated_api(vector, flask_client, monkeypatch):
    import webui
    config = copy.deepcopy(webui.config)
    config.update(_config(ADMIN_VECTORS[0]['stored'], vector['stored']))
    monkeypatch.setattr(webui, 'config', config)
    monkeypatch.setattr(webui, 'get_system_health', lambda: {
        'frigate_online': False, 'mqtt_online': False, 'database_healthy': True,
        'archive_writable': True, 'disk_used_percent': 0,
    })
    monkeypatch.setattr(webui, 'get_retention_status', lambda: None)
    with flask_client.session_transaction() as session:
        session.clear()
    assert flask_client.get('/admin/api/health', headers={
        'X-WAMF-API-Key': vector['secret'] + '-wrong',
    }).status_code == 401
    assert flask_client.get('/admin/api/health', headers={
        'X-WAMF-API-Key': vector['secret'],
    }).status_code == 200
    assert not credentials.verify_credential(vector['stored'], vector['secret'])
