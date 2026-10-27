"""Credential-bearing inputs must not reach failure or DEBUG log records."""

import errno
import importlib
import json
import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

import pytest
import requests
import yaml

from app import archive_media, health
from integrations import bridge


SECRETS = ["SYNTH_PASSWORD_PRIVATE", "SYNTH_TOKEN_PRIVATE", "SYNTH_MQTT_PRIVATE", "SYNTH_SESSION_PRIVATE"]
URL = f"https://user:{SECRETS[0]}@service.invalid/?api_token={SECRETS[1]}"
PRIVATE_MESSAGE = URL + f" mqtt_password={SECRETS[2]} session_secret={SECRETS[3]}\r\nFORGED\x1b[2J"


def _assert_private(caplog):
    assert caplog.records
    for record in caplog.records:
        rendered = logging.Formatter().format(record)
        for secret in SECRETS:
            assert secret not in rendered
        assert "service.invalid" not in rendered
        assert "Traceback" not in rendered
        assert record.exc_info is None
        assert record.exc_text is None
        assert "\x1b" not in rendered


def _http_error():
    response = requests.Response()
    response.status_code = 503
    response.url = URL
    response._content = PRIVATE_MESSAGE.encode()
    return requests.HTTPError(PRIVATE_MESSAGE, response=response)


@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
@pytest.mark.parametrize("prefix", ["!", "*"])
def test_health_yaml_logs_keep_coordinates_and_monitor_continues(
    monkeypatch, tmp_path, caplog, level, prefix
):
    config = tmp_path / (SECRETS[3] + ".yml")
    config.write_text(f"mqtt_password: {prefix}{SECRETS[2]}\n")
    monkeypatch.setattr(health, "get_config_path", lambda: str(config))
    with pytest.raises(yaml.YAMLError) as raised:
        health.load_config()
    assert SECRETS[2] in str(raised.value)  # The real file-loader error would disclose it.
    load = Mock(side_effect=[raised.value, {}])
    calculate = Mock(return_value={"overall_state": "healthy"})
    record = Mock()
    monkeypatch.setattr(health, "load_config", load)
    monkeypatch.setattr(health, "calculate_system_health", calculate)
    monkeypatch.setattr(health, "record_health_transition", record)
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]
    with caplog.at_level(level, logger="app.health"):
        health.health_monitor_loop(stop)
    _assert_private(caplog)
    assert len(caplog.records) == 1
    details = json.loads(caplog.records[0].args[0])
    assert details["category"] == "YAMLError"
    assert details["line"] == 1
    assert details["column"] > 0
    assert load.call_count == 2
    calculate.assert_called_once_with({})
    record.assert_called_once_with({"overall_state": "healthy"}, {})
    assert stop.wait.call_count == 2


@pytest.mark.parametrize("failure", ["http", "mqtt", "database"])
@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
def test_health_debug_failures_do_not_disclose_credentials(monkeypatch, caplog, failure, level):
    config = {
        "frigate": {"frigate_url": URL, "mqtt_server": "broker.invalid", "main_topic": "frigate",
                    "camera": ["audit-camera"], "mqtt_auth": True,
                    "mqtt_username": "user", "mqtt_password": SECRETS[2]},
        "classification": {"model": "audit.tflite", "threshold": 0.5},
        "webui": {"host": "127.0.0.1", "port": 7767},
    }
    get = Mock(return_value=Mock(status_code=200))
    client = Mock()
    database = Mock(return_value=Mock())
    if failure == "http":
        get.side_effect = _http_error()
    elif failure == "mqtt":
        client.connect.side_effect = OSError(errno.ECONNREFUSED, PRIVATE_MESSAGE)
    else:
        database.side_effect = sqlite3.OperationalError(PRIVATE_MESSAGE)
    monkeypatch.setattr(health.requests, "get", get)
    monkeypatch.setattr(health.mqtt, "Client", Mock(return_value=client))
    monkeypatch.setattr(health, "connect_db", database)
    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: (100, 10, 90))
    monkeypatch.setattr(health, "get_snapshots_path", lambda: Mock(exists=lambda: True))
    monkeypatch.setattr(health, "get_clips_path", lambda: Mock(exists=lambda: True))
    monkeypatch.setattr(health, "_detection_worker_enabled", None)
    with caplog.at_level(level, logger="app.health"):
        result = health.calculate_system_health(config)
    flag = {"http": "frigate_online", "mqtt": "mqtt_online", "database": "database_healthy"}[failure]
    assert result[flag] is False
    if level == logging.WARNING:
        assert caplog.records == []
        return
    _assert_private(caplog)
    details = json.loads(caplog.records[0].args[0])
    if failure == "http":
        assert details["http_status"] == 503
    elif failure == "mqtt":
        assert details["errno"] == errno.ECONNREFUSED
    else:
        assert details["category"] == "SQLiteError"


