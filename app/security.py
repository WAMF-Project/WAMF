"""Single-admin auth, API token, next-url, and CSRF helpers."""

import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from flask import request, session, url_for

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
    # No trusted proxy middleware is configured. HTTP forwarding headers are
    # caller-controlled, not evidence of the connection's client address.
    return request.remote_addr or 'unknown'


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
    """Allow only internal absolute paths, without browser/parser ambiguities."""
    if not isinstance(next_url, str) or not next_url:
        return False

    # Reject literal controls before urlsplit can strip or normalize them.
    if '\\' in next_url or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in next_url
    ):
        return False

    try:
        parsed = urlsplit(next_url)
    except ValueError:
        return False

    if (
        parsed.scheme
        or parsed.netloc
        or not next_url.startswith('/')
        or next_url.startswith('//')
    ):
        return False

    # Inspect only the path through every encoding layer. Query/fragment escapes
    # are data, and get_safe_next_url must return the original destination.
    path = parsed.path
    while True:
        if (
            path.startswith('//')
            or '\\' in path
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)
        ):
            return False

        decoded = unquote(path)
        if decoded == path:
            return True
        path = decoded


def get_safe_next_url(default_endpoint='admin.admin_dashboard'):
    next_url = request.args.get('next') or request.form.get('next')

    if is_safe_next_url(next_url):
        return next_url

    return url_for(default_endpoint)
