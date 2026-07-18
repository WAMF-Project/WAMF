from unittest.mock import MagicMock, patch

import pytest
import yaml

import app.health as health_module
from app.health import (
    calculate_system_health,
    get_system_health,
    health_monitor_loop,
    load_config,
    record_health_transition,
    start_health_monitor,
)


@pytest.fixture(autouse=True)
def reset_previous_health_state():
    health_module._previous_health_state = None
    health_module._health_monitor_thread = None
    yield
    health_module._previous_health_state = None
    health_module._health_monitor_thread = None


def _health(state):
    values = {
        "frigate_online": True,
        "mqtt_online": True,
        "database_healthy": True,
        "archive_writable": True,
        "disk_used_percent": 10,
        "overall_state": state,
    }
    if state == "degraded":
        values["mqtt_online"] = False
    elif state == "unhealthy":
        values["database_healthy"] = False
    return values


def test_health_load_config_uses_config_env_var(monkeypatch, tmp_path):
    config_path = tmp_path / "custom-config.yml"
    config_path.write_text("""
frigate:
  frigate_url: http://example.invalid
  mqtt_server: mqtt.example.invalid
  mqtt_port: 1883
""".lstrip())

    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    assert load_config() == yaml.safe_load(config_path.read_text())


def test_initial_health_state_is_recorded_without_publishing():
    with patch("app.health.post_health_event") as mock_post:
        assert record_health_transition(_health("healthy"), {}) is False

    mock_post.assert_not_called()
    assert health_module._previous_health_state == "healthy"


@pytest.mark.parametrize(
    "previous,current,level,title",
    [
        ("healthy", "degraded", "warning", "WAMF health degraded"),
        ("healthy", "unhealthy", "error", "WAMF health unhealthy"),
        ("degraded", "unhealthy", "error", "WAMF health unhealthy"),
        ("degraded", "healthy", "info", "WAMF health recovered"),
        ("unhealthy", "healthy", "info", "WAMF health recovered"),
        ("unhealthy", "degraded", "warning", "WAMF health improved"),
    ],
)
def test_health_transition_publishes_one_event(previous, current, level, title):
    record_health_transition(_health(previous), {})

    with patch("app.health.post_health_event", return_value=True) as mock_post:
        assert record_health_transition(_health(current), {}) is True

    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["level"] == level
    assert mock_post.call_args.kwargs["title"] == title
    details = mock_post.call_args.kwargs["details"]
    assert details
    assert "None" not in details


@pytest.mark.parametrize("state", ["healthy", "degraded", "unhealthy"])
def test_unchanged_health_state_does_not_publish(state):
    record_health_transition(_health(state), {})

    with patch("app.health.post_health_event") as mock_post:
        assert record_health_transition(_health(state), {}) is False

    mock_post.assert_not_called()


def test_health_state_updates_when_bridge_delivery_fails():
    record_health_transition(_health("healthy"), {})

    with patch("app.health.post_health_event", return_value=False) as mock_post:
        assert record_health_transition(_health("degraded"), {}) is False
        assert record_health_transition(_health("degraded"), {}) is False

    mock_post.assert_called_once()
    assert health_module._previous_health_state == "degraded"


def test_overall_health_state_uses_existing_check_results():
    assert health_module._overall_health_state(_health("healthy")) == "healthy"
    assert health_module._overall_health_state(_health("degraded")) == "degraded"
    assert health_module._overall_health_state(_health("unhealthy")) == "unhealthy"


def test_health_calculation_does_not_record_transition():
    with patch("app.health.load_config", return_value={"frigate": {}}), patch(
        "app.health.record_health_transition"
    ) as mock_record, patch("app.health.requests.get"), patch(
        "app.health.mqtt.Client"
    ), patch(
        "app.health.connect_db"
    ), patch(
        "app.health.shutil.disk_usage", return_value=(100, 10, 90)
    ), patch(
        "app.health.get_snapshots_path"
    ) as snapshots, patch(
        "app.health.get_clips_path"
    ) as clips:
        snapshots.return_value.exists.return_value = True
        clips.return_value.exists.return_value = True
        calculate_system_health(
            {
                "frigate": {
                    "mqtt_server": "mqtt",
                    "mqtt_port": 1883,
                    "frigate_url": "http://frigate",
                }
            }
        )

    mock_record.assert_not_called()


def test_get_system_health_does_not_record_transition():
    with patch(
        "app.health.calculate_system_health", return_value=_health("healthy")
    ) as mock_calculate, patch("app.health.record_health_transition") as mock_record:
        assert get_system_health()["overall_state"] == "healthy"

    mock_calculate.assert_called_once_with()
    mock_record.assert_not_called()


class _StopAfterWaits:
    def __init__(self, wait_results):
        self.wait_results = iter(wait_results)
        self.wait_calls = []

    def is_set(self):
        return False

    def wait(self, interval):
        self.wait_calls.append(interval)
        return next(self.wait_results)


def test_monitor_records_initial_state_then_publishes_transition():
    stop_event = _StopAfterWaits([False, True])
    config = {"bridge": {"enabled": True, "health_check_interval_seconds": 12}}

    with patch("app.health.load_config", return_value=config), patch(
        "app.health.calculate_system_health",
        side_effect=[_health("healthy"), _health("degraded")],
    ), patch("app.health.post_health_event", return_value=True) as mock_post:
        health_monitor_loop(stop_event)

    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["title"] == "WAMF health degraded"
    assert stop_event.wait_calls == [12.0, 12.0]


def test_monitor_continues_after_health_check_exception():
    stop_event = _StopAfterWaits([False, True])

    with patch("app.health.load_config", return_value={}), patch(
        "app.health.calculate_system_health",
        side_effect=[RuntimeError("check failed"), _health("healthy")],
    ) as mock_calculate, patch("app.health.record_health_transition") as mock_record:
        health_monitor_loop(stop_event)

    assert mock_calculate.call_count == 2
    mock_record.assert_called_once()


def test_monitor_continues_after_bridge_delivery_failure():
    stop_event = _StopAfterWaits([False, True])

    with patch("app.health.load_config", return_value={}), patch(
        "app.health.calculate_system_health", return_value=_health("healthy")
    ), patch("app.health.record_health_transition", return_value=False) as mock_record:
        health_monitor_loop(stop_event)

    assert mock_record.call_count == 2


@pytest.mark.parametrize(
    "bridge_config,expected",
    [
        ({}, 60),
        ({"health_check_interval_seconds": "15"}, 15.0),
        ({"health_check_interval_seconds": 0}, 60),
        ({"health_check_interval_seconds": -1}, 60),
        ({"health_check_interval_seconds": "invalid"}, 60),
        ({"health_check_interval_seconds": float("nan")}, 60),
        ({"health_check_interval_seconds": True}, 60),
    ],
)
def test_health_check_interval_validation(bridge_config, expected):
    assert health_module._health_check_interval(bridge_config) == expected


def test_health_monitor_starts_only_once():
    thread = MagicMock()
    thread.is_alive.return_value = True

    with patch("app.health.threading.Thread", return_value=thread) as thread_class:
        assert start_health_monitor() is thread
        assert start_health_monitor() is thread

    thread_class.assert_called_once_with(
        target=health_monitor_loop,
        name="wamf-health-monitor",
        daemon=True,
    )
    thread.start.assert_called_once_with()
