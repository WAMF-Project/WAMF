import json
from unittest.mock import Mock, patch

import pytest
import requests

from app.frigate_client import FrigateClient, FrigateError


@pytest.mark.parametrize('value', [None, -1, float('nan'), float('inf'), True, '5', {}, []])
def test_stats_unknown_numbers_never_default_to_zero(value):
    from app.frigate_client import normalize_camera_stats
    result = normalize_camera_stats({'cameras': {'Garden': {
        'camera_fps': value, 'process_fps': value, 'detection_fps': value,
        'pid': value, 'detection_enabled': 'false', 'connection_quality': 'unusable',
    }}, 'service': {'last_updated': value, 'uptime': value, 'version': 'PRIVATE'}})
    assert all(result['cameras']['Garden'][field] is None for field in (
        'camera_fps', 'process_fps', 'detection_fps', 'detection_enabled'))
    assert 'PRIVATE' not in repr(result)
    assert 'connection_quality' not in result['cameras']['Garden']


def test_stats_request_is_bounded_normalized_and_closed():
    reply = response()
    reply.iter_content.return_value = [json.dumps({'cameras': {'Garden': {
        'camera_fps': 5, 'process_fps': 5, 'detection_fps': 0,
        'pid': 123, 'ffmpeg_pid': None, 'detection_enabled': True, 'cmdline': 'PRIVATE',
    }}, 'service': {'last_updated': 10000, 'uptime': 1000}, 'cpu_usages': 'PRIVATE'}).encode()]
    with patch('app.frigate_client.requests.get', return_value=reply) as get:
        result = FrigateClient('https://frigate/prefix/').get_camera_stats()
    get.assert_called_once_with('https://frigate/prefix/api/stats', timeout=(2, 2),
                               stream=True, allow_redirects=False, verify=True)
    reply.close.assert_called_once_with()
    assert result['cameras']['Garden']['detection_fps'] == 0
    assert result['cameras']['Garden']['pid'] == 123
    assert 'PRIVATE' not in repr(result)


@pytest.mark.parametrize('status,code', [(401, 'auth_rejected'), (403, 'auth_rejected'),
    (302, 'redirect'), (500, 'http_error')])
def test_stats_http_failures_safe_and_closed(status, code):
    reply = response(status_code=status)
    with patch('app.frigate_client.requests.get', return_value=reply), pytest.raises(FrigateError) as error:
        FrigateClient('http://PRIVATE:PRIVATE@host').get_camera_stats()
    assert error.value.code == code
    assert 'PRIVATE' not in str(error.value)
    reply.close.assert_called_once_with()


@pytest.mark.parametrize('body', [b'<html>PRIVATE</html>', b'[]', b'{}', b'{"cameras":[]}',
    b'{"cameras":{"Garden":5}}', b'x' * (4 * 1024 * 1024 + 1)])
def test_stats_malformed_or_oversized_response(body):
    reply = response()
    reply.iter_content.return_value = [body]
    with patch('app.frigate_client.requests.get', return_value=reply):
        if body == b'{"cameras":{"Garden":5}}':
            assert FrigateClient('http://frigate').get_camera_stats()['cameras']['Garden']['camera_fps'] is None
        else:
            with pytest.raises(FrigateError) as error:
                FrigateClient('http://PRIVATE:PRIVATE@host').get_camera_stats()
            assert error.value.code == 'unexpected_response'
            assert 'PRIVATE' not in str(error.value)
    reply.close.assert_called_once_with()


@pytest.mark.parametrize('error,code', [(requests.Timeout, 'timeout'),
    (requests.ConnectionError, 'connection_error'), (requests.exceptions.SSLError, 'tls_error')])
@pytest.mark.parametrize('during_read', [False, True])
def test_stats_transport_error_is_safe(error, code, during_read, caplog):
    reply = response()
    reply.iter_content.side_effect = error('PRIVATE')
    with patch('app.frigate_client.requests.get', return_value=reply,
               side_effect=None if during_read else error('PRIVATE')):
        with pytest.raises(FrigateError) as caught:
            FrigateClient('http://PRIVATE:PRIVATE@host').get_camera_stats()
    assert caught.value.code == code
    assert 'PRIVATE' not in str(caught.value) + caplog.text
    if during_read:
        reply.close.assert_called_once_with()


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


