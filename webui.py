from flask import Flask, render_template, request, redirect, url_for, send_file, abort, send_from_directory, jsonify, session, flash
import os
import hmac
import re
import secrets
import time
import sqlite3
import base64
from datetime import datetime, date
from urllib.parse import urlsplit
import yaml
import requests
from io import BytesIO
from pathlib import Path
from werkzeug.security import check_password_hash, generate_password_hash
from queries import recent_detections, get_daily_summary, get_common_name, get_records_for_date_hour
from queries import get_records_for_scientific_name_and_date, get_earliest_detection_date
from queries import get_activity_by_hour, get_top_species, get_latest_visitor, get_species_peak_hours, get_species_stats
from queries import get_species_activity_by_hour, get_admin_stats, get_recent_system_events, get_retention_status
from queries import get_species_info, save_species_info, get_all_species_info, get_detection_count_for_scientific_name_and_date
from queries import get_species_stats_for_date
from health import get_system_health
from version import VERSION
from species_metadata import fetch_species_metadata
from system_events import log_system_event
import shutil
import glob

app = Flask(__name__)
config = None
DBPATH = './data/speciesid.db'
NAMEDBPATH = './birdnames.db'
LOGIN_ATTEMPTS = {}
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_WINDOW_SECONDS = 300
LOGIN_FAILURE_DELAY_SECONDS = 0.35
CSRF_PROTECTED_ENDPOINTS = {
    'save_config',
    'change_password',
    'refresh_species',
    'refresh_missing_species',
    'refresh_all_species',
    'delete_detection',
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
    return redirect(url_for('admin_dashboard'))


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


def get_safe_next_url(default_endpoint='admin_dashboard'):
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
    if not is_admin_auth_enabled():
        return None

    if request.path != '/admin' and not request.path.startswith('/admin/'):
        return None

    if session.get('admin_authenticated'):
        return None

    login_url = url_for('login', next=request.full_path.rstrip('?'))
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
        if request.endpoint == 'delete_detection':
            return jsonify({
                'success': False,
                'message': 'Admin login required.',
            }), 401

        return redirect(url_for('login', next=request.full_path.rstrip('?')))

    if not is_valid_csrf_token(get_request_csrf_token()):
        return csrf_failure_response()

    return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_admin_auth_enabled():
        return redirect(get_safe_next_url())

    if session.get('admin_authenticated'):
        return redirect(get_safe_next_url())

    config_error = get_admin_auth_config_error()

    if config_error:
        return render_template(
            'login.html',
            error=config_error,
            next_url=get_safe_next_url()
        ), 500

    error = None

    if request.method == 'POST':
        client_ip = get_client_ip()

        if is_login_rate_limited(client_ip):
            delay_login_failure()
            error = 'Login temporarily locked. Try again later.'
        else:
            password = request.form.get('password', '')

            if check_password_hash(get_admin_password_hash(), password):
                session.clear()
                session['admin_authenticated'] = True
                get_csrf_token()
                clear_login_failures(client_ip)
                return redirect(get_safe_next_url())

            record_login_failure(client_ip)
            delay_login_failure()
            error = 'Login failed.'

    return render_template(
        'login.html',
        error=error,
        next_url=get_safe_next_url()
    )


@app.route('/logout')
def logout():
    if app.secret_key:
        session.clear()
        flash('You have been logged out.')

    return redirect(url_for('login'))




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
        'admin_dashboard',
        'admin_logs',
        'admin_species',
        'admin_config',
        'change_password',
    }

    if endpoint in admin_template_endpoints:
        context.update({
            "health": get_system_health(),
            "retention_status": get_retention_status()
        })

    return context

