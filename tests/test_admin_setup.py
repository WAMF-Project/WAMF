"""Offline first-run setup lifecycle, persistence and HTTP security."""

import errno
import hashlib
import json
import logging
import multiprocessing
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import yaml
from werkzeug.security import check_password_hash, generate_password_hash

from app import admin_setup, bootstrap, config_store
from app.credentials import valid_credential_hash

PASSWORD = 'SYNTHETIC_PRIVATE_PASSWORD'


@pytest.fixture
def pending(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.yml'
    path.write_text('admin:\n  auth_enabled: true\n  password_hash: ""\napi:\n  token_hash: ""\n')
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY', raising=False)
    monkeypatch.setattr(admin_setup, '_deadlines', {})
    config = bootstrap.bootstrap_config()
    output = capsys.readouterr().out
    token = re.search(r'#token=(\S+)', output).group(1)
    return path, token, config


def test_token_only_digest_persisted_and_once_printed(pending):
    path, token, config = pending
    assert re.fullmatch(r'[A-Za-z0-9_-]{43}', token)
    state = config['admin']['setup']
    assert state['digest'] == hashlib.sha256(token.encode()).hexdigest()
    assert state['expires_at'] - state['issued_at'] == 900
    assert token not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_success_preserves_credentials_and_rejects_replay(pending):
    path, token, config = pending
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'success'
    saved = yaml.safe_load(path.read_text())
    assert check_password_hash(saved['admin']['password_hash'], PASSWORD)
    assert saved['admin']['session_secret'] == config['admin']['session_secret']
    assert saved['api'] == config['api']
    assert 'setup' not in saved['admin'] and 'setup_pending' not in saved['admin']
    before = path.read_bytes()
    assert admin_setup.complete_setup(token, 'another-password', 'another-password') == 'unavailable'
    assert path.read_bytes() == before


@pytest.mark.parametrize('elapsed,expected', [(899, 'success'), (900, 'expired'), (901, 'expired')])
def test_expiration_boundary(pending, monkeypatch, elapsed, expected):
    path, token, config = pending
    now = config['admin']['setup']['issued_at']
    monkeypatch.setattr(admin_setup.time, 'time', lambda: now + elapsed)
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == expected
    if expected == 'expired':
        monkeypatch.setattr(admin_setup.time, 'time', lambda: now + 1)
        assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'expired'
        assert not valid_credential_hash(yaml.safe_load(path.read_text())['admin'].get('password_hash'))


def test_expiration_rechecked_after_hashing(pending, monkeypatch):
    _, token, config = pending
    expiry = config['admin']['setup']['expires_at']
    monkeypatch.setattr(admin_setup.time, 'time', Mock(side_effect=[expiry - 1, expiry]))
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'expired'


def test_monotonic_expiry_survives_wall_clock_rollback(pending, monkeypatch):
    _, token, config = pending
    deadline = admin_setup._deadlines[config['admin']['setup']['digest']]
    monkeypatch.setattr(admin_setup.time, 'monotonic', lambda: deadline)
    # Wall clock still appears valid; monotonic expiry must nevertheless win.
    monkeypatch.setattr(admin_setup.time, 'time', lambda: config['admin']['setup']['issued_at'] + 1)
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'expired'


def test_clock_rollback_is_terminal_and_restart_rotates(pending, monkeypatch, capsys):
    _, token, config = pending
    now = config['admin']['setup']['issued_at']
    monkeypatch.setattr(admin_setup.time, 'time', lambda: now - 1)
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'expired'
    monkeypatch.setattr(admin_setup.time, 'time', lambda: now + 1)
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'expired'
    bootstrap.bootstrap_config()
    new_token = re.search(r'#token=(\S+)', capsys.readouterr().out).group(1)
    assert new_token != token
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'invalid'
    assert admin_setup.complete_setup(new_token, PASSWORD, PASSWORD) == 'success'


@pytest.mark.parametrize('exhausted', [False, True])
def test_concurrent_submissions_have_one_winner(pending, exhausted):
    _, token, _ = pending
    if exhausted:
        for _ in range(admin_setup.ATTEMPT_LIMIT):
            assert admin_setup.complete_setup('A' * 43, PASSWORD, PASSWORD) == 'invalid'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: admin_setup.complete_setup(token, PASSWORD, PASSWORD), range(2)))
    assert sorted(results) == ['success', 'unavailable']


