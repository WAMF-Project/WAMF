from flask import Flask, render_template, request, redirect, url_for, send_file, abort, jsonify, session, flash
import os
import hmac
import logging
import secrets
import time
import sqlite3
import base64
from datetime import datetime, date
from urllib.parse import urlsplit
import yaml
from io import BytesIO
from pathlib import Path
from werkzeug.security import check_password_hash, generate_password_hash
from app.queries import recent_detections, get_daily_summary, get_common_name, get_records_for_date_hour
from app.queries import get_records_for_scientific_name_and_date, get_earliest_detection_date
from app.queries import get_activity_by_hour, get_top_species, get_latest_visitor, get_species_peak_hours, get_species_stats
from app.queries import get_species_activity_by_hour, get_admin_stats, get_recent_system_events, get_retention_status
from app.queries import get_species_info, get_all_species_info, get_detection_count_for_scientific_name_and_date
from app.queries import get_species_stats_for_date
from app.health import get_system_health
from version import VERSION
from app.system_events import log_system_event
from app.frigate_proxy import proxy_frigate_media
from app.metadata_tasks import (
    queue_metadata_refresh,
    refresh_species_metadata as refresh_species_metadata_task,
    species_needs_metadata,
)
from app.db import (
    connect_db,
    ensure_schema,
    DB_PATH as DEFAULT_DB_PATH,
    NAMES_DB_PATH as DEFAULT_NAMES_DB_PATH,
)
from app.config_editor import (
    get_config_file_metadata,
    get_config_path,
    strip_admin_config_block,
    update_admin_password_hash,
    update_api_token_hash,
    write_config_preserving_admin,
)

app = Flask(__name__)
logger = logging.getLogger(__name__)
config = None
DBPATH = DEFAULT_DB_PATH
NAMEDBPATH = DEFAULT_NAMES_DB_PATH
LOGIN_ATTEMPTS = {}
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_WINDOW_SECONDS = 300
LOGIN_FAILURE_DELAY_SECONDS = 0.35
CSRF_PROTECTED_ENDPOINTS = {
    'admin.save_config',
    'admin.change_password',
    'admin.admin_api_token',
    'admin.refresh_species',
    'admin.refresh_missing_species',
    'admin.refresh_all_species',
    'detections.delete_detection',
}


def is_admin_auth_enabled():
    admin_config = (config or {}).get('admin', {})
    return bool(admin_config.get('auth_enabled', False))


def get_admin_password_hash():
    admin_config = (config or {}).get('admin', {})
    return admin_config.get('password_hash')


def get_admin_session_secret():
    admin_config = (config or {}).get('admin', {})
    return os.environ.get('WAMF_SECRET_KEY') or admin_config.get('session_secret')


def get_api_config():
    return (config or {}).get('api', {})


def is_api_token_auth_enabled():
    return bool(get_api_config().get('token_auth_enabled', False))


def get_api_token_hash():
    return get_api_config().get('token_hash')


def is_valid_api_token():
    token = request.headers.get('X-WAMF-API-Key', '')
    token_hash = get_api_token_hash()

    return bool(
        token
        and token_hash
        and check_password_hash(token_hash, token)
    )


def admin_api_unauthorized_response():
    return jsonify({
        'success': False,
        'message': 'Authentication required.',
    }), 401


def get_admin_auth_config_error():
    if not is_admin_auth_enabled():
        return None

    if not get_admin_session_secret():
        return 'Admin authentication requires admin.session_secret or WAMF_SECRET_KEY.'

    if not get_admin_password_hash():
        return 'Admin authentication is enabled, but no password hash is configured.'

    return None


def configure_session_secret():
    admin_config = (config or {}).get('admin', {})
    secret_key = get_admin_session_secret()

    app.secret_key = secret_key
    app.config.update(
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


def get_csrf_token():
    if not app.secret_key:
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


def is_valid_csrf_token(token):
    expected = session.get('csrf_token') if app.secret_key else None
    return bool(
        token
        and expected
        and hmac.compare_digest(token, expected)
    )


def csrf_failure_response():
    if request.path.startswith('/admin/config/save') or request.path.startswith('/detections/'):
        return jsonify({
            'success': False,
            'message': 'Security token expired. Refresh the page and try again.',
        }), 400

    flash('Security token expired. Refresh the page and try again.')
    return redirect(url_for('admin.admin_dashboard'))


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


def is_wamf_media_path(path):
    if not path:
        return False

    try:
        media_path = Path(path).resolve()
        allowed_dirs = (
            Path('media/wamf/snapshots').resolve(),
            Path('media/wamf/clips').resolve(),
        )
        return any(
            media_path == allowed_dir or allowed_dir in media_path.parents
            for allowed_dir in allowed_dirs
        )
    except OSError:
        return False


def delete_wamf_media_files(*paths):
    deleted = []

    for media_path in paths:
        if not is_wamf_media_path(media_path):
            continue

        path = Path(media_path)

        if not path.exists():
            continue

        if not path.is_file():
            continue

        path.unlink()
        deleted.append(str(path))

    return deleted


@app.before_request
def require_admin_auth():
    if request.path.startswith('/admin/api/'):
        if session.get('admin_authenticated') or is_valid_api_token():
            return None

        if is_admin_auth_enabled() or is_api_token_auth_enabled():
            return admin_api_unauthorized_response()

        return None

    if not is_admin_auth_enabled():
        return None

    if request.path != '/admin' and not request.path.startswith('/admin/'):
        return None

    if session.get('admin_authenticated'):
        return None

    login_url = url_for('auth.login', next=request.full_path.rstrip('?'))
    return redirect(login_url)


@app.before_request
def require_csrf_for_mutations():
    if not is_admin_auth_enabled():
        return None

    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return None

    if request.endpoint not in CSRF_PROTECTED_ENDPOINTS:
        return None

    if not session.get('admin_authenticated'):
        if request.endpoint == 'detections.delete_detection':
            return jsonify({
                'success': False,
                'message': 'Admin login required.',
            }), 401

        return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))

    if not is_valid_csrf_token(get_request_csrf_token()):
        return csrf_failure_response()

    return None


def format_datetime(value, format='%B %d, %Y %H:%M:%S'):
    dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f')
    return dt.strftime(format)


app.jinja_env.filters['datetime'] = format_datetime

@app.context_processor
def inject_admin_status():

    context = {
        "version": VERSION,
        "csrf_token": get_csrf_token() if app.secret_key else ""
    }

    endpoint = request.endpoint or ""

    admin_template_endpoints = {
        'admin.admin_dashboard',
        'admin.admin_logs',
        'admin.admin_species',
        'admin.admin_config',
        'admin.admin_api_token',
        'admin.change_password',
    }

    if endpoint in admin_template_endpoints:
        context.update({
            "health": get_system_health(),
            "retention_status": get_retention_status()
        })

    return context

def load_config():
    global config

    with open(get_config_path(), 'r') as config_file:
        config = yaml.safe_load(config_file) or {}

    configure_session_secret()


def register_blueprints(flask_app):
    from routes.admin import admin_bp
    from routes.admin_api import admin_api_bp
    from routes.api import api_bp
    from routes.auth import auth_bp
    from routes.detections import detections_bp
    from routes.media import media_bp
    from routes.public import public_bp

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(public_bp)
    flask_app.register_blueprint(media_bp)
    flask_app.register_blueprint(api_bp)
    flask_app.register_blueprint(detections_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(admin_api_bp)


register_blueprints(app)
load_config()
ensure_schema(DBPATH)