@app.route('/')
def index():
    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')
    earliest_date = get_earliest_detection_date()
    recent_records = recent_detections(4)
    daily_summary = get_daily_summary(today)
    activity_by_hour = get_activity_by_hour(date_str)
    top_species = get_top_species(date_str)
    latest_visitor = get_latest_visitor()
    return render_template('index.html',recent_detections=recent_records,daily_summary=daily_summary,activity_by_hour=activity_by_hour,
                           top_species=top_species,latest_visitor=latest_visitor,current_hour=today.hour,date=date_str,earliest_date=earliest_date)

@app.route('/recent')
def recent_feed():
    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')

    earliest_date = get_earliest_detection_date()

    recent_records = recent_detections(50)

    return render_template(
        'recent_feed.html',
        recent_detections=recent_records,
        current_hour=today.hour,
        date=date_str,
        earliest_date=earliest_date
    )


@app.route('/frigate/<frigate_event>/thumbnail.jpg')
def frigate_thumbnail(frigate_event):
    frigate_url = config['frigate']['frigate_url']
    try:
        response = requests.get(
            f'{frigate_url}/api/events/{frigate_event}/thumbnail.jpg',
            stream=True, timeout=5)
        if response.status_code == 200:
            return send_file(response.raw, mimetype=response.headers['Content-Type'])
        else:
            return send_from_directory('static/images', '1x1.png', mimetype='image/png')
    except Exception as e:
        print(f"Error fetching thumbnail from frigate: {e}", flush=True)
        return send_from_directory('static/images', '1x1.png', mimetype='image/png')


@app.route('/frigate/<frigate_event>/snapshot.jpg')
def frigate_snapshot(frigate_event):
    frigate_url = config['frigate']['frigate_url']
    try:
        response = requests.get(
            f'{frigate_url}/api/events/{frigate_event}/snapshot.jpg',
            stream=True, timeout=5)
        if response.status_code == 200:
            return send_file(response.raw, mimetype=response.headers['Content-Type'])
        else:
            return send_from_directory('static/images', '1x1.png', mimetype='image/png')
    except Exception as e:
        print(f"Error fetching snapshot from frigate: {e}", flush=True)
        return send_from_directory('static/images', '1x1.png', mimetype='image/png')


@app.route('/frigate/<frigate_event>/clip.mp4')
def frigate_clip(frigate_event):
    frigate_url = config['frigate']['frigate_url']
    try:
        response = requests.get(
            f'{frigate_url}/api/events/{frigate_event}/clip.mp4',
            stream=True, timeout=30)
        if response.status_code == 200:
            return send_file(response.raw, mimetype=response.headers['Content-Type'])
        else:
            return send_from_directory('static/images', '1x1.png', mimetype='image/png')
    except Exception as e:
        print(f"Error fetching clip from frigate: {e}", flush=True)
        return send_from_directory('static/images', '1x1.png', mimetype='image/png')

@app.route('/wamf/snapshot/<path:filename>')
def wamf_snapshot(filename):

    return send_file(
        f"media/wamf/snapshots/{filename}"
    )

@app.route('/wamf/clip/<path:filename>')
def wamf_clip(filename):

    return send_file(
        f"media/wamf/clips/{filename}"
    )

@app.route('/detections/by_hour/<date>/<int:hour>')
def show_detections_by_hour(date, hour):
    records = get_records_for_date_hour(date, hour)
    return render_template('detections_by_hour.html', date=date, hour=hour, records=records)


