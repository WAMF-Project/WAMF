"""Single-admin auth, API token, next-url, and CSRF helpers."""

import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

from flask import current_app, request, session, url_for

from app.credentials import verify_credential

LOGIN_ATTEMPTS = {}
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_WINDOW_SECONDS = 300
LOGIN_FAILURE_DELAY_SECONDS = 0.35
LOGIN_CLIENT_LIMIT = 1024
_login_attempts_lock = threading.Lock()
_login_overflow_attempts = []


@dataclass(eq=False, slots=True)
class _LoginAttempt:
    timestamp: float
    pending: bool = False


@dataclass(eq=False, frozen=True, slots=True)
class _LoginReservation:
    client_ip: str
    bucket: list
    attempt: _LoginAttempt
    resettable: tuple


def get_admin_config(config):
    return (config or {}).get('admin', {})


def get_api_config(config):
    return (config or {}).get('api', {})


def is_admin_auth_enabled(config):
    return bool(get_admin_config(config).get('auth_enabled', False))


def get_admin_password_hash(config):
    return get_admin_config(config).get('password_hash')


def get_admin_session_secret(config):
    return (
        os.environ.get('WAMF_SECRET_KEY')
        or get_admin_config(config).get('session_secret')
    )


def is_api_token_auth_enabled(config):
    return bool(get_api_config(config).get('token_auth_enabled', False))


def get_api_token_hash(config):
    return get_api_config(config).get('token_hash')


def is_valid_api_token(config):
    token = request.headers.get('X-WAMF-API-Key', '')
    token_hash = get_api_token_hash(config)

    return bool(
        token
        and token_hash
        and verify_credential(token_hash, token, for_api=True)
    )


def get_admin_auth_config_error(config):
    if not is_admin_auth_enabled(config):
        return None

    if not get_admin_session_secret(config):
        return 'Admin authentication requires admin.session_secret or WAMF_SECRET_KEY.'

    if not get_admin_password_hash(config):
        return 'Admin authentication is enabled, but no password hash is configured.'

    return None


def configure_session_secret(flask_app, config):
    admin_config = get_admin_config(config)
    secret_key = get_admin_session_secret(config)

    flask_app.secret_key = secret_key
    flask_app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=admin_config.get('session_cookie_samesite', 'Lax'),
        SESSION_COOKIE_SECURE=bool(admin_config.get('session_cookie_secure', False)),
    )


def get_client_ip():
    """Trust forwarding only from explicitly configured proxy networks.

    WAMF_TRUSTED_PROXY_CIDRS is an application deployment setting (default: no
    trusted proxies), not inferred from private addresses or public_base_url.
    Each trusted proxy must append its actual peer to X-Forwarded-For. Walk
    right-to-left and stop at the first untrusted hop; never trust the leftmost
    value supplied by an arbitrary client. Do not combine with ProxyFix.
    """
    peer = request.remote_addr or 'unknown'
    configured = current_app.config.get('WAMF_TRUSTED_PROXY_CIDRS', ())
    if not isinstance(configured, (tuple, list)) or not configured:
        return peer
    try:
        networks = tuple(ip_network(value) for value in configured)
        address = ip_address(peer)
        if not any(address in network for network in networks):
            return peer
        forwarded = request.headers.get('X-Forwarded-For', '')
        if not forwarded or len(forwarded) > 4096:
            return peer
        hops = forwarded.split(',')
        if len(hops) > 32:
            return peer
        chain = [ip_address(hop.strip()) for hop in hops]
        for hop in reversed(chain):
            if not any(address in network for network in networks):
                break
            address = hop
        return str(address)
    except (ValueError, TypeError):
        # Invalid trust settings or forwarded syntax must never bypass the peer
        # bucket. No header, address, or configuration value is logged.
        return peer


def _prune_login_attempts(now):
    """Caller holds the lock; never retain empty or expired client buckets."""
    for client_ip, attempts in list(LOGIN_ATTEMPTS.items()):
        recent = [attempt for attempt in attempts
                  if attempt.pending or now - attempt.timestamp < LOGIN_WINDOW_SECONDS]
        if recent:
            attempts[:] = recent[-LOGIN_ATTEMPT_LIMIT:]
        else:
            LOGIN_ATTEMPTS.pop(client_ip, None)
    _login_overflow_attempts[:] = [
        attempt for attempt in _login_overflow_attempts
        if attempt.pending or now - attempt.timestamp < LOGIN_WINDOW_SECONDS
    ][-LOGIN_ATTEMPT_LIMIT:]


