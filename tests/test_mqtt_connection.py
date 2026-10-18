"""Broker-free connection/probe tests, including real child deadline cleanup."""

import errno
import json
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

from app import mqtt_connection as connection
from app.mqtt_settings import MqttSettings


@pytest.fixture
def client(monkeypatch):
    client = MagicMock()
    client.connect.return_value = 0
    client.loop.side_effect = lambda **kwargs: client.on_connect(client, None, {}, 0) or 0
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(connection.mqtt, "Client", factory)
    client.factory = factory
    return client


def assert_no_event_operations(client):
    client.publish.assert_not_called()
    client.subscribe.assert_not_called()
    client.reconnect.assert_not_called()
    client.loop_start.assert_not_called()
    client.loop_forever.assert_not_called()
    client.disconnect.assert_called_once_with()


@pytest.mark.parametrize("auth", [False, True])
@pytest.mark.parametrize("insecure", [False, True])
def test_accepted_connack_and_shared_setup(client, auth, insecure):
    settings = MqttSettings(
        host="broker", authentication_enabled=auth,
        username="PRIVATE-USER", password="PRIVATE-PASSWORD",
        tls_enabled=True, tls_insecure=insecure, ca_certs="/certs/ca.pem",
    )
    before = asdict(settings)
    assert connection._probe_in_worker(settings, 1) == "success"
    if auth:
        client.username_pw_set.assert_called_once_with(settings.username, settings.password)
    else:
        client.username_pw_set.assert_not_called()
    client.tls_set.assert_called_once_with(ca_certs="/certs/ca.pem")
    client.tls_insecure_set.assert_called_once_with(insecure)
    client.connect.assert_called_once_with("broker", 1883, keepalive=60)
    assert asdict(settings) == before
    assert_no_event_operations(client)


def test_plain_settings_and_unique_clean_probe_ids(client):
    for _ in range(2):
        assert connection._probe_in_worker(MqttSettings(host="broker"), 1) == "success"
    ids = [call.kwargs["client_id"] for call in client.factory.call_args_list]
    assert ids[0] != ids[1]
    for call in client.factory.call_args_list:
        assert call.kwargs["clean_session"] is True
        assert call.kwargs["reconnect_on_failure"] is False
    client.username_pw_set.assert_not_called()
    client.tls_set.assert_not_called()
    client.tls_insecure_set.assert_not_called()


@pytest.mark.parametrize("rc,expected", [(4, "auth_rejected"), (5, "auth_rejected"),
                                         (1, "connection_rejected"), (2, "connection_rejected"),
                                         (3, "connection_rejected")])
def test_connack_rejected(client, rc, expected):
    client.loop.side_effect = lambda **kwargs: client.on_connect(client, None, {}, rc) or 0
    assert connection._probe_in_worker(MqttSettings(host="broker"), 1) == expected
    assert_no_event_operations(client)


@pytest.mark.parametrize("error,expected", [
    (socket.gaierror("PRIVATE-PASSWORD"), "dns_error"),
    (ConnectionRefusedError("PRIVATE-PASSWORD"), "refused"),
    (OSError(errno.ENETUNREACH, "PRIVATE-PASSWORD"), "unreachable"),
    (OSError(errno.EHOSTUNREACH, "PRIVATE-PASSWORD"), "unreachable"),
    (TimeoutError("PRIVATE-PASSWORD"), "timeout"),
    (ssl.SSLCertVerificationError("PRIVATE-PASSWORD"), "tls_certificate_error"),
    (ssl.SSLError("PRIVATE-PASSWORD"), "tls_error"),
    (ConnectionResetError("PRIVATE-PASSWORD"), "closed_before_acceptance"),
    (OSError(errno.ECONNRESET, "PRIVATE-PASSWORD"), "closed_before_acceptance"),
    (ValueError("PRIVATE-PASSWORD"), "configuration_error"),
    (RuntimeError("PRIVATE-PASSWORD"), "internal_error"),
])
def test_connection_failures_sanitized_and_cleaned(client, error, expected, caplog):
    client.connect.side_effect = error
    result = connection._probe_in_worker(MqttSettings(host="broker"), 1)
    assert result == expected
    assert "PRIVATE-PASSWORD" not in result + caplog.text
    assert_no_event_operations(client)