@pytest.mark.parametrize("base_url", ["https://frigate", "https://frigate/", "https://frigate/prefix/"])
@pytest.mark.parametrize("body", [b"Frigate is running. Alive and healthy!", b" \r\nFrigate is running. Alive and healthy!\t "])
def test_connection_probe_validates_one_request_and_closes(base_url, body):
    reply = response()
    reply.iter_content.return_value = [body[:10], body[10:]]
    client = FrigateClient(base_url)
    with patch("app.frigate_client.requests.get", return_value=reply) as get:
        assert client.check_api_connection() == "success"
    get.assert_called_once_with(
        base_url.rstrip("/") + "/api/", timeout=(2, 2), stream=True,
        allow_redirects=False, verify=True,
    )
    reply.close.assert_called_once_with()
    assert client.base_url == base_url.rstrip("/")


@pytest.mark.parametrize("status,code", [(401, "auth_rejected"), (403, "auth_rejected"),
    (301, "redirect"), (302, "redirect"), (307, "redirect"), (404, "http_error"),
    (503, "http_error"), (204, "http_error")])
def test_probe_http_failures_do_not_read_body(status, code):
    reply = response(status_code=status)
    with patch("app.frigate_client.requests.get", return_value=reply) as get:
        assert FrigateClient("http://frigate").check_api_connection() == code
    get.assert_called_once()
    reply.iter_content.assert_not_called()
    reply.close.assert_called_once_with()


@pytest.mark.parametrize("body", [b"", b"0.18.0", b"<html>PRIVATE login</html>", b"PRIVATE", b"x" * 1025])
def test_probe_rejects_unrecognized_body(body):
    reply = response()
    reply.iter_content.return_value = [body]
    with patch("app.frigate_client.requests.get", return_value=reply):
        assert FrigateClient("http://frigate").check_api_connection() == "unexpected_response"
    reply.close.assert_called_once_with()


def test_probe_stops_reading_large_response():
    reply = response()
    def chunks():
        yield b"x" * 1024
        yield b"x"
        pytest.fail("must stop at the response limit")
    reply.iter_content.return_value = chunks()
    with patch("app.frigate_client.requests.get", return_value=reply):
        assert FrigateClient("http://frigate").check_api_connection() == "unexpected_response"
    reply.iter_content.assert_called_once_with(chunk_size=256)
    reply.close.assert_called_once_with()


@pytest.mark.parametrize("error,code", [(requests.ConnectionError, "connection_error"),
    (requests.exceptions.SSLError, "tls_error"), (requests.Timeout, "timeout")])
@pytest.mark.parametrize("during_read", [False, True])
def test_probe_request_errors_are_safe_and_cleanup(error, code, during_read, caplog):
    reply = response()
    if during_read:
        reply.iter_content.side_effect = error("PRIVATE-URL-PASSWORD")
    with patch("app.frigate_client.requests.get", return_value=reply,
               side_effect=None if during_read else error("PRIVATE-URL-PASSWORD")) as get:
        result = FrigateClient("http://PRIVATE:PRIVATE@host").check_api_connection()
    assert result == code
    assert "PRIVATE" not in result + caplog.text
    get.assert_called_once()
    if during_read:
        reply.close.assert_called_once_with()


@pytest.mark.parametrize("base_url", ["https://frigate", "https://frigate/", "https://frigate/prefix/"])
def test_camera_discovery_normalizes_only_camera_fields(base_url):
    config = {"mqtt": {"password": "PRIVATE"}, "go2rtc": {"streams": "PRIVATE"},
        "cameras": {
            "Garden": {"friendly_name": " Garden birds ", "enabled": True,
                       "detect": {"enabled": False}, "ffmpeg": "PRIVATE"},
            "garden": {"enabled": False},
            "Side": {"friendly_name": "", "enabled": "yes", "detect": []},
        }}
    reply = response()
    reply.iter_content.return_value = [json.dumps(config).encode()]
    with patch("app.frigate_client.requests.get", return_value=reply) as get:
        result = FrigateClient(base_url).get_camera_inventory()
    assert result == [
        {"id": "Garden", "display_name": "Garden birds", "enabled": True, "detection_enabled": False},
        {"id": "garden", "display_name": "garden", "enabled": False, "detection_enabled": None},
        {"id": "Side", "display_name": "Side", "enabled": None, "detection_enabled": None},
    ]
    assert "PRIVATE" not in repr(result)
    get.assert_called_once_with(base_url.rstrip("/") + "/api/config", timeout=(2, 2),
                                stream=True, allow_redirects=False, verify=True)
    reply.close.assert_called_once_with()