@app.route('/detections/by_scientific_name/<scientific_name>/<date>', defaults={'end_date': None})
@app.route('/detections/by_scientific_name/<scientific_name>/<date>/<end_date>')
def show_detections_by_scientific_name(scientific_name, date, end_date):

    page = request.args.get(
        'page',
        1,
        type=int
    )
    print(f"scientific_name = [{scientific_name}]")
    print(f"date = [{date}]")

    if end_date is not None:
        return jsonify({"error": "Date range queries are not yet implemented."}), 501
    per_page = 25
    total_records = (
        get_detection_count_for_scientific_name_and_date(
            scientific_name,
        date
        )
    )

    total_pages = (
        total_records + per_page - 1
    ) // per_page
    records = get_records_for_scientific_name_and_date(scientific_name, date, page, per_page)
    print(f"scientific_name = [{scientific_name}]")
    print(f"date = [{date}]")
    species_stats = get_species_stats_for_date(scientific_name, date)
    species_info = get_species_info(
        scientific_name
    )

    if (
        not species_info
        or not species_info["description"]
        or not species_info["thumbnail_url"]
    ):

        metadata = fetch_species_metadata(
            scientific_name
        )

        save_species_info(
            scientific_name=scientific_name,
            common_name=get_common_name(
                scientific_name
            ),
            description=metadata["description"],
            wikipedia_url=metadata["wikipedia_url"],
            thumbnail_url=metadata["thumbnail_url"]
        )

        species_info = get_species_info(
            scientific_name
        )
    
    species_activity = get_species_activity_by_hour(
        scientific_name
    )
    return render_template('detections_by_scientific_name.html', scientific_name=scientific_name, date=date,
                           end_date=end_date, common_name=get_common_name(scientific_name), records=records, species_stats=species_stats, 
                           species_activity=species_activity, species_info=species_info, page=page, total_pages=total_pages, total_records=total_records)


@app.route('/api/detections/recent')
def api_recent_detections():
    limit = request.args.get('limit', 5, type=int)
    records = recent_detections(min(limit, 20))  # cap at 20
    return jsonify(records)


@app.route('/daily_summary')
@app.route('/daily_summary/')
def show_daily_summary_today():
    today = datetime.now().strftime('%Y-%m-%d')
    target = url_for('show_daily_summary', date=today)
    query = request.query_string.decode('utf-8')
    if query:
        target = f'{target}?{query}'
    return redirect(target)


@app.route('/daily_summary/<date>')
def show_daily_summary(date):
    date_datetime = datetime.strptime(date, "%Y-%m-%d")
    daily_summary = get_daily_summary(date_datetime)
    today = datetime.now().strftime('%Y-%m-%d')
    earliest_date = get_earliest_detection_date()
    return render_template('daily_summary.html', daily_summary=daily_summary, date=date, today=today,
                           earliest_date=earliest_date)