def test_setup_failure_and_cleanup_failure_are_sanitized(client, caplog):
    client.tls_set.side_effect = OSError("PRIVATE-PASSWORD")
    client.disconnect.side_effect = RuntimeError("PRIVATE-USER")
    assert connection._probe_in_worker(
        MqttSettings(host="broker", tls_enabled=True), 1
    ) == "configuration_error"
    client.connect.assert_not_called()
    assert_no_event_operations(client)
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("callback", [True, False])
def test_closed_before_acceptance(client, callback):
    def loop(**kwargs):
        if callback:
            client.on_disconnect(client, None, 7)
        return connection.mqtt.MQTT_ERR_CONN_LOST
    client.loop.side_effect = loop
    assert connection._probe_in_worker(MqttSettings(host="broker"), 1) == "closed_before_acceptance"
    assert_no_event_operations(client)


def test_connect_completion_alone_is_not_success(client, monkeypatch):
    monkeypatch.setattr(connection.time, "monotonic", iter([0, 0.1, 1.1]).__next__)
    client.loop.side_effect = None
    client.loop.return_value = 0
    assert connection._probe_in_worker(MqttSettings(host="broker"), 1) == "timeout"
    assert_no_event_operations(client)


def test_client_construction_failure_does_not_escape(monkeypatch):
    monkeypatch.setattr(connection.mqtt, "Client", MagicMock(side_effect=RuntimeError("PRIVATE")))
    assert connection._probe_in_worker(MqttSettings(host="broker"), 1) == "internal_error"


@pytest.mark.parametrize("code", list(connection.MESSAGES))
def test_parent_contract_and_private_transport(monkeypatch, code, caplog):
    process = MagicMock(returncode=0)
    process.communicate.return_value = (code, None)
    process.poll.return_value = 0
    factory = MagicMock(return_value=process)
    monkeypatch.setattr(connection.subprocess, "Popen", factory)
    settings = MqttSettings(host="broker", username="PRIVATE-USER", password="PRIVATE-PASSWORD")
    result = connection.probe_mqtt_connection(settings)
    assert result.code == code
    assert result.success is (code == "success")
    assert result.safe_message == connection.MESSAGES[code]
    assert result.duration_ms >= 0
    assert result.completed_at.endswith("+00:00")
    assert "PRIVATE" not in repr(result) + repr(settings) + caplog.text
    args, kwargs = factory.call_args
    assert "PRIVATE" not in repr(args) + repr(kwargs)
    assert kwargs["stderr"] is subprocess.DEVNULL
    payload = json.loads(process.communicate.call_args_list[0].args[0])
    assert payload["settings"]["password"] == settings.password
    assert payload["settings"]["username"] == settings.username


@pytest.mark.parametrize("output,returncode", [("PRIVATE-PASSWORD", 0), ("success", 1)])
def test_invalid_child_output_is_never_returned(monkeypatch, output, returncode):
    process = MagicMock(returncode=returncode)
    process.communicate.return_value = (output, None)
    monkeypatch.setattr(connection.subprocess, "Popen", MagicMock(return_value=process))
    result = connection.probe_mqtt_connection(MqttSettings(host="broker"))
    assert result.code == "internal_error"
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "5"])
def test_invalid_deadline_never_launches_worker(monkeypatch, timeout):
    factory = MagicMock()
    monkeypatch.setattr(connection.subprocess, "Popen", factory)
    assert connection.probe_mqtt_connection(
        MqttSettings(host="broker"), timeout_seconds=timeout
    ).code == "configuration_error"
    factory.assert_not_called()


def test_real_stalled_child_is_killed_and_reaped(monkeypatch):
    real_popen = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        # No broker, DNS or application startup: model blocking library work.
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(connection.subprocess, "Popen", launch)
    started = time.monotonic()
    for _ in range(2):
        result = connection.probe_mqtt_connection(MqttSettings(host="broker"), timeout_seconds=0.1)
        assert result.code == "timeout"
    assert time.monotonic() - started < 3
    assert all(child.returncode is not None for child in children)
    assert all(child.stdin.closed and child.stdout.closed for child in children)


def test_real_worker_invalid_host_is_safe_without_network():
    result = connection.probe_mqtt_connection(
        MqttSettings(host="", username="PRIVATE-USER", password="PRIVATE-PASSWORD")
    )
    assert result.code == "configuration_error"
    assert "PRIVATE" not in repr(result)


def test_probe_does_not_read_or_write_configuration(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    secrets = tmp_path / "secrets.yml"
    config.write_text("deliberately invalid config")
    secrets.write_text("PRIVATE-PASSWORD")
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    result = connection.probe_mqtt_connection(MqttSettings(host=""))
    assert result.code == "configuration_error"
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
