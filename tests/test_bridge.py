from unittest.mock import MagicMock, patch

import requests

from integrations.bridge import (
    post_event,
    post_health_event,
    post_observation_event,
)

BRIDGE_CONFIG = {
    "enabled": True,
    "events_url": "http://bridge.test/api/events",
    "timeout_seconds": 0.75,
}


def test_post_event_uses_configured_url_timeout_and_accepts_2xx():
    with patch(
        "integrations.bridge.requests.post",
        return_value=MagicMock(status_code=201),
    ) as mock_post:
        result = post_event(
            BRIDGE_CONFIG,
            category="system",
            level="warning",
            title="WAMF health degraded",
            details="MQTT offline",
        )

    assert result is True
    mock_post.assert_called_once_with(
        "http://bridge.test/api/events",
        json={
            "source": "WAMF",
            "category": "system",
            "level": "warning",
            "title": "WAMF health degraded",
            "details": "MQTT offline",
        },
        timeout=0.75,
    )


def test_health_wrapper_uses_system_category_without_none_values():
    with patch(
        "integrations.bridge.requests.post",
        return_value=MagicMock(status_code=200),
    ) as mock_post:
        assert (
            post_health_event(
                BRIDGE_CONFIG,
                level="info",
                title="WAMF health recovered",
                details=None,
            )
            is True
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["category"] == "system"
    assert payload["details"] == ""
    assert "None" not in str(payload)


def test_disabled_bridge_makes_no_request():
    with patch("integrations.bridge.requests.post") as mock_post:
        assert post_observation_event({"enabled": False}) is False
    mock_post.assert_not_called()


def test_enabled_bridge_posts_expected_payload_and_timeout():
    response = MagicMock(status_code=204)

    with patch(
        "integrations.bridge.requests.post",
        return_value=response,
    ) as mock_post:
        result = post_observation_event(
            BRIDGE_CONFIG,
            common_name="American Robin",
            scientific_name="Turdus migratorius",
            confidence=0.9234,
            camera="birdcam",
            frigate_event="evt-001",
        )

    assert result is True
    mock_post.assert_called_once_with(
        "http://bridge.test/api/events",
        json={
            "source": "WAMF",
            "category": "observation",
            "level": "info",
            "title": "American Robin observed",
            "details": (
                "Camera: birdcam | Scientific: Turdus migratorius | "
                "Confidence: 92.3% | Frigate event: evt-001"
            ),
        },
        timeout=0.75,
    )


def test_missing_optional_fields_do_not_render_none():
    response = MagicMock(status_code=200)

    with patch(
        "integrations.bridge.requests.post",
        return_value=response,
    ) as mock_post:
        assert post_observation_event(BRIDGE_CONFIG) is True

    payload = mock_post.call_args.kwargs["json"]
    assert payload["title"] == "Observation recorded"
    assert payload["details"] == ""
    assert "None" not in str(payload)


def test_connection_failure_does_not_raise():
    with patch(
        "integrations.bridge.requests.post",
        side_effect=requests.ConnectionError("offline"),
    ):
        assert post_observation_event(BRIDGE_CONFIG) is False


def test_timeout_does_not_raise():
    with patch(
        "integrations.bridge.requests.post",
        side_effect=requests.Timeout("slow"),
    ):
        assert post_observation_event(BRIDGE_CONFIG) is False


def test_non_2xx_response_does_not_raise():
    with patch(
        "integrations.bridge.requests.post",
        return_value=MagicMock(status_code=503),
    ):
        assert post_observation_event(BRIDGE_CONFIG) is False


def test_invalid_response_does_not_raise():
    with patch(
        "integrations.bridge.requests.post",
        return_value=object(),
    ):
        assert post_observation_event(BRIDGE_CONFIG) is False
