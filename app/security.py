"""Single-admin auth, API token, next-url, and CSRF helpers."""

import hmac
import os
import secrets
import time
from urllib.parse import urlsplit

from flask import request, session, url_for
from werkzeug.security import check_password_hash


LOGIN_ATTEMPTS = {}
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_WINDOW_SECONDS = 300
LOGIN_FAILURE_DELAY_SECONDS = 0.35


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
        and check_password_hash(token_hash, token)
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
    forwarded_for = request.headers.get('X-Forwarded-For', '')

    if forwarded_for:
        return forwarded_for.split(',')[0].strip()

    return request.remote_addr or 'unknown'


def is_login_rate_limited(client_ip):
    now = time.monotonic()
    attempts = [
        timestamp
        for timestamp in LOGIN_ATTEMPTS.get(client_ip, [])
        if now - timestamp < LOGIN_WINDOW_SECONDS
    ]
    LOGIN_ATTEMPTS[client_ip] = attempts
    return len(attempts) >= LOGIN_ATTEMPT_LIMIT


def record_login_failure(client_ip):
    now = time.monotonic()
    attempts = [
        timestamp
        for timestamp in LOGIN_ATTEMPTS.get(client_ip, [])
        if now - timestamp < LOGIN_WINDOW_SECONDS
    ]
    attempts.append(now)
    LOGIN_ATTEMPTS[client_ip] = attempts


def clear_login_failures(client_ip):
    LOGIN_ATTEMPTS.pop(client_ip, None)


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
