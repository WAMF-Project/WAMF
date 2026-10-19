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
def reset_previous_health_state(monkeypatch):
    monkeypatch.setattr(health_module, "_detection_worker_enabled", None)
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


@pytest.mark.parametrize('connection,subscription,expected', [
    ('connected', 'subscribed', True), ('subscribing', 'pending', False),
    ('subscribing', 'failed', False), ('reconnecting', 'unknown', False),
])
def test_mqtt_boolean_health_requires_operational_runtime(connection, subscription, expected):
    from app.mqtt_runtime import MqttRuntimeSnapshot
    snapshot = MqttRuntimeSnapshot(worker_generation='test', is_current=True,
                                   connection_state=connection, subscription_state=subscription)
    config = {'mqtt': {'host': 'broker', 'topic_prefix': 'frigate'}, 'frigate': {'frigate_url': 'http://frigate'}}
    with patch('app.health.preflight', return_value=[]), patch('app.health.frigate_health_cache'), patch(
        'app.health.connect_db'
    ), patch('app.health.shutil.disk_usage', return_value=(100, 10, 90)), patch(
        'app.health.get_snapshots_path'
    ), patch('app.health.get_clips_path'), patch('app.mqtt_connection.probe_mqtt_connection') as probe:
        result = calculate_system_health(config, mqtt_runtime_status=lambda: snapshot)
    assert result['mqtt_online'] is expected
    probe.assert_not_called()


@pytest.mark.parametrize('online', [True, False, None])
def test_frigate_health_uses_shared_observation(online):
    config = {'frigate': {'frigate_url': 'http://configured-frigate:5000'}}
    cameras = {'online': online, 'degraded': False, 'rows': [], 'coverage_complete': False}
    with patch('app.health.preflight', return_value=[]), patch(
        'app.health.frigate_health_cache'
    ) as cache, patch('app.health.connect_db'), patch(
        'app.health.shutil.disk_usage', return_value=(100, 10, 90)
    ), patch('app.health.get_snapshots_path'), patch('app.health.get_clips_path'):
        cache.get.return_value = cameras
        result = calculate_system_health(config)
    cache.get.assert_called_once_with(config, refresh=True, interval=60)
    assert result['frigate_online'] is online
    assert result['frigate_cameras'] is cameras


def test_health_load_config_uses_config_env_var(monkeypatch, tmp_path):
    config_path = tmp_path / "custom-config.yml"
    config_path.write_text("""
frigate:
  frigate_url: http://example.invalid
mqtt:
  host: mqtt.example.invalid
  port: 1883
""".lstrip())

    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    assert load_config() == {
        "config_version": 2,
        **yaml.safe_load(config_path.read_text()),
    }


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


@pytest.mark.parametrize('status,degraded', [
    ('missing', True), ('disabled', True), ('detection_disabled', True),
    ('unavailable', True), ('healthy', False), ('unknown', False), ('stale', False),
])
def test_camera_aggregation_and_precedence(status, degraded):
    health = _health('healthy')
    health['frigate_cameras'] = {'degraded': degraded, 'rows': [{'status': status}]}
    assert health_module._overall_health_state(health) == ('degraded' if degraded else 'healthy')
    health['setup_required'] = True
    assert health_module._overall_health_state(health) == 'setup_required'
    health['database_healthy'] = False
    assert health_module._overall_health_state(health) == 'unhealthy'


def test_health_details_one_camera_summary_or_service_problem():
    health = _health('healthy')
    health['frigate_cameras'] = {'warnings': [{'status': 'missing'}] * 3,
                               'summary': '3 missing', 'degraded': True}
    assert health_module._health_details(health) == 'Monitored cameras: 3 missing'
    health['frigate_online'] = False
    assert health_module._health_details(health) == 'Frigate offline'
    health['frigate_online'] = True
    health['frigate_cameras']['access_warning'] = True
    assert health_module._health_details(health) == 'Frigate camera health access unavailable'
    health['frigate_cameras']['warnings'] = []
    assert health_module._overall_health_state(health) == 'degraded'


def test_unknown_service_observation_is_not_reported_offline():
    health = _health('healthy')
    health['frigate_online'] = None
    assert health_module._overall_health_state(health) == 'healthy'
    assert 'offline' not in health_module._health_details(health)


def test_health_calculation_does_not_record_transition():
    with patch("app.health.load_config", return_value={"frigate": {}}), patch(
        "app.health.record_health_transition"
    ) as mock_record, patch("app.health.frigate_health_cache"), patch(
        "app.mqtt_connection.probe_mqtt_connection"
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
                    "frigate_url": "http://frigate",
                    "camera": ["birdcam"],
                },
                "mqtt": {
                    "host": "mqtt",
                    "port": 1883,
                    "topic_prefix": "frigate",
                },
                "classification": {"model": "model.tflite", "threshold": 0.7},
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
    assert health_module._health_check_interval({"bridge": bridge_config}) == expected


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
