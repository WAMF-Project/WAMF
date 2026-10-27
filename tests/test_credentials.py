"""Original-image vectors and read-only credential compatibility."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app import admin_setup, bootstrap, credentials, security

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


@pytest.mark.parametrize('vector', ADMIN_VECTORS, ids=lambda v: v['id'])
def test_startup_preserves_existing_vectors(vector, tmp_path, monkeypatch, capsys, caplog):
    path = tmp_path/'config.yml'
    token = next(v for v in VECTORS if v['id'] == 'sha256-empty-salt-ascii')
    config = {'admin': {'auth_enabled': True, 'session_secret': 'synthetic-existing-session-secret',
                        'password_hash': vector['stored']},
              'api': {'token_auth_enabled': True, 'token_hash': token['stored']}}
    path.write_text('# Existing comment and formatting\n'+yaml.safe_dump(config,sort_keys=False))
    path.chmod(0o600)
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY', raising=False)
    monkeypatch.setattr(admin_setup, 'generate_password_hash', lambda *a, **k: pytest.fail('Startup rehashed a credential'))
    before = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode
    for _ in range(2):
        assert bootstrap.bootstrap_config() == config
        assert credentials.verify_credential(vector['stored'], vector['secret'])
    assert before == (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
    assert len(list(tmp_path.iterdir())) == 1
    assert capsys.readouterr().out == '' and caplog.text == ''


@pytest.mark.parametrize('vector', [v for v in VECTORS if v['for_api_only']], ids=lambda v: v['id'])
def test_startup_preserves_api_only_wrappers(vector, tmp_path, monkeypatch, capsys):
    cfg = {'admin': {'auth_enabled': True, 'session_secret': 'synthetic-existing-secret',
                     'password_hash': ADMIN_VECTORS[0]['stored']},
           'api': {'token_auth_enabled': True, 'token_hash': vector['stored']}}
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump(cfg))
    original = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY', raising=False)
    for _ in range(2):
        assert bootstrap.bootstrap_config() == cfg
        assert credentials.verify_credential(vector['stored'], vector['secret'], for_api=True)
    assert original == (path.read_bytes(), path.stat().st_mtime_ns)
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('field,value', [
    ('password_hash','unsupported$salt$'+'a'*64), ('password_hash','broken'),
    ('password_hash',{'invalid':'secret-value'}), ('password_hash','your-existing-secret'),
    ('token_hash','unsupported$salt$'+'a'*64), ('token_hash','broken'),
    ('token_hash',['invalid']), ('session_secret',123),
])
def test_existing_invalid_config_is_never_repaired(field, value, tmp_path, monkeypatch, capsys, caplog):
    vector = ADMIN_VECTORS[0]
    cfg = {'admin':{'auth_enabled':True,'session_secret':'synthetic-existing-secret','password_hash':vector['stored']},
           'api':{'token_auth_enabled':True,'token_hash':vector['stored']}}
    cfg['api' if field == 'token_hash' else 'admin'][field] = value
    path = tmp_path/'config.yml';path.write_text(yaml.safe_dump(cfg))
    before = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG',str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY',raising=False)
    for _ in range(2):
        with pytest.raises(ValueError,match=field) as error:bootstrap.bootstrap_config()
        assert str(value) not in str(error.value)
    assert before == (path.read_bytes(),path.stat().st_mtime_ns,path.stat().st_mode)
    assert len(list(tmp_path.iterdir())) == 1
    assert capsys.readouterr().out == '' and caplog.text == ''


@pytest.mark.parametrize('configured', ['session_secret','token_hash'])
def test_partial_existing_installation_is_not_fresh(configured, tmp_path, monkeypatch, capsys):
    cfg = {'admin':{'auth_enabled':True},'api':{}}
    if configured == 'session_secret':cfg['admin']['session_secret'] = 'synthetic-existing-secret'
    else:cfg['api']['token_hash'] = ADMIN_VECTORS[0]['stored']
    path=tmp_path/'config.yml';path.write_text(yaml.safe_dump(cfg))
    original=path.read_bytes()
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG',str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY',raising=False)
    with pytest.raises(ValueError,match='password_hash'):bootstrap.bootstrap_config()
    assert path.read_bytes()==original and capsys.readouterr().out==''


ROUTE_VECTORS = [v for v in VECTORS if v['id'] in {
    'sha256-ascii','sha1-unicode','md5-ascii','sha512-ascii',
    'pbkdf2:sha256-short-ascii','pbkdf2:sha256:600000-ascii','scrypt:32768:8:1-ascii'}]


@pytest.mark.parametrize('vector', ROUTE_VECTORS, ids=lambda v:v['id'])
def test_login_api_and_password_change_share_verification(vector, flask_client, tmp_path, monkeypatch, caplog):
    import webui
    cfg=copy.deepcopy(webui.config)
    cfg['admin']={'auth_enabled':True,'session_secret':'synthetic-route-secret','password_hash':vector['stored']}
    cfg['api']={'token_auth_enabled':True,'token_hash':vector['stored']}
    path=tmp_path/'config.yml';path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG',str(path))
    monkeypatch.setattr(webui,'config',cfg)
    monkeypatch.setitem(webui.app.config,'SECRET_KEY','synthetic-route-secret')
    monkeypatch.setattr(security,'LOGIN_ATTEMPTS',{})
    monkeypatch.setattr(security,'LOGIN_FAILURE_DELAY_SECONDS',0)
    monkeypatch.setattr(webui,'get_system_health',lambda:{'frigate_online':True,'mqtt_online':True,
                        'database_healthy':True,'archive_writable':True,'disk_used_percent':0})
    monkeypatch.setattr(webui,'get_retention_status',lambda:None)
    before=path.read_bytes(),path.stat().st_mtime_ns
    with flask_client.session_transaction() as s:s.clear()
    assert flask_client.post('/login',data={'password':vector['secret']+'-wrong'}).status_code==200
    assert flask_client.get('/admin/api/health',headers={'X-WAMF-API-Key':vector['secret']+'-wrong'}).status_code==401
    assert flask_client.get('/admin/api/health',headers={'X-WAMF-API-Key':vector['secret']}).status_code==200
    assert flask_client.post('/login',data={'password':vector['secret']}).status_code==302
    with flask_client.session_transaction() as s:csrf=s['csrf_token']
    assert flask_client.post('/admin/config/restart',json={}).status_code==400
    assert before==(path.read_bytes(),path.stat().st_mtime_ns)
    assert len(list(tmp_path.iterdir()))==1
    assert vector['stored'] not in caplog.text and vector['secret'] not in caplog.text
    response=flask_client.post('/admin/password',data={'current_password':vector['secret'],
         'new_password':'synthetic-replacement-password','confirm_password':'synthetic-replacement-password','csrf_token':csrf})
    assert response.status_code==302
    saved=yaml.safe_load(path.read_text())
    assert credentials.verify_credential(saved['admin']['password_hash'],'synthetic-replacement-password')
    assert saved['api']['token_hash']==vector['stored']
    assert saved['admin']['session_secret']=='synthetic-route-secret'


@pytest.mark.parametrize('stored',['unknown$salt$'+'a'*64,'broken',{},['bad']])
def test_invalid_stored_method_never_causes_route_500(stored, flask_client, monkeypatch):
    import webui
    cfg=copy.deepcopy(webui.config)
    cfg['admin']={'auth_enabled':True,'session_secret':'synthetic-secret','password_hash':stored}
    cfg['api']={'token_auth_enabled':True,'token_hash':stored}
    monkeypatch.setattr(webui,'config',cfg)
    monkeypatch.setitem(webui.app.config,'SECRET_KEY','synthetic-secret')
    monkeypatch.setattr(security,'LOGIN_ATTEMPTS',{})
    monkeypatch.setattr(security,'LOGIN_FAILURE_DELAY_SECONDS',0)
    with flask_client.session_transaction() as s:s.clear()
    assert flask_client.get('/admin/api/health',headers={'X-WAMF-API-Key':'synthetic-input'}).status_code==401
    if stored:
        assert flask_client.post('/login',data={'password':'synthetic-input'}).status_code==200
    with flask_client.session_transaction() as s:s['admin_authenticated']=True;s['csrf_token']='synthetic-csrf'
    assert flask_client.post('/admin/password',data={'current_password':'synthetic-input','csrf_token':'synthetic-csrf'}).status_code==200