@pytest.mark.parametrize("error_type", [requests.ConnectionError, RuntimeError])
@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
def test_bridge_errors_are_private_and_keep_rate_limiting(monkeypatch, caplog, error_type, level):
    error = error_type(PRIVATE_MESSAGE)
    post = Mock(side_effect=error)
    monkeypatch.setattr(bridge.requests, "post", post)
    monkeypatch.setattr(bridge, "_last_warning_at", 0.0)
    monkeypatch.setattr(bridge.time, "monotonic", Mock(side_effect=[100.0, 101.0]))
    with caplog.at_level(level, logger="integrations.bridge"):
        for _ in range(2):
            assert bridge.post_event({"enabled": True, "events_url": URL}, "audit", "info", "test") is False
    _assert_private(caplog)
    expected = [logging.WARNING, logging.DEBUG] if level == logging.DEBUG else [logging.WARNING]
    assert [record.levelno for record in caplog.records] == expected
    assert bridge._last_warning_at == 100.0
    assert post.call_count == 2
    assert post.call_args.args == (URL,)


@pytest.mark.parametrize("status", [503, SECRETS[1]])
def test_bridge_logs_only_numeric_http_status(monkeypatch, caplog, status):
    monkeypatch.setattr(bridge.requests, "post", Mock(return_value=Mock(status_code=status)))
    monkeypatch.setattr(bridge, "_last_warning_at", 0.0)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 100.0)
    with caplog.at_level(logging.WARNING, logger="integrations.bridge"):
        assert bridge.post_event({"enabled": True, "events_url": URL}, "audit", "info", "test") is False
    _assert_private(caplog)
    assert caplog.records[0].args == ((503,) if status == 503 else ("unknown",))


@pytest.mark.parametrize("component", ["bridge", "snapshot", "clip"])
@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
def test_real_unsupported_scheme_errors_do_not_echo_userinfo_or_query(
    monkeypatch, tmp_path, caplog, component, level
):
    url = URL.replace("https://", "audit-unsupported://", 1)
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda *args, **kwargs: None)
    monkeypatch.setattr(requests.sessions, "get_environ_proxies", lambda *args, **kwargs: {})
    send = Mock(side_effect=AssertionError("Unexpected HTTP transport"))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(bridge, "_last_warning_at", 0.0)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(archive_media, "ensure_storage_paths", Mock())
    monkeypatch.setattr(archive_media, "get_snapshots_path", lambda: tmp_path)
    monkeypatch.setattr(archive_media, "get_clips_path", lambda: tmp_path)
    with caplog.at_level(level):
        if component == "bridge":
            assert bridge.post_event({"enabled": True, "events_url": url}, "audit", "info", "test") is False
        else:
            assert getattr(archive_media, "archive_" + component)(url, "audit-event") is None
    _assert_private(caplog)
    assert json.loads(caplog.records[0].args[0])["category"] == "InvalidSchema"
    send.assert_not_called()


@pytest.mark.parametrize("operation", ["snapshot", "clip"])
@pytest.mark.parametrize("failure", ["request", "file"])
@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
def test_archive_failure_logs_are_private(
    monkeypatch, tmp_path, caplog, operation, failure, level
):
    directory = tmp_path / SECRETS[3]
    monkeypatch.setattr(archive_media, "ensure_storage_paths", Mock())
    monkeypatch.setattr(archive_media, "get_snapshots_path", lambda: directory)
    monkeypatch.setattr(archive_media, "get_clips_path", lambda: directory)
    events = Mock()
    monkeypatch.setattr(archive_media, "log_system_event", events)
    response = Mock(status_code=200, content=b"media")
    get = Mock(return_value=response)
    monkeypatch.setattr(archive_media.requests, "get", get)
    function = getattr(archive_media, "archive_" + operation)
    with caplog.at_level(level, logger="app.archive_media"):
        if failure == "request":
            get.side_effect = _http_error()
            assert function(URL, "audit-event") is None
        else:
            error = PermissionError(errno.EACCES, PRIVATE_MESSAGE, str(directory))
            with patch("builtins.open", side_effect=error):
                assert function(URL, "audit-event") is None
    _assert_private(caplog)
    details = json.loads(caplog.records[0].args[0])
    assert details["http_status" if failure == "request" else "errno"] == (503 if failure == "request" else errno.EACCES)
    events.assert_not_called()
    get.assert_called_once()


