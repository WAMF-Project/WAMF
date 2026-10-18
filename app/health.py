import logging
import math
import shutil
import sqlite3
import threading

from app.bootstrap import preflight
from app.config_editor import get_config_path
from app.config_loader import load_runtime_config
from app.config_normalization import normalize_config
from app.db import connect_db
from app.frigate_client import FrigateClient, FrigateError
from app.mqtt_presentation import build_mqtt_presentation
from integrations.bridge import post_health_event
from wamf_paths import get_clips_path, get_snapshots_path

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
    mqtt_status = health.get('mqtt_runtime', {}).get('status', 'offline').lower()
    checks = (
        ("frigate_online", "Frigate offline"),
        ("mqtt_online", f"MQTT {mqtt_status}"),
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
    return load_runtime_config()


def calculate_system_health(config=None, *, mqtt_runtime_status=None):
    """Calculate current health without publishing state transitions."""

    health = {}

    try:
        config = load_config() if config is None else config
    except Exception:
        config = None
    try:
        issues = preflight(config) if config is not None else ['Configuration unavailable']
    except Exception:
        issues = ['Configuration contains invalid values. Review Settings.']
    health['configuration_issues'] = issues
    health['restart_required'] = not issues and _detection_worker_enabled is False
    health['setup_required'] = bool(issues) or health['restart_required']
    # Not checked in setup mode: placeholders are not service outages.
    health['frigate_online'] = None
    try:
        snapshot = mqtt_runtime_status() if mqtt_runtime_status is not None else None
    except Exception:
        snapshot = None
    health['mqtt_runtime'] = build_mqtt_presentation(snapshot, config)
    # Existing nullable API field: True now means the real consumer is both
    # connected and subscribed. Unknown/stale/setup remain None.
    health['mqtt_online'] = health['mqtt_runtime']['online']
    health['frigate_disk_percent'] = None

    if not health['setup_required']:
        frigate_url = config["frigate"]["frigate_url"]

        # Frigate connectivity
        try:
            FrigateClient(frigate_url).get_version()
            health["frigate_online"] = True
        except FrigateError as exc:
            logger.debug("Frigate health check failed: %s", exc)
            health["frigate_online"] = False

    # Database connectivity
    try:

        conn = connect_db(DB_PATH, row_factory=False)

        conn.execute("SELECT 1")

        conn.close()

        health["database_healthy"] = True

    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        logger.debug("Database health check failed")

        health["database_healthy"] = False

        # Disk usage
    total, used, free = shutil.disk_usage("/")

    used_percent = round((used / total) * 100, 1)

    health["disk_used_percent"] = used_percent

    # Archive directories
    try:
        snapshot_dir = get_snapshots_path()
        clip_dir = get_clips_path()
        health["archive_writable"] = snapshot_dir.exists() and clip_dir.exists()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        health["archive_writable"] = False

    health["overall_state"] = _overall_health_state(health)

    return health


def get_system_health(*, mqtt_runtime_status=None):
    """Return current health for request-driven routes and templates."""
    if mqtt_runtime_status is None:
        return calculate_system_health()
    return calculate_system_health(mqtt_runtime_status=mqtt_runtime_status)


def _health_check_interval(config):
    try:
        value = normalize_config(config or {}).config['health']['check_interval_seconds']
    except (ValueError, TypeError):
        return DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

    if isinstance(value, bool) or not math.isfinite(interval) or interval <= 0:
        return DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

    return interval


def health_monitor_loop(stop_event=None, *, mqtt_runtime_status=None):
    """Continuously calculate health and publish overall state transitions."""
    stop_event = stop_event or threading.Event()

    while not stop_event.is_set():
        config = {}
        try:
            config = load_config() or {}
            health = (calculate_system_health(config) if mqtt_runtime_status is None else
                      calculate_system_health(config, mqtt_runtime_status=mqtt_runtime_status))
            record_health_transition(health, config.get("bridge", {}))
        except Exception:
            logger.warning("Health monitor iteration failed")

        interval = _health_check_interval(config)
        if stop_event.wait(interval):
            break


def start_health_monitor(*, mqtt_runtime_status=None):
    """Start the Flask process health monitor exactly once."""
    global _health_monitor_thread

    with _health_monitor_start_lock:
        if _health_monitor_thread is not None and _health_monitor_thread.is_alive():
            return _health_monitor_thread

        _health_monitor_thread = threading.Thread(
            target=health_monitor_loop,
            name="wamf-health-monitor",
            daemon=True,
            **({'kwargs': {'mqtt_runtime_status': mqtt_runtime_status}}
               if mqtt_runtime_status is not None else {}),
        )
        _health_monitor_thread.start()
        return _health_monitor_thread