@pytest.mark.parametrize("friendly", [None, "", "   ", [], 7, "bad\nname", "x" * 257])
def test_discovery_optional_friendly_name_is_defensive(friendly):
    reply = response()
    reply.iter_content.return_value = [json.dumps({"cameras": {"Exact_ID": {"friendly_name": friendly}}}).encode()]
    with patch("app.frigate_client.requests.get", return_value=reply):
        assert FrigateClient("http://frigate").get_camera_inventory() == [
            {"id": "Exact_ID", "display_name": "Exact_ID", "enabled": None, "detection_enabled": None},
        ]
    reply.close.assert_called_once_with()


def test_discovery_empty_inventory_is_successful():
    reply = response()
    reply.iter_content.return_value = [b'{"cameras": {}}']
    with patch("app.frigate_client.requests.get", return_value=reply):
        assert FrigateClient("http://frigate").get_camera_inventory() == []
    reply.close.assert_called_once_with()


@pytest.mark.parametrize("body", [b"PRIVATE invalid JSON", b"null", b"[]", b"{}", b'{"cameras": null}',
    b'{"cameras": []}', b'{"cameras": {"bad": null}}', b'{"cameras": {"bad": []}}',
    b'{"cameras": {"": {}}}', b'\xff'])
def test_discovery_invalid_response_is_safe(body, caplog):
    from app.frigate_client import FrigateDiscoveryError
    reply = response()
    reply.iter_content.return_value = [body]
    with patch("app.frigate_client.requests.get", return_value=reply), pytest.raises(FrigateDiscoveryError) as error:
        FrigateClient("http://PRIVATE:PRIVATE@frigate").get_camera_inventory()
    assert error.value.code == "unexpected_response"
    assert "PRIVATE" not in str(error.value) + caplog.text
    reply.close.assert_called_once_with()


@pytest.mark.parametrize("status,code", [(401, "auth_rejected"), (403, "auth_rejected"),
    (302, "redirect"), (404, "http_error"), (500, "http_error"), (204, "http_error")])
def test_discovery_http_failure_closes_response(status, code):
    from app.frigate_client import FrigateDiscoveryError
    reply = response(status_code=status)
    with patch("app.frigate_client.requests.get", return_value=reply) as get, pytest.raises(FrigateDiscoveryError) as error:
        FrigateClient("http://frigate").get_camera_inventory()
    assert error.value.code == code
    get.assert_called_once()
    reply.iter_content.assert_not_called()
    reply.close.assert_called_once_with()


@pytest.mark.parametrize("error,code", [(requests.Timeout, "timeout"),
    (requests.ConnectionError, "connection_error"), (requests.exceptions.SSLError, "tls_error")])
@pytest.mark.parametrize("during_read", [False, True])
def test_discovery_transport_failures(error, code, during_read, caplog):
    from app.frigate_client import FrigateDiscoveryError
    reply = response()
    reply.iter_content.side_effect = error("PRIVATE")
    with patch("app.frigate_client.requests.get", return_value=reply,
               side_effect=None if during_read else error("PRIVATE")) as get, pytest.raises(FrigateDiscoveryError) as caught:
        FrigateClient("http://PRIVATE:PRIVATE@frigate").get_camera_inventory()
    assert caught.value.code == code
    assert "PRIVATE" not in str(caught.value) + caplog.text
    get.assert_called_once()
    if during_read:
        reply.close.assert_called_once_with()


def test_discovery_response_limit():
    from app.frigate_client import FrigateDiscoveryError
    reply = response()
    def chunks():
        for _ in range(513):
            yield b"x" * 8192
        pytest.fail("response limit must stop reading")
    reply.iter_content.return_value = chunks()
    with patch("app.frigate_client.requests.get", return_value=reply), pytest.raises(FrigateDiscoveryError) as error:
        FrigateClient("http://frigate").get_camera_inventory()
    assert error.value.code == "unexpected_response"
    reply.close.assert_called_once_with()