def test_processes_share_single_use_configuration_lock(pending):
    _, token, _ = pending
    context = multiprocessing.get_context('fork')
    results = context.Queue()
    def submit():
        results.put(admin_setup.complete_setup(token, PASSWORD, PASSWORD))
    processes = [context.Process(target=submit) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(results.get(timeout=2) for _ in processes) == ['success', 'unavailable']
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        results.close()


def test_file_fsync_failure_never_replaces_original(pending, monkeypatch):
    path, token, _ = pending
    before = path.read_bytes()
    replace = Mock()
    monkeypatch.setattr(config_store.os, 'replace', replace)
    def fail(*args):
        raise OSError(errno.EIO, 'synthetic sensitive failure')
    monkeypatch.setattr(config_store.os, 'fsync', fail)
    with pytest.raises(OSError):
        admin_setup.complete_setup(token, PASSWORD, PASSWORD)
    assert path.read_bytes() == before
    assert list(path.parent.glob('.wamf-config-*')) == []
    replace.assert_not_called()


def test_expiration_rechecked_after_file_fsync_before_replacement(pending, monkeypatch):
    path, token, config = pending
    state = config['admin']['setup']
    now = [state['issued_at'] + 1]
    real_fsync = config_store.os.fsync
    def fsync(descriptor):
        real_fsync(descriptor)
        now[0] = state['expires_at']
    monkeypatch.setattr(admin_setup.time, 'time', lambda: now[0])
    monkeypatch.setattr(config_store.os, 'fsync', fsync)
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'expired'
    saved = yaml.safe_load(path.read_text())
    assert admin_setup.is_pending(saved)
    assert saved['admin']['setup']['expired'] is True


@pytest.mark.parametrize('error_number', [errno.EACCES, errno.EROFS, errno.EBUSY])
def test_atomic_replace_failure_preserves_original(pending, monkeypatch, error_number):
    path, token, _ = pending
    original = path.read_bytes()
    def fail(*args):
        raise OSError(error_number, 'SYNTHETIC_SECRET mount failure')
    monkeypatch.setattr(config_store.os, 'replace', fail)
    with pytest.raises(OSError):
        admin_setup.complete_setup(token, PASSWORD, PASSWORD)
    assert path.read_bytes() == original
    assert list(path.parent.glob('.wamf-config-*')) == []


def test_read_only_configuration_fails_without_change(pending):
    path, token, _ = pending
    original = path.read_bytes()
    path.chmod(0o400)
    try:
        with pytest.raises(PermissionError):
            admin_setup.complete_setup(token, PASSWORD, PASSWORD)
        assert path.read_bytes() == original
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize('url', [
    'https://user:password@example.com', 'https://example.com?token=secret',
    'https://example.com#token=secret', 'https://example.com/path', '//example.com',
    'ftp://example.com', 'http://', 'http://example.com:', 'http://example.com:70000',
    'https://example.com\r\nFORGED', 'http://example.com\\evil', 'http://0.0.0.0',
    'http://[::]', 'http://bad..host', 'http://-bad.host', 'http://%65xample.com',
    'http://[v1.example]', 'http://999.999.999.999',
    'http://[::1]garbage', 'http://[::1]extra:80', 'http://example.com..',
])
def test_public_url_rejects_unsafe_origins(url):
    with pytest.raises(ValueError):
        admin_setup.public_setup_url({'webui': {'public_base_url': url}})


@pytest.mark.parametrize('url', ['https://wamf.example', 'http://localhost:7767/', 'https://[::1]:8443'])
def test_public_url_valid_origin(url):
    assert admin_setup.public_setup_url({'webui': {'public_base_url': url}}) == url.rstrip('/') + '/setup'


@pytest.fixture
def setup_client(pending, flask_client, monkeypatch):
    import webui
    _, token, config = pending
    monkeypatch.setattr(webui, 'config', config)
    monkeypatch.setitem(webui.app.config, 'SECRET_KEY', config['admin']['session_secret'])
    runtime = Mock()
    monkeypatch.setattr(webui, 'start_runtime_services', runtime)
    with flask_client.session_transaction() as session:
        session.clear()
    response = flask_client.get('/setup')
    assert response.status_code == 200
    with flask_client.session_transaction() as session:
        csrf = session['csrf_token']
    return flask_client, token, csrf, runtime


def test_get_does_not_consume_and_gates_operational_routes(setup_client, pending):
    client, token, _, runtime = setup_client
    path, _, _ = pending
    before = path.read_bytes()
    for target in ['/setup', '/setup']:
        response = client.get(target)
        assert response.status_code == 200
        assert token.encode() not in response.data
    for target in ['/', '/admin', '/login', '/admin/api/status', '/frigate/a/snapshot.jpg']:
        response = client.get(target)
        assert response.status_code == 303
        assert response.location == '/setup'
    assert path.read_bytes() == before
    runtime.assert_not_called()


@pytest.mark.parametrize('level', [logging.WARNING, logging.DEBUG])
def test_setup_http_success_has_no_secret_disclosure(setup_client, pending, tmp_dbs, caplog, level):
    client, token, csrf, runtime = setup_client
    with caplog.at_level(level):
        response = client.post('/setup', data={'token': token, 'password': PASSWORD,
            'confirmation': PASSWORD, 'csrf_token': csrf})
    assert response.status_code == 303 and response.location == '/login'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['Referrer-Policy'] == 'no-referrer'
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert response.request.path == '/setup' and response.request.query_string == b''
    with client.session_transaction() as session:
        assert not session.get('admin_authenticated')
        assert token not in json.dumps(dict(session))
    for secret in (token, PASSWORD):
        assert secret not in caplog.text
        assert secret.encode() not in response.data
        assert secret not in str(response.headers)
    runtime.assert_called_once_with()
    with sqlite3.connect(tmp_dbs['det_db']) as database:
        messages = '\n'.join(row[0] for row in database.execute('SELECT message FROM system_events'))
    assert token not in messages and PASSWORD not in messages
    assert client.get('/setup').status_code == 404
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302


@pytest.mark.parametrize('kind', ['csrf', 'unicode-csrf', 'long-csrf', 'short', 'mismatch', 'invalid', 'oversized'])
def test_setup_rejects_invalid_submissions(setup_client, pending, kind):
    client, token, csrf, runtime = setup_client
    data = {'token': token, 'password': PASSWORD, 'confirmation': PASSWORD, 'csrf_token': csrf}
    if kind == 'csrf': data['csrf_token'] = 'wrong'
    if kind == 'unicode-csrf': data['csrf_token'] = '\u2603'
    if kind == 'long-csrf': data['csrf_token'] = 'C' * 129
    if kind == 'short': data.update(password='short', confirmation='short')
    if kind == 'mismatch': data['confirmation'] = 'different'
    if kind == 'invalid': data['token'] = 'A' * 43
    if kind == 'oversized': data['password'] = 'P' * 9000
    response = client.post('/setup', data=data)
    assert response.status_code == (413 if kind == 'oversized' else 400)
    assert token.encode() not in response.data and PASSWORD.encode() not in response.data
    runtime.assert_not_called()
    assert admin_setup.is_pending(yaml.safe_load(pending[0].read_text()))


def test_invalid_rate_limit_cannot_block_valid_redemption(setup_client, pending, monkeypatch):
    client, token, csrf, runtime = setup_client
    password_hash = Mock(wraps=admin_setup.generate_password_hash)
    monkeypatch.setattr(admin_setup, 'generate_password_hash', password_hash)
    data = {'token': 'A' * 43, 'password': PASSWORD, 'confirmation': PASSWORD, 'csrf_token': csrf}
    for i in range(10):
        assert client.post('/setup', data=data, headers={'X-Forwarded-For': f'192.0.2.{i}'}).status_code == 400
    assert client.post('/setup', data=data).status_code == 429
    limited_content = pending[0].read_bytes()
    # Continuing abuse and changing forwarded IPs cannot extend the window or
    # consume any allowance needed by the actual token holder.
    for i in range(5):
        assert client.post('/setup', data=data, headers={'X-Forwarded-For': f'198.51.100.{i}'}).status_code == 429
    state = yaml.safe_load(pending[0].read_text())['admin']['setup']
    assert state['attempts'] == 10
    assert state['expires_at'] == pending[2]['admin']['setup']['expires_at']
    assert pending[0].read_bytes() == limited_content
    password_hash.assert_not_called()
    data['token'] = token
    assert client.post('/setup', data=data).status_code == 303
    password_hash.assert_called_once_with(PASSWORD)
    runtime.assert_called_once()


def test_invalid_token_window_recovers_without_extending_expiry(pending, monkeypatch):
    path, token, config = pending
    state = config['admin']['setup']
    now = [state['issued_at'] + 1]
    monkeypatch.setattr(admin_setup.time, 'time', lambda: now[0])
    for _ in range(admin_setup.ATTEMPT_LIMIT):
        assert admin_setup.complete_setup('A' * 43, PASSWORD, PASSWORD) == 'invalid'
    assert admin_setup.complete_setup('A' * 43, PASSWORD, PASSWORD) == 'limited'
    now[0] += admin_setup.ATTEMPT_WINDOW
    assert admin_setup.complete_setup('A' * 43, PASSWORD, PASSWORD) == 'invalid'
    current = yaml.safe_load(path.read_text())['admin']['setup']
    assert current['attempts'] == 1
    assert current['expires_at'] == state['expires_at']
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'success'


def test_valid_token_password_corrections_do_not_spend_invalid_allowance(setup_client, pending, monkeypatch):
    client, token, csrf, runtime = setup_client
    password_hash = Mock(wraps=admin_setup.generate_password_hash)
    monkeypatch.setattr(admin_setup, 'generate_password_hash', password_hash)
    data = {'token': token, 'password': 'short', 'confirmation': 'short', 'csrf_token': csrf}
    for _ in range(admin_setup.ATTEMPT_LIMIT + 1):
        response = client.post('/setup', data=data)
        assert response.status_code == 400
        assert 'Passwords must match' in response.json['error']
    assert yaml.safe_load(pending[0].read_text())['admin']['setup']['attempts'] == 0
    password_hash.assert_not_called()
    runtime.assert_not_called()
    data.update(password=PASSWORD, confirmation=PASSWORD)
    assert client.post('/setup', data=data).status_code == 303
    password_hash.assert_called_once_with(PASSWORD)


def test_exhausted_invalid_allowance_does_not_bypass_expiry(pending, monkeypatch):
    _, token, config = pending
    for _ in range(admin_setup.ATTEMPT_LIMIT):
        assert admin_setup.complete_setup('A' * 43, PASSWORD, PASSWORD) == 'invalid'
    monkeypatch.setattr(admin_setup.time, 'time', lambda: config['admin']['setup']['expires_at'])
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'expired'


@pytest.mark.parametrize('level', [logging.WARNING, logging.DEBUG])
def test_setup_save_error_logs_only_safe_metadata(setup_client, monkeypatch, caplog, level):
    client, token, csrf, runtime = setup_client
    def fail(*args, **kwargs):
        raise PermissionError(errno.EACCES, token + PASSWORD)
    monkeypatch.setattr(admin_setup, 'atomic_write_config', fail)
    with caplog.at_level(level):
        response = client.post('/setup', data={'token': token, 'password': PASSWORD,
            'confirmation': PASSWORD, 'csrf_token': csrf})
    assert response.status_code == 503
    assert 'errno' in caplog.text
    for secret in (token, PASSWORD):
        assert secret not in caplog.text and secret.encode() not in response.data
    assert all(record.exc_info is None for record in caplog.records)
    runtime.assert_not_called()


def test_existing_valid_hash_takes_precedence_over_stale_state(pending, capsys):
    path, _, config = pending
    config['admin']['password_hash'] = generate_password_hash(PASSWORD)
    config['api']['token_hash'] = generate_password_hash('existing-api-secret')
    path.write_text(yaml.safe_dump(config))
    original = path.read_bytes()
    bootstrap.bootstrap_config()
    assert not admin_setup.is_pending(config)
    assert path.read_bytes() == original
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('section,value', [('admin', []), ('admin', None), ('api', []), ('api', None)])
def test_malformed_authentication_sections_never_provision(tmp_path, monkeypatch, capsys, section, value):
    path = tmp_path / 'config.yml'
    config = {'admin': {'auth_enabled': True}, 'api': {}}
    config[section] = value
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        bootstrap.bootstrap_config()
    assert path.read_bytes() == before
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('fields', [
    {'setup_pending': False}, {'setup_pending': 'true'},
    {'setup_pending': True}, {'setup': {}}, {'auth_enabled': 'false'},
])
def test_ambiguous_first_run_state_never_provisions(tmp_path, monkeypatch, capsys, fields):
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump({'admin': {'auth_enabled': True, **fields}}))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        bootstrap.bootstrap_config()
    assert path.read_bytes() == before
    assert capsys.readouterr().out == ''