@app.route('/detections/<frigate_event>', methods=['DELETE'])
def delete_detection(frigate_event):
    if not frigate_event:
        return jsonify({"success": False, "message": "Missing detection identifier."}), 400

    conn = None
    try:
        conn = sqlite3.connect(DBPATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT wamf_snapshot_path, wamf_clip_path
            FROM detections
            WHERE frigate_event = ?
            """,
            (frigate_event,)
        )
        detection = cursor.fetchone()

        if detection is None:
            return jsonify({"success": False, "message": "Detection not found."}), 404

        deleted_media = delete_wamf_media_files(
            detection["wamf_snapshot_path"],
            detection["wamf_clip_path"]
        )

        cursor.execute(
            "DELETE FROM detections WHERE frigate_event = ?",
            (frigate_event,)
        )
        conn.commit()
    except sqlite3.Error as e:
        print(f"Error deleting detection '{frigate_event}': {e}", flush=True)
        return jsonify({"success": False, "message": "Unable to delete detection."}), 500
    except OSError as e:
        print(f"Error deleting media for detection '{frigate_event}': {e}", flush=True)
        return jsonify({"success": False, "message": "Unable to delete detection media."}), 500
    finally:
        if conn:
            conn.close()

    return jsonify({
        "success": True,
        "message": "Detection deleted.",
        "frigate_event": frigate_event,
        "deleted_media": deleted_media
    }), 200


@app.route('/activity')
def activity():

    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')

    activity_by_hour = get_activity_by_hour(date_str)

    top_species = get_top_species(date_str)

    species_peak_hours = get_species_peak_hours(date_str)

    total_detections = sum(
        item['total']
        for item in activity_by_hour
    )

    busiest_hour = max(
        activity_by_hour,
        key=lambda x: x['total'],
        default=None
    )

    species_count = len(top_species)

    return render_template(
        'activity.html',
        activity_by_hour=activity_by_hour,
        top_species=top_species,
        total_detections=total_detections,
        busiest_hour=busiest_hour,
        species_count=species_count,
        species_peak_hours=species_peak_hours,
        date=date_str
    )

@app.route('/live')
def live_view():
    return render_template(
        'live_view.html'
    )

@app.route('/admin')
def admin_dashboard():

    stats = get_admin_stats()

    health = get_system_health()

    events = get_recent_system_events()

    retention_status = get_retention_status()

    return render_template(
        'admin.html',
        stats=stats,
        health=health,
        events=events,
        retention_status=retention_status
    )

@app.route('/admin/logs')
def admin_logs():

    event_type = request.args.get("filter")

    logs = get_recent_system_events(
        100,
        event_type
    )

    return render_template(
        'admin_logs.html',
        logs=logs,
        current_filter=event_type
    )

@app.route('/admin/species')
def admin_species():

    species = get_all_species_info()

    species_count = len(species)

    missing_description = sum(
        1
        for s in species
        if not s["description"]
    )

    missing_thumbnail = sum(
        1
        for s in species
        if not s["thumbnail_url"]
    )

    latest_update = None

    if species:

        latest_update = max(
            s["last_updated"]
            for s in species
            if s["last_updated"]
        )

    return render_template(
        'admin_species.html',
        species=species,
        species_count=species_count,
        missing_description=missing_description,
        missing_thumbnail=missing_thumbnail,
        latest_update=latest_update
    )

def refresh_species_metadata(scientific_name):

    metadata = fetch_species_metadata(
        scientific_name
    )

    save_species_info(
        scientific_name=scientific_name,
        common_name=get_common_name(
            scientific_name
        ),
        description=metadata["description"],
        wikipedia_url=metadata["wikipedia_url"],
        thumbnail_url=metadata["thumbnail_url"]
    ) 

@app.route('/admin/species/refresh/<path:scientific_name>', methods=['POST'])

def refresh_species(scientific_name):

    refresh_species_metadata(
        scientific_name
    )

    log_system_event(
        "INFO",
        "SPECIES",
        f"Refreshed metadata for {scientific_name}"
    )

    return redirect(
        url_for(
            'admin_species'
        )
    )

@app.route('/admin/species/refresh-missing', methods=['POST'])
def refresh_missing_species():

    species = get_all_species_info()

    refreshed = 0

    for item in species:

        if (
            not item["description"]
            or not item["thumbnail_url"]
        ):

            refresh_species_metadata(
                item["scientific_name"]
            )

            refreshed += 1

    log_system_event(
        "INFO",
        "SPECIES",
        f"Refreshed metadata for {refreshed} species with missing data"
    )

    return redirect(
        url_for(
            'admin_species'
        )
    )

@app.route('/admin/species/refresh-all', methods=['POST'])
def refresh_all_species():

    species = get_all_species_info()

    for item in species:

        refresh_species_metadata(
            item["scientific_name"]
        )

    log_system_event(
        "INFO",
        "SPECIES",
        f"Refreshed metadata for {len(species)} species"
    )

    return redirect(
        url_for(
            'admin_species'
        )
    )

@app.route("/admin/api/health")
def admin_health():

    health = get_system_health()

    retention = get_retention_status()

    return jsonify({
        "version": VERSION,

        "frigate": health["frigate_online"],
        "mqtt": health["mqtt_online"],
        "database": health["database_healthy"],
        "archive_storage": health["archive_writable"],
        "disk_used_percent": health["disk_used_percent"],

        "last_retention_run": (
            retention["last_run"]
            if retention else None
        ),

        "orphan_count": (
            retention["orphan_count"]
            if retention else None
        ),

        "missing_count": (
            retention["missing_count"]
            if retention else None
        )
    })

def strip_admin_config_block(config_content):
    lines = config_content.splitlines()
    kept = []
    skipping_admin = False

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if skipping_admin:
            if not stripped or line.lstrip().startswith('#') or indent > 0:
                continue
            skipping_admin = False

        if indent == 0 and re.match(r'^admin\s*:', line):
            skipping_admin = True
            continue

        kept.append(line)

    return '\n'.join(kept).strip() + '\n'


def load_config_file_content():
    with open(get_config_path(), 'r') as config_file:
        return config_file.read()


def load_config_from_content(config_content):
    return yaml.safe_load(config_content) or {}


def get_existing_admin_config():
    current_config = load_config_from_content(load_config_file_content())
    return current_config.get('admin')


def append_admin_config_block(config_content, admin_config):
    sanitized_content = strip_admin_config_block(config_content).rstrip()

    if not admin_config:
        return sanitized_content + '\n'

    admin_content = yaml.safe_dump(
        {'admin': admin_config},
        sort_keys=False
    ).strip()

    return f"{sanitized_content}\n\n{admin_content}\n"


def write_config_preserving_admin(config_content, admin_config=None):
    if admin_config is None:
        admin_config = get_existing_admin_config()

    sanitized_content = strip_admin_config_block(config_content)
    load_config_from_content(sanitized_content)
    final_content = append_admin_config_block(sanitized_content, admin_config)
    load_config_from_content(final_content)

    backup_path = (
        f"{get_config_path()}."
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"
    )

    shutil.copy2(
        get_config_path(),
        backup_path
    )

    with open(
        get_config_path(),
        'w'
    ) as config_file:

        config_file.write(
            final_content
        )

    load_config()


def update_admin_password_hash(password_hash):
    current_content = load_config_file_content()
    current_config = load_config_from_content(current_content)
    admin_config = current_config.get('admin', {})
    admin_config['password_hash'] = password_hash
    write_config_preserving_admin(current_content, admin_config)


def get_config_path():

    return os.environ.get(
        'WHOSATMYFEEDER_CONFIG',
        './config/config.yml'
    )

@app.route('/admin/config')
def admin_config():

    with open(
        get_config_path(),
        'r'
    ) as config_file:

        config_content = strip_admin_config_block(config_file.read())

        file_size = os.path.getsize(
        get_config_path()
    )

    last_modified = datetime.fromtimestamp(
        os.path.getmtime(
            get_config_path()
        )
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    backup_count = len(
    glob.glob(
        f"{get_config_path()}.*.bak"
    )
)

    return render_template(
        'admin_config.html',
        config_content=config_content,
        config_path=get_config_path(),
        file_size=file_size,
        last_modified=last_modified,
        backup_count=backup_count
    )   

@app.route(
    '/admin/config/save',
    methods=['POST']
)
def save_config():

    data = request.get_json()

    config_content = data.get(
        'config_content',
        ''
    )

    try:

        write_config_preserving_admin(
            config_content
        )

        log_system_event(
            "INFO",
            "CONFIG",
            "Configuration updated via admin editor"
        )
        
        return {
            "success": True,
            "message": "Configuration updated"
        }

    except yaml.YAMLError as e:

        return {
            "success": False,
            "error": str(e)
        }


@app.route('/admin/password', methods=['GET', 'POST'])
def change_password():

    error = None

    if request.method == 'POST':

        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not check_password_hash(get_admin_password_hash() or '', current_password):
            error = 'Password change failed.'
        elif len(new_password) < 8:
            error = 'New password must be at least 8 characters.'
        elif new_password != confirm_password:
            error = 'New passwords do not match.'
        else:
            update_admin_password_hash(
                generate_password_hash(new_password)
            )
            flash('Admin password updated.')
            return redirect(url_for('change_password'))

    return render_template(
        'admin_password.html',
        error=error
    )


def load_config():
    global config

    with open(get_config_path(), 'r') as config_file:
        config = yaml.safe_load(config_file) or {}

    configure_session_secret()


load_config()

