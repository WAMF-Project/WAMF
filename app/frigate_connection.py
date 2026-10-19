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


def probe_frigate_connection(base_url, *, timeout_seconds=PROBE_TIMEOUT_SECONDS):
    """Keep sensitive input on stdin; kill and reap every unfinished child."""
    started = time.monotonic()
    code = "internal_error"
    process = None
    try:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            code = "configuration_error"
        else:
            payload = json.dumps({"base_url": base_url})
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("frigate_probe_worker.py")), str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
            )
            remaining = timeout_seconds - (time.monotonic() - started)
            output, _ = process.communicate(payload, timeout=max(remaining, 0))
            candidate = output.strip()
            if process.returncode == 0 and candidate in MESSAGES:
                code = candidate
    except subprocess.TimeoutExpired:
        code = "timeout"
    except Exception:
        code = "internal_error"  # Raw exceptions can contain the sensitive URL.
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
    return FrigateProbeResult(
        success=code == "success", code=code, safe_message=MESSAGES[code],
        duration_ms=round((time.monotonic() - started) * 1000),
        completed_at=datetime.now(UTC).isoformat(),
    )