def test_no_startup_deadline_fails_closed_without_http_generation(pending, monkeypatch):
    path, token, _ = pending
    before = path.read_bytes()
    monkeypatch.setattr(admin_setup, '_deadlines', {})
    monkeypatch.setattr(bootstrap.secrets, 'token_urlsafe', lambda *args: pytest.fail('HTTP generated token'))
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'unavailable'
    assert path.read_bytes() == before


def test_http_expiry_does_not_activate_workers(setup_client, pending, monkeypatch):
    client, token, csrf, runtime = setup_client
    expiry = pending[2]['admin']['setup']['expires_at']
    monkeypatch.setattr(admin_setup.time, 'time', lambda: expiry)
    response = client.post('/setup', data={'token': token, 'password': PASSWORD,
        'confirmation': PASSWORD, 'csrf_token': csrf})
    assert response.status_code == 400
    runtime.assert_not_called()


def test_setup_never_copies_url_secrets_into_response(setup_client):
    client, token, _, _ = setup_client
    response = client.get('/setup', headers={'Host': 'attacker.invalid',
        'X-Forwarded-Host': 'attacker.invalid', 'X-Forwarded-Proto': 'https'})
    assert response.status_code == 200
    assert b'attacker.invalid' not in response.data and token.encode() not in response.data
    assert 'Location' not in response.headers


