"""Connection-peer login throttling and bounded, thread-safe attempt storage."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from unittest.mock import Mock

import pytest
from werkzeug.security import generate_password_hash

from app import security


@pytest.fixture(autouse=True)
def isolated_attempts(monkeypatch):
    monkeypatch.setattr(security, 'LOGIN_ATTEMPTS', {})
    monkeypatch.setattr(security, '_login_overflow_attempts', [])
    monkeypatch.setattr(security, 'LOGIN_FAILURE_DELAY_SECONDS', 0)


@pytest.fixture
def login_client(flask_client, monkeypatch):
    import webui
    from routes import auth

    monkeypatch.setattr(webui, 'config', {**webui.config, 'admin': {
        'auth_enabled': True, 'session_secret': 'synthetic-login-secret',
        'password_hash': generate_password_hash('synthetic-password'),
    }})
    monkeypatch.setitem(webui.app.config, 'SECRET_KEY', 'synthetic-login-secret')
    verify = Mock(wraps=auth.verify_credential)
    monkeypatch.setattr(auth, 'verify_credential', verify)
    with flask_client.session_transaction() as session:
        session.clear()
    return flask_client, verify


@pytest.mark.parametrize('header,template', [
    ('X-Forwarded-For', '198.51.100.{index}'),
    ('X-Forwarded-For', '198.51.100.{index}, 10.0.0.1'),
    ('Forwarded', 'for=198.51.100.{index}'),
    ('X-Real-IP', '198.51.100.{index}'),
])
def test_spoofed_forwarding_headers_cannot_reset_login_bucket(login_client, header, template):
    client, verify = login_client
    for index in range(security.LOGIN_ATTEMPT_LIMIT):
        response = client.post('/login', data={'password': 'wrong'},
            headers={header: template.format(index=index)},
            environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
        assert response.status_code == 200 and b'Login failed.' in response.data
    response = client.post('/login', data={'password': 'synthetic-password'},
        headers={header: template.format(index=99)},
        environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
    assert response.status_code == 200 and b'Login temporarily locked.' in response.data
    assert verify.call_count == security.LOGIN_ATTEMPT_LIMIT
    assert set(security.LOGIN_ATTEMPTS) == {'192.0.2.10'}
    assert len(security.LOGIN_ATTEMPTS['192.0.2.10']) == security.LOGIN_ATTEMPT_LIMIT


@pytest.mark.parametrize('peer', ['192.0.2.10', '2001:db8::123', '10.0.0.1', None])
def test_client_address_is_connection_peer_even_for_proxy_like_addresses(flask_client, peer):
    import webui
    with webui.app.test_request_context('/login', headers={
        'X-Forwarded-For': '198.51.100.10', 'Forwarded': 'for=198.51.100.10',
        'X-Real-IP': '198.51.100.10',
    }, environ_overrides={'REMOTE_ADDR': peer}):
        assert security.get_client_ip() == (peer or 'unknown')


def test_public_base_url_does_not_enable_forwarded_address_trust(login_client, monkeypatch):
    import webui
    monkeypatch.setattr(webui, 'config', {**webui.config,
        'webui': {'public_base_url': 'https://wamf.example'}})
    with webui.app.test_request_context('/login', headers={'X-Forwarded-For': '198.51.100.10'},
            environ_overrides={'REMOTE_ADDR': '10.0.0.1'}):
        assert security.get_client_ip() == '10.0.0.1'


def test_distinct_connection_peers_keep_separate_buckets_and_success_clears_only_own(login_client):
    client, _ = login_client
    for _ in range(security.LOGIN_ATTEMPT_LIMIT):
        client.post('/login', data={'password': 'wrong'}, environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
    response = client.post('/login', data={'password': 'wrong'},
        headers={'X-Forwarded-For': '192.0.2.10'}, environ_overrides={'REMOTE_ADDR': '192.0.2.11'})
    assert b'Login failed.' in response.data
    assert security.is_login_rate_limited('192.0.2.10')
    assert not security.is_login_rate_limited('192.0.2.11')
    response = client.post('/login', data={'password': 'synthetic-password'},
        environ_overrides={'REMOTE_ADDR': '192.0.2.11'})
    assert response.status_code == 302 and response.location == '/admin'
    assert '192.0.2.11' not in security.LOGIN_ATTEMPTS
    assert security.is_login_rate_limited('192.0.2.10')


def test_checks_do_not_allocate_empty_client_buckets():
    for index in range(2000):
        assert not security.is_login_rate_limited(f'client-{index}')
    assert security.LOGIN_ATTEMPTS == {}
    assert security._login_overflow_attempts == []


def test_repeated_failures_cannot_grow_timestamp_list(monkeypatch):
    monkeypatch.setattr(security.time, 'monotonic', lambda: 1000)
    for _ in range(2000):
        security.record_login_failure('192.0.2.10')
    assert len(security.LOGIN_ATTEMPTS['192.0.2.10']) == security.LOGIN_ATTEMPT_LIMIT


def test_capacity_uses_bounded_overflow_without_evicting_active_throttles(monkeypatch):
    monkeypatch.setattr(security, 'LOGIN_CLIENT_LIMIT', 3)
    monkeypatch.setattr(security.time, 'monotonic', lambda: 1000)
    for _ in range(security.LOGIN_ATTEMPT_LIMIT):
        security.record_login_failure('throttled-peer')
    security.record_login_failure('other-peer')
    security.record_login_failure('third-peer')
    for index in range(2000):
        security.record_login_failure(f'new-peer-{index}')
    assert len(security.LOGIN_ATTEMPTS) == 3
    assert set(security.LOGIN_ATTEMPTS) == {'throttled-peer', 'other-peer', 'third-peer'}
    assert all(len(attempts) <= security.LOGIN_ATTEMPT_LIMIT for attempts in security.LOGIN_ATTEMPTS.values())
    assert len(security._login_overflow_attempts) == security.LOGIN_ATTEMPT_LIMIT
    assert security.is_login_rate_limited('throttled-peer')
    assert security.is_login_rate_limited('another-new-peer')
    assert not security.is_login_rate_limited('other-peer')
    security.clear_login_failures('unknown-successful-peer')
    assert security.is_login_rate_limited('another-new-peer')


def test_expired_buckets_and_overflow_are_pruned_at_window_boundary(monkeypatch):
    monkeypatch.setattr(security, 'LOGIN_CLIENT_LIMIT', 2)
    now = [1000]
    monkeypatch.setattr(security.time, 'monotonic', lambda: now[0])
    for _ in range(security.LOGIN_ATTEMPT_LIMIT):
        for peer in ['peer-one', 'peer-two', 'overflow-peer']:
            security.record_login_failure(peer)
    now[0] = 1000 + security.LOGIN_WINDOW_SECONDS - 0.001
    assert security.is_login_rate_limited('peer-one')
    assert security.is_login_rate_limited('overflow-peer')
    now[0] = 1000 + security.LOGIN_WINDOW_SECONDS
    assert not security.is_login_rate_limited('new-peer')
    assert security.LOGIN_ATTEMPTS == {}
    assert security._login_overflow_attempts == []
    security.record_login_failure('new-peer')
    assert set(security.LOGIN_ATTEMPTS) == {'new-peer'}


def test_parallel_failure_recording_stays_bounded(monkeypatch):
    monkeypatch.setattr(security, 'LOGIN_CLIENT_LIMIT', 3)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(security.record_login_failure, [f'peer-{index % 20}' for index in range(400)]))
    assert len(security.LOGIN_ATTEMPTS) == 3
    assert all(len(attempts) <= security.LOGIN_ATTEMPT_LIMIT for attempts in security.LOGIN_ATTEMPTS.values())
    assert len(security._login_overflow_attempts) <= security.LOGIN_ATTEMPT_LIMIT


def test_eight_simultaneous_requests_admit_only_five_and_hash_outside_lock(login_client, monkeypatch):
    import webui
    from routes import auth

    start = Barrier(9)
    admitted_five = Event()
    denied_three = Event()
    release = Event()
    counts_lock = Lock()
    counts = {'admitted': 0, 'denied': 0}

    def verify(*args):
        with counts_lock:
            counts['admitted'] += 1
            if counts['admitted'] == 5:
                admitted_five.set()
        assert release.wait(10), 'Verification was never released'
        return False

    monkeypatch.setattr(auth, 'verify_credential', verify)

    def login(index):
        with webui.app.test_client() as client:
            start.wait(timeout=10)
            response = client.post('/login', data={'password': 'wrong'},
                headers={'X-Forwarded-For': f'198.51.100.{index}'},
                environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
            if b'Login temporarily locked.' in response.data:
                with counts_lock:
                    counts['denied'] += 1
                    if counts['denied'] == 3:
                        denied_three.set()
            return response.get_data(as_text=True)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(login, index) for index in range(8)]
        try:
            start.wait(timeout=10)
            assert admitted_five.wait(5), 'Hashing appears serialized behind the limiter lock'
            assert denied_three.wait(5), 'Excess requests reached verification instead of being denied'
            assert counts == {'admitted': 5, 'denied': 3}
            assert len(security.LOGIN_ATTEMPTS['192.0.2.10']) == 5
            assert all(attempt.pending for attempt in security.LOGIN_ATTEMPTS['192.0.2.10'])
        finally:
            release.set()
        responses = [future.result(timeout=10) for future in futures]
    assert sum('Login failed.' in response for response in responses) == 5
    assert sum('Login temporarily locked.' in response for response in responses) == 3
    assert not any(attempt.pending for attempt in security.LOGIN_ATTEMPTS['192.0.2.10'])


def test_correct_password_is_not_verified_while_all_slots_are_reserved(login_client):
    client, verify = login_client
    reservations = [security.reserve_login_attempt('192.0.2.10') for _ in range(5)]
    try:
        response = client.post('/login', data={'password': 'synthetic-password'},
            environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
        assert response.status_code == 200 and b'Login temporarily locked.' in response.data
        verify.assert_not_called()
    finally:
        for reservation in reservations:
            security.settle_login_attempt(reservation)


@pytest.mark.parametrize('first', ['success', 'failure'])
def test_concurrent_success_does_not_erase_other_pending_or_new_failures(login_client, monkeypatch, first):
    import webui
    from routes import auth

    for _ in range(3):
        security.record_login_failure('192.0.2.10')
    entered = {kind: Event() for kind in ['success', 'failure']}
    release = {kind: Event() for kind in ['success', 'failure']}

    def verify(stored, supplied):
        kind = 'success' if supplied == 'synthetic-password' else 'failure'
        entered[kind].set()
        assert release[kind].wait(10), 'Verification was never released'
        return kind == 'success'

    monkeypatch.setattr(auth, 'verify_credential', verify)

    def login(kind):
        with webui.app.test_client() as client:
            response = client.post('/login', data={
                'password': 'synthetic-password' if kind == 'success' else 'wrong',
            }, environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {kind: executor.submit(login, kind) for kind in entered}
        try:
            assert all(event.wait(5) for event in entered.values())
            assert security.is_login_rate_limited('192.0.2.10')
            release[first].set()
            assert futures[first].result(timeout=5) == (302 if first == 'success' else 200)
            attempts = security.LOGIN_ATTEMPTS['192.0.2.10']
            assert sum(attempt.pending for attempt in attempts) == 1
        finally:
            for event in release.values():
                event.set()
        assert futures['success'].result(timeout=5) == 302
        assert futures['failure'].result(timeout=5) == 200
    attempts = security.LOGIN_ATTEMPTS['192.0.2.10']
    assert len(attempts) == 1 and not attempts[0].pending


def test_verification_exception_settles_reservation_as_failure(login_client, monkeypatch):
    from routes import auth
    client, _ = login_client
    def fail(*args):
        raise RuntimeError('Synthetic verifier failure')
    monkeypatch.setattr(auth, 'verify_credential', fail)
    with pytest.raises(RuntimeError, match='Synthetic verifier failure'):
        client.post('/login', data={'password': 'wrong'}, environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
    attempts = security.LOGIN_ATTEMPTS['192.0.2.10']
    assert len(attempts) == 1 and not attempts[0].pending


@pytest.mark.parametrize('overflow', [False, True])
def test_pending_slots_survive_expiration_and_cleanup_until_settled(monkeypatch, overflow):
    monkeypatch.setattr(security, 'LOGIN_CLIENT_LIMIT', 1)
    now = [1000]
    monkeypatch.setattr(security.time, 'monotonic', lambda: now[0])
    if overflow:
        security.record_login_failure('dedicated-peer')
    reservations = [security.reserve_login_attempt('peer') for _ in range(5)]
    assert all(reservations)
    now[0] += security.LOGIN_WINDOW_SECONDS + 1
    security.clear_login_failures('peer')
    assert security.is_login_rate_limited('peer')
    assert security.reserve_login_attempt('peer') is None
    for reservation in reservations:
        security.settle_login_attempt(reservation)
        security.settle_login_attempt(reservation, success=True)  # Settlement is idempotent.
    assert security.is_login_rate_limited('peer')
    now[0] += security.LOGIN_WINDOW_SECONDS
    assert not security.is_login_rate_limited('peer')


def test_overflow_remains_enforced_after_success_opens_capacity(login_client, monkeypatch):
    import webui
    client, verify = login_client
    monkeypatch.setattr(security, 'LOGIN_CLIENT_LIMIT', 1)
    security.record_login_failure('192.0.2.10')
    for _ in range(5):
        security.record_login_failure('overflow-peer')
    response = client.post('/login', data={'password': 'synthetic-password'},
        environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
    assert response.status_code == 302 and security.LOGIN_ATTEMPTS == {}
    assert security.is_login_rate_limited('overflow-peer')
    with webui.app.test_client() as anonymous:
        response = anonymous.post('/login', data={'password': 'synthetic-password'},
            environ_overrides={'REMOTE_ADDR': '192.0.2.11'})
    assert response.status_code == 200 and b'Login temporarily locked.' in response.data
    assert verify.call_count == 1
    assert security.LOGIN_ATTEMPTS == {}


@pytest.mark.parametrize('failures', [1, 4, 5])
def test_partial_and_full_overflow_do_not_migrate_when_capacity_opens(monkeypatch, failures):
    monkeypatch.setattr(security, 'LOGIN_CLIENT_LIMIT', 1)
    security.record_login_failure('dedicated-peer')
    for _ in range(failures):
        security.record_login_failure('overflow-peer')
    security.clear_login_failures('dedicated-peer')
    reservations = [security.reserve_login_attempt(f'new-peer-{index}') for index in range(5 - failures)]
    assert all(reservation.bucket is security._login_overflow_attempts for reservation in reservations)
    assert security.LOGIN_ATTEMPTS == {}
    assert security.reserve_login_attempt('another-peer') is None
    for reservation in reservations:
        security.settle_login_attempt(reservation, success=True)
    assert len(security._login_overflow_attempts) == failures


def test_staggered_overflow_expiration_does_not_follow_dedicated_expiration(monkeypatch):
    monkeypatch.setattr(security, 'LOGIN_CLIENT_LIMIT', 1)
    now = [1000]
    monkeypatch.setattr(security.time, 'monotonic', lambda: now[0])
    security.record_login_failure('dedicated-peer')
    now[0] = 1100
    for _ in range(5):
        security.record_login_failure('overflow-peer')
    now[0] = 1300  # Dedicated state expired, overflow is only 200 seconds old.
    assert security.is_login_rate_limited('new-peer')
    assert security.LOGIN_ATTEMPTS == {}
    assert security.reserve_login_attempt('new-peer') is None
    now[0] = 1399.999
    assert security.reserve_login_attempt('new-peer') is None
    now[0] = 1400
    reservation = security.reserve_login_attempt('new-peer')
    assert reservation is not None
    assert reservation.bucket is security.LOGIN_ATTEMPTS['new-peer']
    assert security._login_overflow_attempts == []
    security.settle_login_attempt(reservation, success=True)


def test_more_than_1024_peers_stay_bounded_without_resetting_overflow(monkeypatch):
    monkeypatch.setattr(security.time, 'monotonic', lambda: 1000)
    for index in range(1024):
        reservation = security.reserve_login_attempt(f'peer-{index}')
        assert reservation is not None
        security.settle_login_attempt(reservation)
    overflow = [security.reserve_login_attempt(f'extra-{index}') for index in range(5)]
    assert all(overflow)
    assert security.reserve_login_attempt('extra-denied') is None
    assert len(security.LOGIN_ATTEMPTS) == 1024
    assert len(security._login_overflow_attempts) == 5
    security.clear_login_failures('peer-0')
    assert len(security.LOGIN_ATTEMPTS) == 1023
    assert security.reserve_login_attempt('extra-denied') is None
    for reservation in overflow:
        security.settle_login_attempt(reservation)
    assert security.reserve_login_attempt('extra-denied') is None
    assert all(len(attempts) <= 5 for attempts in security.LOGIN_ATTEMPTS.values())


def test_late_success_cannot_erase_failures_admitted_after_an_earlier_success():
    security.record_login_failure('peer')
    early_success = security.reserve_login_attempt('peer')
    late_success = security.reserve_login_attempt('peer')
    security.settle_login_attempt(early_success, success=True)
    new_failure = security.reserve_login_attempt('peer')
    security.settle_login_attempt(new_failure)
    security.settle_login_attempt(late_success, success=True)
    attempts = security.LOGIN_ATTEMPTS['peer']
    assert len(attempts) == 1 and attempts[0] is new_failure.attempt
    security.settle_login_attempt(early_success, success=True)
    assert len(attempts) == 1  # Repeated success settlement cannot clear new state.


def test_overflow_releases_only_expired_slots_then_returns_to_dedicated_capacity(monkeypatch):
    monkeypatch.setattr(security, 'LOGIN_CLIENT_LIMIT', 1)
    now = [1000]
    monkeypatch.setattr(security.time, 'monotonic', lambda: now[0])
    security.record_login_failure('dedicated-peer')
    for index in range(5):
        now[0] = 1100 + index
        security.record_login_failure('overflow-peer')
    now[0] = 1400
    reservation = security.reserve_login_attempt('new-peer')
    assert reservation is not None and reservation.bucket is security._login_overflow_attempts
    assert security.LOGIN_ATTEMPTS == {}
    assert security.reserve_login_attempt('another-peer') is None
    security.settle_login_attempt(reservation, success=True)
    assert len(security._login_overflow_attempts) == 4
    now[0] = 1404
    reservation = security.reserve_login_attempt('new-peer')
    assert reservation is not None and reservation.bucket is security.LOGIN_ATTEMPTS['new-peer']
    assert security._login_overflow_attempts == []
    security.settle_login_attempt(reservation, success=True)


@pytest.mark.parametrize('peer,forwarded,expected', [
    ('10.0.0.2', '198.51.100.9', '198.51.100.9'),
    ('10.0.0.2', '192.0.2.99, 198.51.100.9', '198.51.100.9'),
    ('10.0.0.2', '198.51.100.9, 10.0.0.3', '198.51.100.9'),
    ('2001:db8:1::2', '2001:db8:2::9', '2001:db8:2::9'),
    ('192.0.2.10', '198.51.100.9', '192.0.2.10'),
    ('10.0.0.2', 'malformed, 198.51.100.9', '10.0.0.2'),
    ('10.0.0.2', '', '10.0.0.2'),
    ('10.0.0.2', ',198.51.100.9', '10.0.0.2'),
    ('10.0.0.2', ','.join(['198.51.100.9'] * 33), '10.0.0.2'),
    ('10.0.0.2', 'x' * 4097, '10.0.0.2'),
])
def test_explicit_proxy_trust_walks_only_trusted_suffix(flask_client, monkeypatch, peer, forwarded, expected):
    import webui
    monkeypatch.setitem(webui.app.config, 'WAMF_TRUSTED_PROXY_CIDRS',
                        ['10.0.0.0/24', '2001:db8:1::/64'])
    with webui.app.test_request_context('/login', headers={'X-Forwarded-For': forwarded},
                                       environ_overrides={'REMOTE_ADDR': peer}):
        assert security.get_client_ip() == expected


@pytest.mark.parametrize('configured', ['10.0.0.0/24', ['bad-network'], [None], None, []])
def test_invalid_proxy_trust_falls_back_to_peer(flask_client, monkeypatch, configured):
    import webui
    monkeypatch.setitem(webui.app.config, 'WAMF_TRUSTED_PROXY_CIDRS', configured)
    with webui.app.test_request_context('/login', headers={'X-Forwarded-For': '198.51.100.9'},
                                       environ_overrides={'REMOTE_ADDR': '10.0.0.2'}):
        assert security.get_client_ip() == '10.0.0.2'


def test_trusted_proxy_cannot_reset_client_bucket_with_spoofed_prefix(login_client, monkeypatch):
    import webui
    client, verify = login_client
    monkeypatch.setitem(webui.app.config, 'WAMF_TRUSTED_PROXY_CIDRS', ['10.0.0.0/24'])
    for index in range(security.LOGIN_ATTEMPT_LIMIT + 1):
        response = client.post('/login', data={'password': 'wrong'},
            headers={'X-Forwarded-For': f'192.0.2.{index}, 198.51.100.9'},
            environ_overrides={'REMOTE_ADDR': '10.0.0.2'})
    assert b'Login temporarily locked.' in response.data
    assert verify.call_count == security.LOGIN_ATTEMPT_LIMIT
    assert set(security.LOGIN_ATTEMPTS) == {'198.51.100.9'}
