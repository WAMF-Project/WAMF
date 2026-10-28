"""Exercise validated login redirects with authentication enabled or disabled."""

from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from werkzeug.datastructures import MultiDict

from app import security


@pytest.mark.parametrize('query,form,expected', [
    ([], [], '/admin'),
    ([('next', '')], [('next', '/activity')], '/activity'),
    ([('next', '/recent')], [('next', '//evil.example')], '/recent'),
    ([('next', '//evil.example')], [('next', '/recent')], '/admin'),
    ([('next', '//evil.example'), ('next', '/recent')], [], '/admin'),
    ([('next', '/recent'), ('next', '//evil.example')], [], '/recent'),
    ([], [('next', '//evil.example'), ('next', '/recent')], '/admin'),
    ([], [('next', '/recent'), ('next', '//evil.example')], '/recent'),
    ([('next', '')], [('next', '')], '/admin'),
])
def test_safe_next_parameter_precedence_and_duplicates(flask_client, query, form, expected):
    import webui

    with webui.app.test_request_context('/login', method='POST', query_string=MultiDict(query),
                                       data=MultiDict(form)):
        assert webui.get_safe_next_url() == expected


def test_default_endpoint_and_host_headers_do_not_select_redirect_origin(flask_client):
    import webui

    for destination in ('', '//evil.example', 'https://wamf.example@evil.example/admin'):
        with webui.app.test_request_context('/login', query_string={'next': destination},
                                           headers={'Host': 'evil.example',
                                                    'X-Forwarded-Host': 'another.example',
                                                    'X-Forwarded-Proto': 'https'}):
            assert webui.get_safe_next_url() == '/admin'
            assert webui.get_safe_next_url('public.index') == '/'


@pytest.fixture
def login_redirect_client(flask_client, monkeypatch):
    import routes.auth as auth
    import webui

    monkeypatch.setattr(webui, 'config', {**webui.config, 'admin': {
        'auth_enabled': True, 'session_secret': 'test-secret',
        'password_hash': 'synthetic-password-hash',
    }})
    monkeypatch.setitem(webui.app.config, 'SECRET_KEY', 'test-secret')
    monkeypatch.setattr(security, 'LOGIN_ATTEMPTS', {})
    monkeypatch.setattr(security, '_login_overflow_attempts', [])
    verify = Mock(return_value=True)
    monkeypatch.setattr(auth, 'verify_credential', verify)
    with flask_client.session_transaction() as session:
        session.clear()
    return flask_client, verify


