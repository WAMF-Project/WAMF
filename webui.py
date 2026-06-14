from flask import Flask, request, redirect, url_for, jsonify, session, flash
import logging
from datetime import datetime
import yaml
from pathlib import Path
from app import security
from app.queries import recent_detections, get_daily_summary, get_common_name, get_records_for_date_hour
from app.queries import get_records_for_scientific_name_and_date, get_earliest_detection_date
from app.queries import get_activity_by_hour, get_top_species, get_latest_visitor, get_species_peak_hours, get_species_stats
from app.queries import get_species_activity_by_hour, get_admin_stats, get_recent_system_events, get_retention_status
from app.queries import get_species_info, get_all_species_info, get_detection_count_for_scientific_name_and_date
from app.queries import get_species_stats_for_date
from app.health import get_system_health
from version import VERSION
from app.metadata_tasks import (
    queue_metadata_refresh,
    refresh_species_metadata as refresh_species_metadata_task,
    species_needs_metadata,
)
from app.db import (
    ensure_schema,
    DB_PATH as DEFAULT_DB_PATH,
    NAMES_DB_PATH as DEFAULT_NAMES_DB_PATH,
)
from app.config_editor import (
    get_config_path,
    update_admin_password_hash,
    update_api_token_hash,
    write_config_preserving_admin,
)

app = Flask(__name__)
logger = logging.getLogger(__name__)
config = None
DBPATH = DEFAULT_DB_PATH
NAMEDBPATH = DEFAULT_NAMES_DB_PATH
LOGIN_ATTEMPTS = security.LOGIN_ATTEMPTS
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
    return security.is_admin_auth_enabled(config)


def get_admin_password_hash():
    return security.get_admin_password_hash(config)


def get_admin_session_secret():
    return security.get_admin_session_secret(config)


def get_api_config():
    return security.get_api_config(config)


def is_api_token_auth_enabled():
    return security.is_api_token_auth_enabled(config)


def get_api_token_hash():
    return security.get_api_token_hash(config)


def is_valid_api_token():
    return security.is_valid_api_token(config)


def admin_api_unauthorized_response():
    return jsonify({
        'success': False,
        'message': 'Authentication required.',
    }), 401


def get_admin_auth_config_error():
    return security.get_admin_auth_config_error(config)


def configure_session_secret():
    security.configure_session_secret(app, config)


def get_client_ip():
    return security.get_client_ip()


def is_login_rate_limited(client_ip):
    return security.is_login_rate_limited(client_ip)


def record_login_failure(client_ip):
    security.record_login_failure(client_ip)


def clear_login_failures(client_ip):
    security.clear_login_failures(client_ip)


def delay_login_failure():
    security.delay_login_failure()


def get_csrf_token():
    return security.get_csrf_token(app.secret_key)


def get_request_csrf_token():
    return security.get_request_csrf_token()


def is_valid_csrf_token(token):
    return security.is_valid_csrf_token(token, app.secret_key)


def csrf_failure_response():
    if request.path.startswith('/admin/config/save') or request.path.startswith('/detections/'):
        return jsonify({
            'success': False,
            'message': 'Security token expired. Refresh the page and try again.',
        }), 400

    flash('Security token expired. Refresh the page and try again.')
    return redirect(url_for('admin.admin_dashboard'))


def is_safe_next_url(next_url):
    return security.is_safe_next_url(next_url)


def get_safe_next_url(default_endpoint='admin.admin_dashboard'):
    return security.get_safe_next_url(default_endpoint)


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