def _login_bucket(client_ip, create=False):
    """Caller holds the lock and has pruned expired attempts."""
    attempts = LOGIN_ATTEMPTS.get(client_ip)
    if attempts is not None:
        return attempts
    # Overflow membership cannot be tracked with an unbounded peer map. Keep
    # all untracked peers in the shared bucket until its active state drains.
    if _login_overflow_attempts or len(LOGIN_ATTEMPTS) >= LOGIN_CLIENT_LIMIT:
        return _login_overflow_attempts
    if create:
        return LOGIN_ATTEMPTS.setdefault(client_ip, [])
    return []


def reserve_login_attempt(client_ip):
    """Atomically admit an attempt; pending reservations never expire early."""
    with _login_attempts_lock:
        now = time.monotonic()
        _prune_login_attempts(now)
        bucket = _login_bucket(client_ip, create=True)
        if len(bucket) >= LOGIN_ATTEMPT_LIMIT:
            return None
        resettable = (
            tuple(attempt for attempt in bucket if not attempt.pending)
            if bucket is not _login_overflow_attempts else ()
        )
        attempt = _LoginAttempt(now, pending=True)
        bucket.append(attempt)
        return _LoginReservation(client_ip, bucket, attempt, resettable)


def settle_login_attempt(reservation, success=False):
    """Settle once; a success must not erase concurrently reserved attempts."""
    with _login_attempts_lock:
        attempt = reservation.attempt
        if not attempt.pending:
            return
        now = time.monotonic()
        _prune_login_attempts(now)
        attempt.pending = False
        if success:
            reservation.bucket[:] = [entry for entry in reservation.bucket
                                     if entry is not attempt and entry not in reservation.resettable]
            if not reservation.bucket and LOGIN_ATTEMPTS.get(reservation.client_ip) is reservation.bucket:
                LOGIN_ATTEMPTS.pop(reservation.client_ip, None)
        else:
            # Failures count for a full window from verification completion.
            attempt.timestamp = now


def is_login_rate_limited(client_ip):
    with _login_attempts_lock:
        _prune_login_attempts(time.monotonic())
        return len(_login_bucket(client_ip)) >= LOGIN_ATTEMPT_LIMIT


def record_login_failure(client_ip):
    with _login_attempts_lock:
        now = time.monotonic()
        _prune_login_attempts(now)
        attempts = _login_bucket(client_ip, create=True)
        if len(attempts) < LOGIN_ATTEMPT_LIMIT:
            attempts.append(_LoginAttempt(now))


def clear_login_failures(client_ip):
    with _login_attempts_lock:
        attempts = LOGIN_ATTEMPTS.get(client_ip)
        if attempts is not None:
            attempts[:] = [attempt for attempt in attempts if attempt.pending]
            if not attempts:
                LOGIN_ATTEMPTS.pop(client_ip, None)
        # A successful login must not clear failures shared by other clients
        # in the overflow bucket.


def delay_login_failure():
    time.sleep(LOGIN_FAILURE_DELAY_SECONDS)


def get_csrf_token(secret_key):
    if not secret_key:
        return ''

    token = session.get('csrf_token')

    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token

    return token


def get_request_csrf_token():
    return (
        request.headers.get('X-CSRFToken')
        or request.headers.get('X-CSRF-Token')
        or request.form.get('csrf_token')
    )


def is_valid_csrf_token(token, secret_key):
    expected = session.get('csrf_token') if secret_key else None
    return bool(
        token
        and expected
        and hmac.compare_digest(token, expected)
    )


def is_safe_next_url(next_url):
    if not next_url:
        return False

    parsed = urlsplit(next_url)
    return (
        not parsed.scheme
        and not parsed.netloc
        and next_url.startswith('/')
        and not next_url.startswith('//')
    )


def get_safe_next_url(default_endpoint='admin.admin_dashboard'):
    next_url = request.args.get('next') or request.form.get('next')

    if is_safe_next_url(next_url):
        return next_url

    return url_for(default_endpoint)