@pytest.mark.parametrize('flow', ['login_post', 'authenticated', 'auth_disabled'])
@pytest.mark.parametrize('source', ['query', 'form'])
@pytest.mark.parametrize('destination,expected', [
    ('https://evil.example/admin', '/admin'),
    ('https://localhost@evil.example/admin', '/admin'),
    ('https://localhost.evil.example/admin', '/admin'),
    ('//localhost@evil.example', '/admin'),
    ('//[::1]@evil.example', '/admin'),
    ('https:evil.example', '/admin'),
    ('hTtPs:///evil.example', '/admin'),
    ('JaVaScRiPt:alert(1)', '/admin'),
    ('data:text/html,unsafe', '/admin'),
    ('vbscript:msgbox(1)', '/admin'),
    ('file:///etc/passwd', '/admin'),
    (' /admin', '/admin'),
    ('\t/admin', '/admin'),
    ('\r\n//evil.example', '/admin'),
    ('/admin\x00', '/admin'),
    ('/admin\x85', '/admin'),
    ('\\/evil.example', '/admin'),
    ('/\\evil.example', '/admin'),
    ('/%2f%5cevil.example', '/admin'),
    ('/%252f%255cevil.example', '/admin'),
    ('/%25%32%46evil.example', '/admin'),
    ('/%250d%250aLocation%253Aevil.example', '/admin'),
    ('http://[', '/admin'),
    ('//[invalid', '/admin'),
    ('/admin/api/health?check=1', '/admin/api/health?check=1'),
    ('/daily_summary/2024-06-01?embed=ha#bird', '/daily_summary/2024-06-01?embed=ha#bird'),
    ('/admin?next=//evil.example', '/admin?next=//evil.example'),
    ('/admin?next=https://localhost@evil.example', '/admin?next=https://localhost@evil.example'),
    ('/admin?value=%0D%0A&tag=one&tag=two&empty=&flag',
     '/admin?value=%0D%0A&tag=one&tag=two&empty=&flag'),
    ('/admin?next=%252F%252Fevil.example#value=%255C',
     '/admin?next=%252F%252Fevil.example#value=%255C'),
    ('/media/blue%2520tit.jpg', '/media/blue%2520tit.jpg'),
])
def test_login_redirects_only_to_validated_internal_destination(
    login_redirect_client, monkeypatch, flow, source, destination, expected
):
    import webui

    client, verify = login_redirect_client
    authenticated = flow == 'authenticated'
    if flow == 'auth_disabled':
        monkeypatch.setitem(webui.config['admin'], 'auth_enabled', False)
    validate = Mock(wraps=security.is_safe_next_url)
    monkeypatch.setattr(security, 'is_safe_next_url', validate)
    data = {'password': 'synthetic-password'}
    query = {}
    (query if source == 'query' else data)['next'] = destination
    if authenticated:
        with client.session_transaction() as session:
            session['admin_authenticated'] = True
            session['csrf_token'] = 'existing-session-token'
    if flow == 'login_post':
        response = client.post('/login', query_string=query, data=data)
        verify.assert_called_once_with('synthetic-password-hash', 'synthetic-password')
    else:
        response = (client.get('/login', query_string=query) if source == 'query'
                    else client.post('/login', data=data))
        verify.assert_not_called()
    validate.assert_called_once_with(destination)
    assert response.status_code == 302
    assert response.headers['Location'] == expected
    target = urlsplit(response.headers['Location'])
    assert target.scheme == target.netloc == ''
    assert response.headers['Location'].startswith('/')
    assert not response.headers['Location'].startswith('//')
    with client.session_transaction() as session:
        if flow == 'auth_disabled':
            assert not session.get('admin_authenticated')
            assert 'csrf_token' not in session
        else:
            assert session['admin_authenticated'] is True
            assert session['csrf_token']
            if authenticated:
                assert session['csrf_token'] == 'existing-session-token'


@pytest.mark.parametrize('flow', ['authenticated', 'auth_disabled'])
@pytest.mark.parametrize('headers', [
    {'Host': 'evil.example'},
    {'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Proto': 'https'},
    {'X-Forwarded-Prefix': '//evil.example'},
    {'Forwarded': 'host=evil.example;proto=https'},
])
@pytest.mark.parametrize('source', ['query', 'form'])
@pytest.mark.parametrize('destination,expected', [
    ('https://evil.example/admin', '/admin'),
    ('/daily_summary/2024-06-01?embed=ha', '/daily_summary/2024-06-01?embed=ha'),
])
def test_early_login_redirect_ignores_host_and_forwarding_headers(
    login_redirect_client, monkeypatch, flow, headers, source, destination, expected
):
    import webui

    client, verify = login_redirect_client
    if flow == 'auth_disabled':
        monkeypatch.setitem(webui.config['admin'], 'auth_enabled', False)
    validate = Mock(wraps=security.is_safe_next_url)
    monkeypatch.setattr(security, 'is_safe_next_url', validate)
    base = 'http://' + headers.get('Host', 'localhost')
    # Set session state for the tested Host to exercise the intended branch even
    # when the request Host differs from localhost.
    with client.session_transaction(base_url=base) as session:
        session.clear()
        if flow == 'authenticated':
            session['admin_authenticated'] = True
    if source == 'query':
        response = client.get('/login', base_url=base, headers=headers,
                              query_string={'next': destination})
    else:
        response = client.post('/login', base_url=base, headers=headers,
                               data={'next': destination})
    assert response.status_code == 302
    assert response.headers['Location'] == expected
    validate.assert_called_once_with(destination)
    verify.assert_not_called()


def test_auth_disabled_without_next_validates_missing_input(login_redirect_client, monkeypatch):
    import webui

    client, verify = login_redirect_client
    monkeypatch.setitem(webui.config['admin'], 'auth_enabled', False)
    validate = Mock(wraps=security.is_safe_next_url)
    monkeypatch.setattr(security, 'is_safe_next_url', validate)
    response = client.get('/login')
    assert response.status_code == 302
    assert response.headers['Location'] == '/admin'
    validate.assert_called_once_with(None)
    verify.assert_not_called()
