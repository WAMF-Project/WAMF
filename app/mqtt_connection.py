"""Shared MQTT setup and an isolated, one-shot CONNECT/CONNACK probe."""

import errno
import json
import math
import os
import socket
import ssl
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

from app.mqtt_settings import MqttSettings

PROBE_TIMEOUT_SECONDS = 5.0
MESSAGES = {
    "success": "Broker accepted an MQTT connection using the saved settings.",
    "dns_error": "Broker hostname could not be resolved.",
    "refused": "Broker refused the network connection.",
    "unreachable": "Broker could not be reached.",
    "timeout": "Connection test timed out.",
    "tls_certificate_error": "Broker certificate could not be verified.",
    "tls_error": "TLS connection could not be established.",
    "auth_rejected": "Broker rejected authentication or authorization.",
    "connection_rejected": "Broker rejected the MQTT connection.",
    "closed_before_acceptance": "Connection closed before MQTT acceptance.",
    "configuration_error": "MQTT connection settings could not be used.",
    "internal_error": "MQTT connection test could not be completed.",
}


@dataclass(frozen=True)
class MqttProbeResult:
    success: bool
    code: str
    safe_message: str
    duration_ms: int
    completed_at: str


def configure_mqtt_client(client, settings: MqttSettings):
    """Apply only connection options common to runtime and temporary clients."""
    if settings.authentication_enabled:
        client.username_pw_set(settings.username, settings.password)
    if settings.tls_enabled:
        client.tls_set(ca_certs=settings.ca_certs)
        client.tls_insecure_set(settings.tls_insecure)


def _exception_code(exc):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_certificate_error"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, socket.gaierror):
        return "dns_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "closed_before_acceptance"
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return "unreachable"
        if exc.errno in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE):
            return "closed_before_acceptance"
    if isinstance(exc, (ValueError, TypeError)):
        return "configuration_error"
    return "internal_error"


def _probe_in_worker(settings, timeout_seconds):
    """Run only inside the owned child; the parent also enforces the deadline."""
    client = None
    code = None
    deadline = time.monotonic() + timeout_seconds

    def on_connect(client, userdata, flags, rc):
        nonlocal code
        code = "success" if rc == 0 else (
            "auth_rejected" if rc in (4, 5) else "connection_rejected"
        )

    def on_disconnect(client, userdata, rc):
        nonlocal code
        if code is None:
            code = "closed_before_acceptance"

    try:
        client = mqtt.Client(
            client_id="wamf-probe-" + uuid.uuid4().hex,
            clean_session=True,
            reconnect_on_failure=False,
        )
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        # File/SSL-context setup errors describe unusable configuration, not
        # a broker-side TLS handshake failure.
        try:
            configure_mqtt_client(client, settings)
        except Exception:
            return "configuration_error"
        if client.connect(settings.host, settings.port, keepalive=60) != mqtt.MQTT_ERR_SUCCESS:
            return "connection_rejected"
        while code is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            rc = client.loop(timeout=min(remaining, 0.1))
            if code is None and rc != mqtt.MQTT_ERR_SUCCESS:
                return "closed_before_acceptance"
        return code
    except Exception as exc:
        return _exception_code(exc)
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass  # Cleanup cannot replace the sanitized probe outcome.


def probe_mqtt_connection(settings: MqttSettings, *, timeout_seconds=PROBE_TIMEOUT_SECONDS):
    """Probe canonical settings without Flask, config writes, or runtime state.

    DNS, TCP, TLS and cleanup can block in Paho. A fresh interpreter isolates
    those operations. Credentials travel through stdin only, never argv or
    output. No worker threads survive a timeout; every child is killed/reaped.
    """
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
            payload = json.dumps({"settings": asdict(settings), "timeout": timeout_seconds})
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("mqtt_probe_worker.py")), str(os.getpid())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            remaining = timeout_seconds - (time.monotonic() - started)
            output, _ = process.communicate(payload, timeout=max(remaining, 0))
            candidate = output.strip()
            if process.returncode == 0 and candidate in MESSAGES:
                code = candidate
    except subprocess.TimeoutExpired:
        code = "timeout"
    except Exception:
        code = "internal_error"
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
    return MqttProbeResult(
        success=code == "success",
        code=code,
        safe_message=MESSAGES[code],
        duration_ms=round((time.monotonic() - started) * 1000),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
