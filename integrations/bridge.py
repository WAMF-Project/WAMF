import logging
import time

import requests

logger = logging.getLogger(__name__)
_last_warning_at = 0.0
_WARNING_INTERVAL_SECONDS = 60


def _log_failure(message, *args):
    """Rate-limit warnings while retaining failures at debug level."""
    global _last_warning_at

    now = time.monotonic()
    if now - _last_warning_at >= _WARNING_INTERVAL_SECONDS:
        logger.warning(message, *args)
        _last_warning_at = now
    else:
        logger.debug(message, *args)


def post_event(bridge_config, category, level, title, details=""):
    """Post a WAMF event to Bridge, without raising errors."""
    bridge_config = bridge_config or {}
    if not bridge_config.get("enabled", False):
        return False

    events_url = bridge_config.get("events_url")
    if not events_url:
        _log_failure("Bridge is enabled but events_url is not configured")
        return False

    payload = {
        "source": "WAMF",
        "category": category or "",
        "level": level or "",
        "title": title or "",
        "details": details or "",
    }

    try:
        response = requests.post(
            events_url,
            json=payload,
            timeout=bridge_config.get("timeout_seconds", 1),
        )
    except requests.exceptions.RequestException as exc:
        _log_failure("Bridge observation event failed: %s", exc)
        return False
    except Exception as exc:
        # This integration is deliberately best-effort, including malformed
        # optional configuration or unusual HTTP client failures.
        _log_failure("Bridge observation event failed: %s", exc)
        return False

    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        _log_failure(
            "Bridge observation event returned HTTP %s",
            status_code,
        )
        return False

    return True


def post_observation_event(
    bridge_config,
    common_name=None,
    scientific_name=None,
    confidence=None,
    camera=None,
    frigate_event=None,
):
    """Post a new-observation event to Bridge, without raising errors."""
    title = f"{common_name} observed" if common_name else "Observation recorded"

    detail_parts = []
    if camera:
        detail_parts.append(f"Camera: {camera}")
    if scientific_name:
        detail_parts.append(f"Scientific: {scientific_name}")
    if confidence is not None:
        try:
            detail_parts.append(f"Confidence: {float(confidence):.1%}")
        except (TypeError, ValueError):
            pass
    if frigate_event:
        detail_parts.append(f"Frigate event: {frigate_event}")

    return post_event(
        bridge_config,
        category="observation",
        level="info",
        title=title,
        details=" | ".join(detail_parts),
    )


def post_health_event(bridge_config, level, title, details):
    """Post an overall health state-change event to Bridge."""
    return post_event(
        bridge_config,
        category="system",
        level=level,
        title=title,
        details=details,
    )
