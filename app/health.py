import logging
import math
import sqlite3
import threading

import requests
import yaml
import paho.mqtt.client as mqtt
import shutil
from app.config_editor import get_config_path
from app.bootstrap import preflight
from app.config_normalization import normalize_config
from app.db import connect_db
from app.mqtt_settings import mqtt_settings_from_config
from wamf_paths import get_clips_path, get_snapshots_path
from integrations.bridge import post_health_event

# Optional test/explicit override. None keeps config resolution dynamic.
DB_PATH = None
logger = logging.getLogger(__name__)
_previous_health_state = None
_health_state_lock = threading.Lock()
_health_monitor_thread = None
_health_monitor_start_lock = threading.Lock()
DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 60
_detection_worker_enabled = None


def set_detection_worker_enabled(enabled):
    """Remember whether this web process was started with a detection worker."""
    global _detection_worker_enabled
    _detection_worker_enabled = enabled


def _overall_health_state(health):
    if (
        not health["database_healthy"]
        or not health["archive_writable"]
        or health["disk_used_percent"] >= 90
    ):
        return "unhealthy"

    if health.get("setup_required"):
        return "setup_required"

    if (
        not health["frigate_online"]
        or not health["mqtt_online"]
        or health["disk_used_percent"] >= 75
    ):
        return "degraded"

    return "healthy"


def _health_details(health):
    if health.get('setup_required'):
        if health.get('restart_required'):
            return 'Configuration saved; restart WAMF to start detection'
        return 'Setup/configuration required: ' + ', '.join(health.get('configuration_issues', []))
    details = []
    checks = (
        ("frigate_online", "Frigate offline"),
        ("mqtt_online", "MQTT offline"),
        ("database_healthy", "Database unhealthy"),
        ("archive_writable", "Archive storage unavailable"),
    )
    for key, message in checks:
        if not health.get(key, False):
            details.append(message)

    disk_percent = health.get("disk_used_percent")
    if disk_percent is not None and disk_percent >= 75:
        severity = "critical" if disk_percent >= 90 else "high"
        details.append(f"Disk usage {severity} ({disk_percent}%)")

    return "; ".join(details) or "All monitored services healthy"


def record_health_transition(health, bridge_config):
    """Remember overall health and publish only genuine state transitions."""
    global _previous_health_state

    current_state = health["overall_state"]
    with _health_state_lock:
        previous_state = _previous_health_state
        _previous_health_state = current_state

    if previous_state is None or previous_state == current_state:
        return False

    if current_state == "setup_required":
        level = "info"
        title = "WAMF setup required"
    elif current_state == "healthy":
        level = "info"
        title = "WAMF health recovered"
    elif current_state == "unhealthy":
        level = "error"
        title = "WAMF health unhealthy"
    elif previous_state == "unhealthy":
        level = "warning"
        title = "WAMF health improved"
    else:
        level = "warning"
        title = "WAMF health degraded"

    return post_health_event(
        bridge_config,
        level=level,
        title=title,
        details=_health_details(health),
    )


def load_config():

    with open(get_config_path(), "r") as f:
        return yaml.safe_load(f)


def calculate_system_health(config=None):
    """Calculate current health without publishing state transitions."""

    health = {}

    config = load_config() if config is None else config

    issues = preflight(config)
    health['configuration_issues'] = issues
    health['restart_required'] = not issues and _detection_worker_enabled is False
    health['setup_required'] = bool(issues) or health['restart_required']
    # Not checked in setup mode: placeholders are not service outages.
    health['frigate_online'] = None
    health['mqtt_online'] = None
    health['frigate_disk_percent'] = None

    if not health['setup_required']:
        frigate_url = config["frigate"]["frigate_url"]

        # Frigate connectivity
        try:

            response = requests.get(f"{frigate_url}/api/version", timeout=5)

            health["frigate_online"] = response.status_code == 200

        except requests.exceptions.RequestException as exc:
            logger.debug("Frigate health check failed: %s", exc)

            health["frigate_online"] = False

        # MQTT connectivity
        try:
            mqtt_settings = mqtt_settings_from_config(normalize_config(config).config)

            client = mqtt.Client()

            if mqtt_settings.authentication_enabled:
                client.username_pw_set(
                    mqtt_settings.username,
                    mqtt_settings.password,
                )

            if mqtt_settings.tls_enabled:
                client.tls_set(ca_certs=mqtt_settings.ca_certs)
                client.tls_insecure_set(mqtt_settings.tls_insecure)

            client.connect(mqtt_settings.host, mqtt_settings.port, 5)

            client.disconnect()

            health["mqtt_online"] = True

        except OSError as exc:
            logger.debug("MQTT health check failed: %s", exc)

            health["mqtt_online"] = False

    # Database connectivity
    try:

        conn = connect_db(DB_PATH, row_factory=False)

        conn.execute("SELECT 1")

        conn.close()

        health["database_healthy"] = True

    except sqlite3.Error as exc:
        logger.debug("Database health check failed: %s", exc)

        health["database_healthy"] = False

        # Disk usage
    total, used, free = shutil.disk_usage("/")

    used_percent = round((used / total) * 100, 1)

    health["disk_used_percent"] = used_percent

    # Archive directories
    snapshot_dir = get_snapshots_path()
    clip_dir = get_clips_path()

    health["archive_writable"] = snapshot_dir.exists() and clip_dir.exists()

    health["overall_state"] = _overall_health_state(health)

    return health


def get_system_health():
    """Return current health for request-driven routes and templates."""
    return calculate_system_health()


def _health_check_interval(bridge_config):
    value = (bridge_config or {}).get(
        "health_check_interval_seconds",
        DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    )
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

    if isinstance(value, bool) or not math.isfinite(interval) or interval <= 0:
        return DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

    return interval


def health_monitor_loop(stop_event=None):
    """Continuously calculate health and publish overall state transitions."""
    stop_event = stop_event or threading.Event()

    while not stop_event.is_set():
        config = {}
        try:
            config = load_config() or {}
            health = calculate_system_health(config)
            record_health_transition(health, config.get("bridge", {}))
        except Exception as exc:
            logger.warning("Health monitor iteration failed: %s", exc)

        interval = _health_check_interval(config.get("bridge", {}))
        if stop_event.wait(interval):
            break


def start_health_monitor():
    """Start the Flask process health monitor exactly once."""
    global _health_monitor_thread

    with _health_monitor_start_lock:
        if _health_monitor_thread is not None and _health_monitor_thread.is_alive():
            return _health_monitor_thread

        _health_monitor_thread = threading.Thread(
            target=health_monitor_loop,
            name="wamf-health-monitor",
            daemon=True,
        )
        _health_monitor_thread.start()
        return _health_monitor_thread
