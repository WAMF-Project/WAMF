"""Saved Frigate API diagnostic with an owned interpreter and hard deadline."""

import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROBE_TIMEOUT_SECONDS = 5.0
MESSAGES = {
    "success": "Frigate API responded using the saved settings.",
    "configuration_error": "Frigate connection settings could not be used.",
    "timeout": "Connection test timed out.",
    "connection_error": "Frigate could not be reached.",
    "tls_error": "A verified TLS connection to Frigate could not be established.",
    "auth_rejected": "Frigate rejected authentication or authorization.",
    "redirect": "Frigate API redirected the request. Review the saved connection settings.",
    "http_error": "Frigate API returned an unsuccessful HTTP status.",
    "unexpected_response": "The server did not return the expected Frigate API response.",
    "internal_error": "Frigate connection test could not be completed.",
}


@dataclass(frozen=True)
class FrigateProbeResult:
    success: bool
    code: str
    safe_message: str
    duration_ms: int
    completed_at: str


@dataclass(frozen=True)
class FrigateDiscoveryResult(FrigateProbeResult):
    # None is a failed discovery; [] is a successfully retrieved empty inventory.
    cameras: list[dict] | None


DISCOVERY_MESSAGES = {
    **MESSAGES,
    "success": "Cameras discovered. Choose cameras, then Save Settings to apply changes.",
    "unexpected_response": "Frigate did not return a usable camera inventory.",
    "internal_error": "Camera discovery could not be completed.",
}


def probe_frigate_connection(base_url, *, timeout_seconds=PROBE_TIMEOUT_SECONDS):
    code, _, duration, completed = _run_worker(base_url, timeout_seconds)
    return FrigateProbeResult(code == "success", code, MESSAGES[code], duration, completed)


def discover_frigate_cameras(base_url, *, timeout_seconds=PROBE_TIMEOUT_SECONDS):
    code, cameras, duration, completed = _run_worker(base_url, timeout_seconds, discover=True)
    return FrigateDiscoveryResult(
        code == "success", code, DISCOVERY_MESSAGES[code], duration, completed, cameras,
    )


def _run_worker(base_url, timeout_seconds, *, discover=False):
    """Keep sensitive input on stdin; kill and reap every unfinished child."""
    started = time.monotonic()
    code = "internal_error"
    process = None
    cameras = None
    try:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            code = "configuration_error"
        else:
            payload = json.dumps({"base_url": base_url, "discover": discover})
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("frigate_probe_worker.py")), str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
            )
            remaining = timeout_seconds - (time.monotonic() - started)
            output, _ = process.communicate(payload, timeout=max(remaining, 0))
            candidate = output.strip()
            if process.returncode == 0:
                if not discover and candidate in MESSAGES:
                    code = candidate
                elif discover:
                    result = json.loads(candidate)
                    if isinstance(result, dict) and result.get("code") in DISCOVERY_MESSAGES:
                        if result["code"] != "success":
                            code = result["code"]
                        elif isinstance(result.get("cameras"), list) and all(
                            isinstance(camera, dict)
                            and set(camera) == {"id", "display_name", "enabled", "detection_enabled"}
                            and isinstance(camera["id"], str) and bool(camera["id"])
                            and isinstance(camera["display_name"], str)
                            and (camera["enabled"] is None or isinstance(camera["enabled"], bool))
                            and (camera["detection_enabled"] is None or isinstance(camera["detection_enabled"], bool))
                            for camera in result["cameras"]
                        ):
                            code = "success"
                            cameras = result["cameras"]
    except subprocess.TimeoutExpired:
        code = "timeout"
    except Exception:
        code = "internal_error"  # Raw exceptions can contain the sensitive URL.
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
    return (
        code, cameras, round((time.monotonic() - started) * 1000),
        datetime.now(UTC).isoformat(),
    )
