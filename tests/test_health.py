from unittest.mock import patch

import pytest
import yaml

import app.health as health_module
from app.health import load_config, record_health_transition


@pytest.fixture(autouse=True)
def reset_previous_health_state():
    health_module._previous_health_state = None
    yield
    health_module._previous_health_state = None


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