@pytest.mark.parametrize("operation", ["snapshot", "clip"])
def test_archive_success_still_writes_media(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(archive_media, "ensure_storage_paths", Mock())
    monkeypatch.setattr(archive_media, "get_snapshots_path", lambda: tmp_path)
    monkeypatch.setattr(archive_media, "get_clips_path", lambda: tmp_path)
    monkeypatch.setattr(archive_media, "log_system_event", Mock())
    response = Mock(status_code=200, content=b"media")
    response.iter_content.return_value = iter([b"me", b"", b"dia"])
    monkeypatch.setattr(archive_media.requests, "get", Mock(return_value=response))
    result = getattr(archive_media, "archive_" + operation)("https://frigate.invalid", "audit-event")
    assert Path(result).read_bytes() == b"media"
    if operation == "clip":
        response.iter_content.assert_called_once_with(chunk_size=8192)


def test_clip_not_ready_retries_are_unchanged(monkeypatch, tmp_path):
    get = Mock(return_value=Mock(status_code=404))
    sleep = Mock()
    events = Mock()
    monkeypatch.setattr(archive_media, "ensure_storage_paths", Mock())
    monkeypatch.setattr(archive_media, "get_clips_path", lambda: tmp_path)
    monkeypatch.setattr(archive_media.requests, "get", get)
    monkeypatch.setattr(archive_media.time, "sleep", sleep)
    monkeypatch.setattr(archive_media, "log_system_event", events)
    assert archive_media.archive_clip("https://frigate.invalid", "audit-event") is None
    assert get.call_count == 10
    assert sleep.call_args_list == [call(2)] * 10
    assert events.call_count == 11
    assert not list(tmp_path.iterdir())


@pytest.fixture
def classifier(flask_client, monkeypatch):
    # Match existing classifier tests: no model/native ML initialization.
    for name in ["numpy", "tflite_support", "tflite_support.task", "tflite_support.task.core",
                 "tflite_support.task.processor", "tflite_support.task.vision", "PIL", "PIL.Image"]:
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    module = importlib.import_module("speciesid")
    monkeypatch.setattr(module, "config", {
        "frigate": {"frigate_url": URL, "mqtt_server": URL, "main_topic": "frigate",
                    "camera": ["audit-camera"], "mqtt_auth": True,
                    "mqtt_username": "user", "mqtt_password": SECRETS[2]},
        "classification": {"model": "audit.tflite", "threshold": 0.5},
    })
    return module


@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
def test_classifier_snapshot_errors_and_debug_never_log_url(classifier, monkeypatch, caplog, level):
    connection = Mock()
    monkeypatch.setattr(classifier, "connect_db", Mock(return_value=connection))
    monkeypatch.setattr(classifier.requests, "get", Mock(side_effect=_http_error()))
    message = Mock(retain=False, topic="frigate/events")
    message.payload = json.dumps({"type": "new", "after": {
        "camera": "audit-camera", "label": "bird", "id": "audit-event",
    }}).encode()
    with caplog.at_level(level, logger="speciesid"):
        assert classifier._on_message_inner(Mock(), None, message) is None
    _assert_private(caplog)
    connection.close.assert_called_once_with()
    assert any("http_status" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
def test_sublabel_error_keeps_status_without_exception_text(classifier, monkeypatch, caplog, level):
    post = Mock(side_effect=_http_error())
    monkeypatch.setattr(classifier.requests, "post", post)
    with caplog.at_level(level, logger="speciesid"):
        assert classifier.set_sublabel(URL, "audit-event", "American Robin") is None
    _assert_private(caplog)
    assert json.loads(caplog.records[0].args[0])["http_status"] == 503
    assert post.call_args.args[0] == URL + "/api/events/audit-event/sub_label"


@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
def test_classifier_catchall_does_not_append_sensitive_traceback(classifier, monkeypatch, caplog, level):
    error = ValueError(PRIVATE_MESSAGE)
    error.__cause__ = RuntimeError(PRIVATE_MESSAGE)
    inner = Mock(side_effect=[error, None])
    monkeypatch.setattr(classifier, "_on_message_inner", inner)
    with caplog.at_level(level, logger="speciesid"):
        assert classifier.on_message(Mock(), None, Mock()) is None
        assert classifier.on_message(Mock(), None, Mock()) is None
    _assert_private(caplog)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    assert inner.call_count == 2


def test_mqtt_reconnect_logs_safe_category_and_keeps_retry(classifier, monkeypatch, caplog):
    client = Mock()
    client.reconnect.side_effect = [RuntimeError(PRIVATE_MESSAGE), None]
    sleep = Mock()
    monkeypatch.setattr(classifier.time, "sleep", sleep)
    with caplog.at_level(logging.DEBUG, logger="speciesid"):
        classifier.on_disconnect(client, None, 1)
    _assert_private(caplog)
    assert client.reconnect.call_count == 2
    sleep.assert_called_once_with(60)


def test_mqtt_startup_does_not_log_misconfigured_credential_url(classifier, monkeypatch, caplog):
    client = Mock()
    monkeypatch.setattr(classifier, "classifier", None)
    monkeypatch.setattr(classifier, "configure_worker_signals", Mock())
    monkeypatch.setattr(classifier.mqtt, "Client", Mock(return_value=client))
    with caplog.at_level(logging.DEBUG, logger="speciesid"):
        classifier.run_mqtt_client()
    _assert_private(caplog)
    client.username_pw_set.assert_called_once_with("user", SECRETS[2])
    client.connect.assert_called_once_with(URL, 1883)
    client.loop_forever.assert_called_once_with()