@pytest.mark.parametrize('section,field,value', [
    ('admin', 'password_hash', 'malformed-existing-hash'),
    ('admin', 'auth_enabled', 'true'),
    ('admin', 'session_secret', ''),
    ('api', 'token_hash', 'malformed-existing-token'),
])
def test_changed_authoritative_credentials_fail_closed(pending, section, field, value):
    path, token, config = pending
    config[section][field] = value
    path.write_text(yaml.safe_dump(config))
    before = path.read_bytes()
    assert admin_setup.complete_setup(token, PASSWORD, PASSWORD) == 'unavailable'
    assert path.read_bytes() == before


@pytest.mark.parametrize('content', [
    'admin: {auth_enabled: true}\nadmin: {auth_enabled: true}\n',
    'admin: {auth_enabled: true, password_hash: "", password_hash: ""}\n',
    'api: {}\napi: {}\nadmin: {auth_enabled: true}\n',
    'defaults: &fresh {auth_enabled: true}\nadmin: *fresh\n',
    'admin: &fresh {auth_enabled: true}\nother: *fresh\n',
])
def test_ambiguous_or_anchored_setup_config_is_not_rewritten(tmp_path, monkeypatch, capsys, content):
    path = tmp_path / 'config.yml'
    path.write_text(content)
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY', raising=False)
    with pytest.raises(ValueError):
        bootstrap.bootstrap_config()
    assert path.read_text() == content
    assert capsys.readouterr().out == ''


def test_existing_anchored_credentials_preserved(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.yml'
    path.write_text('defaults: &existing ' + yaml.safe_dump({
        'auth_enabled': True, 'session_secret': 'synthetic-existing-secret',
        'password_hash': generate_password_hash(PASSWORD),
    }, default_flow_style=True).strip() + '\nadmin: *existing\n')
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    before = path.read_bytes()
    bootstrap.bootstrap_config()
    assert path.read_bytes() == before
    assert capsys.readouterr().out == ''
