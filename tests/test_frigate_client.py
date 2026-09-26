import json
from unittest.mock import Mock, patch

import pytest
import requests

from app.frigate_client import FrigateClient, FrigateError


def response(*, status_code=200, content=b"image", text="0.14.1"):
    result = Mock(spec=requests.Response)
    result.status_code = status_code
    result.content = content
    result.text = text
    if status_code >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status_code}", response=result
        )
    return result


@pytest.mark.parametrize(
    "base_url",
    ["http://frigate:5000", "http://frigate:5000/"],
)
def test_base_url_and_trailing_slash_handling(base_url):
    with patch("app.frigate_client.requests.get", return_value=response()) as get:
        FrigateClient(base_url).get_version()

    get.assert_called_once_with("http://frigate:5000/api/version", timeout=5)


def test_set_event_sub_label_uses_current_payload_endpoint_and_timeout():
    with patch("app.frigate_client.requests.post", return_value=response()) as post:
        result = FrigateClient("http://frigate").set_event_sub_label(
            "event-123", "American Robin"
        )

    assert result is None
    post.assert_called_once_with(
        "http://frigate/api/events/event-123/sub_label",
        data=json.dumps({"subLabel": "American Robin"}),
        headers={"Content-Type": "application/json"},
        timeout=2,
    )


def test_set_event_sub_label_preserves_twenty_character_limit():
    with patch("app.frigate_client.requests.post", return_value=response()) as post:
        FrigateClient("http://frigate").set_event_sub_label(
            "event-123", "Black-capped Chickadee"
        )

    assert json.loads(post.call_args.kwargs["data"]) == {
        "subLabel": "Black-capped Chickad"
    }


def test_cropped_snapshot_uses_crop_quality_and_classifier_timeout():
    snapshot = response(content=b"cropped jpeg")
    with patch("app.frigate_client.requests.get", return_value=snapshot) as get:
        result = FrigateClient("http://frigate/").get_event_snapshot(
            "event-123", crop=True, quality=95
        )

    assert result == b"cropped jpeg"
    get.assert_called_once_with(
        "http://frigate/api/events/event-123/snapshot.jpg",
        params={"crop": 1, "quality": 95},
        timeout=2,
    )


def test_uncropped_snapshot_omits_params_and_uses_archive_timeout():
    with patch("app.frigate_client.requests.get", return_value=response()) as get:
        FrigateClient("http://frigate").get_event_snapshot("event-123")

    get.assert_called_once_with(
        "http://frigate/api/events/event-123/snapshot.jpg",
        params=None,
        timeout=10,
    )


def test_snapshot_quality_can_be_sent_without_crop():
    with patch("app.frigate_client.requests.get", return_value=response()) as get:
        FrigateClient("http://frigate").get_event_snapshot(
            "event-123", quality=80
        )

    assert get.call_args.kwargs["params"] == {"quality": 80}


def test_version_returns_response_text():
    with patch(
        "app.frigate_client.requests.get",
        return_value=response(text="0.14.1"),
    ):
        assert FrigateClient("http://frigate").get_version() == "0.14.1"


@pytest.mark.parametrize(
    ("media_name", "timeout"),
    [("thumbnail.jpg", 5), ("snapshot.jpg", 5), ("clip.mp4", 30)],
)
def test_media_request_is_streamed_with_existing_timeouts(media_name, timeout):
    streamed_response = response()
    with patch(
        "app.frigate_client.requests.get", return_value=streamed_response
    ) as get:
        result = FrigateClient("http://frigate").stream_event_media(
            "event-123", media_name
        )

    assert result is streamed_response
    streamed_response.close.assert_not_called()
    get.assert_called_once_with(
        f"http://frigate/api/events/event-123/{media_name}",
        stream=True,
        timeout=timeout,
    )


@pytest.mark.parametrize(
    ("operation", "request_target"),
    [
        (
            lambda client: client.set_event_sub_label("event-123", "Robin"),
            "app.frigate_client.requests.post",
        ),
        (
            lambda client: client.get_event_snapshot("event-123"),
            "app.frigate_client.requests.get",
        ),
        (
            lambda client: client.stream_event_media("event-123", "clip.mp4"),
            "app.frigate_client.requests.get",
        ),
        (
            lambda client: client.get_version(),
            "app.frigate_client.requests.get",
        ),
    ],
)
def test_non_success_http_response_raises_frigate_error(operation, request_target):
    http_error = requests.HTTPError("HTTP 503")
    failed_response = response(status_code=503)
    failed_response.raise_for_status.side_effect = http_error

    with patch(
        request_target,
        return_value=failed_response,
    ), pytest.raises(FrigateError) as raised:
        operation(FrigateClient("http://frigate"))

    assert raised.value.status_code == 503
    assert raised.value.__cause__ is http_error


def test_redirect_response_exposes_status_without_requests_inspection():
    redirected_response = response(status_code=302)

    with patch(
        "app.frigate_client.requests.get",
        return_value=redirected_response,
    ), pytest.raises(FrigateError) as raised:
        FrigateClient("http://frigate").get_version()

    assert raised.value.status_code == 302


def test_rejected_streaming_response_is_closed_before_error_is_raised():
    failed_response = response(status_code=503)

    with patch(
        "app.frigate_client.requests.get",
        return_value=failed_response,
    ), pytest.raises(FrigateError) as raised:
        FrigateClient("http://frigate").stream_event_media(
            "event-123", "clip.mp4"
        )

    assert raised.value.status_code == 503
    failed_response.close.assert_called_once_with()


def test_request_failure_is_translated_and_chained():
    connection_error = requests.ConnectionError("connection refused")
    with patch(
        "app.frigate_client.requests.get", side_effect=connection_error
    ), pytest.raises(FrigateError) as raised:
        FrigateClient("http://frigate").get_version()

    assert raised.value.status_code is None
    assert raised.value.__cause__ is connection_error
