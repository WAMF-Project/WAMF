"""Unit tests for webui.py Flask routes."""
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_detection(det_db, frigate_event="evt-test-999", display_name="Turdus migratorius"):
    conn = sqlite3.connect(det_db)
    conn.execute(
        """INSERT INTO detections
               (detection_time, detection_index, score, display_name, category_name, frigate_event, camera_name, wamf_snapshot_path, wamf_clip_path)
           VALUES ('2024-06-02 10:00:00.000000', 99, 0.9, ?, 'bird', ?, 'birdcam', NULL, NULL)""",
        (display_name, frigate_event),
    )
    conn.commit()
    conn.close()


def _delete_detection(det_db, frigate_event):
    conn = sqlite3.connect(det_db)
    conn.execute("DELETE FROM detections WHERE frigate_event = ?", (frigate_event,))
    conn.commit()
    conn.close()


def _insert_detection_with_media(det_db, frigate_event, snapshot_path, clip_path):
    conn = sqlite3.connect(det_db)
    conn.execute(
        """INSERT INTO detections
               (detection_time, detection_index, score, display_name, category_name, frigate_event, camera_name, wamf_snapshot_path, wamf_clip_path)
           VALUES ('2024-06-02 10:00:00.000000', 99, 0.9, 'Turdus migratorius', 'bird', ?, 'birdcam', ?, ?)""",
        (frigate_event, str(snapshot_path), str(clip_path)),
    )
    conn.commit()
    conn.close()


def _login_as_admin(flask_client):
    flask_client.post(
        "/login",
        data={"password": "secret"},
        follow_redirects=False,
    )
    with flask_client.session_transaction() as sess:
        return sess["csrf_token"]


def _set_admin_api_auth(webui, monkeypatch, api_token=None):
    from werkzeug.security import generate_password_hash

    api_config = {
        "token_auth_enabled": True,
        "token_hash": generate_password_hash(api_token) if api_token else "",
    }
    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
        "api": api_config,
    })
    webui.app.secret_key = "test-secret"


def test_live_view_uses_camera_config(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "camera": {"live_view_url": "https://camera.example/live"},
        "live_view": {"url": "https://legacy.example/live"},
    })

    response = flask_client.get("/live")

    assert response.status_code == 200
    assert b'https://camera.example/live' in response.data


def test_live_view_keeps_legacy_config_fallback(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "live_view": {"url": "https://legacy.example/live"},
    })
    webui.config.pop("camera", None)

    response = flask_client.get("/live")

    assert response.status_code == 200
    assert b'https://legacy.example/live' in response.data


def _stub_admin_api_dependencies(webui, monkeypatch):
    monkeypatch.setattr(webui, "get_system_health", lambda: {
        "frigate_online": True,
        "mqtt_online": True,
        "database_healthy": True,
        "archive_writable": True,
        "disk_used_percent": 10,
    })
    monkeypatch.setattr(webui, "get_retention_status", lambda: {
        "last_run": None,
        "orphan_count": 0,
        "missing_count": 0,
    })


def _extract_generated_token(response):
    match = re.search(rb"<code>([^<]+)</code>", response.data)
    assert match
    return match.group(1).decode("utf-8")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def test_index_returns_200(flask_client):
    response = flask_client.get("/")
    assert response.status_code == 200


def test_index_contains_html(flask_client):
    response = flask_client.get("/")
    assert b"<!DOCTYPE html>" in response.data or b"<html" in response.data


def test_index_handles_empty_database(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(webui, "get_latest_visitor", lambda: None)

    response = flask_client.get("/")

    assert response.status_code == 200
    assert b"No detections yet" in response.data


def test_expected_blueprint_endpoints_are_registered(flask_client):
    import webui

    endpoints = {
        rule.endpoint
        for rule in webui.app.url_map.iter_rules()
    }

    assert {
        "public.index",
        "public.recent_feed",
        "api.api_recent_detections",
        "admin.admin_dashboard",
        "admin_api.admin_health",
        "auth.login",
        "auth.logout",
        "detections.delete_detection",
        "media.wamf_snapshot",
        "media.wamf_clip",
    }.issubset(endpoints)


def test_public_pages_do_not_run_admin_health_checks(flask_client, monkeypatch):
    import webui

    def fail_health_check():
        raise AssertionError("public pages should not run admin health checks")

    monkeypatch.setattr(webui, "get_system_health", fail_health_check)
    response = flask_client.get("/")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# /daily_summary redirect
# ---------------------------------------------------------------------------

def test_daily_summary_redirect(flask_client):
    response = flask_client.get("/daily_summary")
    assert response.status_code == 302
    assert "/daily_summary/20" in response.headers["Location"]


def test_daily_summary_redirect_preserves_query(flask_client):
    response = flask_client.get("/daily_summary?live=true")
    assert response.status_code == 302
    assert "live=true" in response.headers["Location"]


@pytest.mark.parametrize("path", ["/daily_summary", "/daily_summary/"])
@pytest.mark.parametrize("query", [
    "live=true&tag=one&tag=two&empty=&flag",
    "q=blue+tit&name=blue%20tit&encoded=%26%3D%3F%23&literal=%252F",
    "next=https://evil.example/path&url=//evil.example",
    "//evil.example/path?next=https://evil.example",
    "next=%2F%5Cevil.example&value=%0D%0ALocation%3A%20https%3A%2F%2Fevil.example",
    "next=%252F%252Fevil.example&value=%255C%250D%250A",
    "next=%25252F%25252Fevil.example&value=%25255C%25250D%25250A",
])
def test_daily_summary_query_cannot_change_redirect_destination(flask_client, path, query):
    from urllib.parse import urlsplit

    response = flask_client.get(f"{path}?{query}")
    assert response.status_code == 302
    target = urlsplit(response.headers["Location"])
    assert target.scheme == ""
    assert target.netloc == ""
    assert re.fullmatch(r"/daily_summary/\d{4}-\d{2}-\d{2}", target.path)
    assert target.query == query
    assert target.fragment == ""


def test_daily_summary_date_returns_200(flask_client):
    response = flask_client.get("/daily_summary/2024-06-01")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# /api/detections/recent
# ---------------------------------------------------------------------------

def test_api_recent_detections_default(flask_client):
    response = flask_client.get("/api/detections/recent")
    assert response.status_code == 200
    data = json.loads(response.data)
    assert isinstance(data, list)
    assert len(data) <= 5


def test_api_recent_detections_custom_limit(flask_client):
    response = flask_client.get("/api/detections/recent?limit=2")
    assert response.status_code == 200
    data = json.loads(response.data)
    assert len(data) <= 2


def test_api_recent_detections_cap_at_20(flask_client):
    response = flask_client.get("/api/detections/recent?limit=100")
    assert response.status_code == 200
    data = json.loads(response.data)
    assert len(data) <= 20


# ---------------------------------------------------------------------------
# DELETE /detections/<frigate_event>
# ---------------------------------------------------------------------------

def test_delete_detection_success(flask_client, tmp_dbs):
    _insert_detection(tmp_dbs["det_db"], frigate_event="evt-delete-me")
    response = flask_client.delete("/detections/evt-delete-me")
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data["success"] is True
    assert data["frigate_event"] == "evt-delete-me"


def test_delete_detection_not_found(flask_client):
    response = flask_client.delete("/detections/evt-does-not-exist")
    assert response.status_code == 404
    data = json.loads(response.data)
    assert data["success"] is False


def test_delete_detection_idempotent(flask_client, tmp_dbs):
    """Second delete of same event returns 404, not 500."""
    _insert_detection(tmp_dbs["det_db"], frigate_event="evt-idempotent")
    flask_client.delete("/detections/evt-idempotent")
    response = flask_client.delete("/detections/evt-idempotent")
    assert response.status_code == 404


def test_delete_detection_removes_archived_media(
    flask_client, tmp_dbs, tmp_path, monkeypatch
):
    import webui

    snapshot_root = tmp_path / "snapshots"
    clip_root = tmp_path / "clips"
    monkeypatch.setattr(webui, "get_snapshots_path", lambda: snapshot_root)
    monkeypatch.setattr(webui, "get_clips_path", lambda: clip_root)
    monkeypatch.setattr(
        webui,
        "resolve_media_path",
        lambda value, media_type: (
            snapshot_root if media_type == "snapshots" else clip_root
        ) / Path(value).name,
    )

    snapshot_path = snapshot_root / "test-delete-media.jpg"
    clip_path = clip_root / "test-delete-media.mp4"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(b"snapshot")
    clip_path.write_bytes(b"clip")

    _insert_detection_with_media(
        tmp_dbs["det_db"],
        "evt-delete-media",
        snapshot_path,
        clip_path
    )

    response = flask_client.delete("/detections/evt-delete-media")
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data["success"] is True
    assert sorted(data["deleted_media"]) == sorted([
        str(snapshot_path),
        str(clip_path),
    ])
    assert not snapshot_path.exists()
    assert not clip_path.exists()


# ---------------------------------------------------------------------------
# /detections/by_hour
# ---------------------------------------------------------------------------

def test_detections_by_hour_returns_200(flask_client):
    response = flask_client.get("/detections/by_hour/2024-06-01/8")
    assert response.status_code == 200


def test_detections_by_hour_empty_hour_returns_200(flask_client):
    response = flask_client.get("/detections/by_hour/2024-06-01/23")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# /frigate proxy routes — streaming and error handling
# ---------------------------------------------------------------------------

@pytest.fixture
def frigate_client(flask_client):
    import webui

    # Avoid preserved test contexts interfering with cancelled streaming contexts.
    return webui.app.test_client()


@pytest.fixture
def frigate_upstream(monkeypatch):
    upstream = Mock(status_code=200, headers={"Content-Type": "image/jpeg"})
    upstream.raw.stream.return_value = iter([b"media"])
    get = Mock(return_value=upstream)
    monkeypatch.setattr("app.frigate_proxy.requests.get", get)
    return upstream, get


def _assert_frigate_fallback(response):
    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.data == (
        Path(__file__).resolve().parents[1] / "static/images/1x1.png"
    ).read_bytes()
    response.close()


@pytest.mark.parametrize("media,content_type,timeout", [
    ("thumbnail.jpg", "image/jpeg", 5),
    ("snapshot.jpg", "image/jpeg", 5),
    ("clip.mp4", "video/mp4", 30),
])
def test_frigate_media_streams_and_closes(
    frigate_client, frigate_upstream, monkeypatch, media, content_type, timeout
):
    import webui

    upstream, get = frigate_upstream
    # Preserve a configured base path and credentials without exposing them.
    base_url = "http://user:secret@frigate.example/base"
    monkeypatch.setitem(webui.config["frigate"], "frigate_url", base_url)
    upstream.headers = {"Content-Type": content_type}
    consumed = []

    def chunks():
        for chunk in [b"a" * 8192, b"", b"last"]:
            consumed.append(chunk)
            yield chunk

    upstream.raw.stream.return_value = chunks()
    event = "1710000000.123456-abc123"
    response = frigate_client.get(f"/frigate/{event}/{media}", buffered=False)
    assert response.status_code == 200
    assert response.content_type == content_type
    assert response.cache_control.no_cache
    assert response.is_streamed
    assert len(consumed) == 1
    upstream.close.assert_not_called()
    assert response.data == b"a" * 8192 + b"last"
    upstream.close.assert_called_once_with()
    response.close()
    upstream.close.assert_called_once_with()
    upstream.raw.stream.assert_called_once_with(8192, decode_content=False)
    get.assert_called_once_with(
        f"{base_url}/api/events/{event}/{media}",
        stream=True, timeout=timeout, allow_redirects=False,
        headers={"Accept-Encoding": "identity"},
    )


@pytest.mark.parametrize("media", ["thumbnail.jpg", "snapshot.jpg", "clip.mp4"])
@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_frigate_request_failure_returns_fallback(
    frigate_client, frigate_upstream, caplog, media, error
):
    _, get = frigate_upstream
    get.side_effect = error("http://user:secret@private-frigate.example/")
    _assert_frigate_fallback(frigate_client.get(f"/frigate/evt-test/{media}"))
    assert "secret" not in caplog.text
    assert "private-frigate" not in caplog.text


@pytest.mark.parametrize("status", [201, 301, 302, 307, 308, 404, 500])
@pytest.mark.parametrize("media", ["thumbnail.jpg", "snapshot.jpg", "clip.mp4"])
def test_frigate_non_200_closes_and_returns_fallback(
    frigate_client, frigate_upstream, media, status
):
    upstream, get = frigate_upstream
    upstream.status_code = status
    upstream.headers["Location"] = "http://other-host.example/media"
    _assert_frigate_fallback(frigate_client.get(f"/frigate/evt-test/{media}"))
    upstream.close.assert_called_once_with()
    upstream.raw.stream.assert_not_called()
    assert get.call_count == 1
    assert get.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("event", [
    ".", "..", "evt%3Fquery", "evt%23fragment", "evt%25encoded",
    "evt%5Cpath", "evt%20space", "evt%0Aline", "evt%252Fpath", "caf%C3%A9",
])
def test_frigate_malformed_event_returns_fallback_without_request(
    frigate_client, frigate_upstream, event
):
    _, get = frigate_upstream
    _assert_frigate_fallback(frigate_client.get(f"/frigate/{event}/thumbnail.jpg"))
    get.assert_not_called()


def test_frigate_encoded_slash_is_rejected(frigate_client, frigate_upstream):
    _, get = frigate_upstream
    response = frigate_client.get("/frigate/evt%2Fother/thumbnail.jpg")
    assert response.status_code == 404
    get.assert_not_called()


def test_frigate_initial_read_timeout_closes_and_returns_fallback(
    frigate_client, frigate_upstream
):
    upstream, _ = frigate_upstream

    def chunks():
        raise requests.Timeout("read timed out")
        yield  # Make the timeout occur while advancing the iterator.

    upstream.raw.stream.return_value = chunks()
    _assert_frigate_fallback(frigate_client.get("/frigate/evt-test/clip.mp4"))
    upstream.close.assert_called_once_with()


@pytest.mark.parametrize("error", [requests.ConnectionError, RuntimeError])
def test_frigate_midstream_failure_closes(frigate_client, frigate_upstream, error):
    upstream, _ = frigate_upstream

    def chunks():
        yield b"first"
        raise error("http://user:secret@private-frigate.example/")

    upstream.raw.stream.return_value = chunks()
    response = frigate_client.get("/frigate/evt-test/clip.mp4", buffered=False)
    expected = requests.RequestException if error is requests.ConnectionError else error
    with pytest.raises(expected) as raised:
        response.get_data()
    if error is requests.ConnectionError:
        assert str(raised.value) == "Frigate media stream failed"
        assert raised.value.__suppress_context__
    upstream.close.assert_called_once_with()
    response.close()


def test_frigate_unexpected_initial_read_failure_closes(frigate_client, frigate_upstream):
    upstream, _ = frigate_upstream
    upstream.raw.stream.side_effect = RuntimeError("unexpected failure")
    with pytest.raises(RuntimeError, match="unexpected failure"):
        frigate_client.get("/frigate/evt-test/clip.mp4")
    upstream.close.assert_called_once_with()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_frigate_cancelled_stream_closes(frigate_client, frigate_upstream, method):
    upstream, _ = frigate_upstream

    def chunks():
        yield b"first"
        pytest.fail("Cancelled streams must not read the remaining media")

    upstream.raw.stream.return_value = chunks()
    response = frigate_client.open(
        "/frigate/evt-test/clip.mp4", method=method, buffered=False
    )
    response.close()
    upstream.close.assert_called_once_with()


@pytest.mark.parametrize("content_type", [
    'video/mp4; codecs="avc1.640028, mp4a.40.2"',
    "text/plain; charset=iso-8859-1",
])
def test_frigate_preserves_exact_content_type(
    frigate_client, frigate_upstream, content_type
):
    upstream, _ = frigate_upstream
    upstream.headers["Content-Type"] = content_type
    response = frigate_client.get("/frigate/evt-test/clip.mp4")
    assert response.content_type == content_type
    assert response.data == b"media"
    response.close()
    upstream.close.assert_called_once_with()


@pytest.mark.parametrize("byte_range,content_range", [
    ("bytes=0-4", "bytes 0-4/20"),
    ("bytes=15-", "bytes 15-19/20"),
    ("bytes=-5", "bytes 15-19/20"),
])
def test_frigate_clip_partial_content(
    frigate_client, frigate_upstream, byte_range, content_range
):
    upstream, get = frigate_upstream
    upstream.status_code = 206
    upstream.headers = {
        "Content-Type": "video/mp4", "Content-Range": content_range,
        "Content-Length": "5", "Accept-Ranges": "bytes",
        "ETag": '"clip-version"', "Last-Modified": "Mon, 26 Oct 2026 12:00:00 GMT",
        "Set-Cookie": "upstream=secret", "Location": "http://private.example/",
        "Transfer-Encoding": "chunked", "Connection": "keep-alive",
        "X-Accel-Redirect": "/private/clip.mp4",
    }
    response = frigate_client.get(
        "/frigate/evt-test/clip.mp4",
        headers={"Range": byte_range, "Authorization": "Bearer private-token"},
    )
    assert response.status_code == 206
    assert response.content_type == "video/mp4"
    for name in ("Content-Range", "Content-Length", "Accept-Ranges", "ETag", "Last-Modified"):
        assert response.headers[name] == upstream.headers[name]
    for name in ("Set-Cookie", "Location", "Transfer-Encoding", "Connection", "X-Accel-Redirect"):
        assert name not in response.headers
    assert response.data == b"media"
    response.close()
    upstream.close.assert_called_once_with()
    assert get.call_args.kwargs["headers"] == {
        "Accept-Encoding": "identity", "Range": byte_range,
    }


def test_frigate_unsatisfiable_range_preserves_416(frigate_client, frigate_upstream):
    upstream, _ = frigate_upstream
    upstream.status_code = 416
    upstream.headers = {
        "Content-Type": "text/plain; charset=utf-8", "Content-Range": "bytes */20",
        "Content-Length": "0", "Accept-Ranges": "bytes",
    }
    upstream.raw.stream.return_value = iter([])
    response = frigate_client.get(
        "/frigate/evt-test/clip.mp4", headers={"Range": "bytes=100-"}
    )
    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */20"
    assert response.content_length == 0
    assert response.data == b""
    response.close()
    upstream.close.assert_called_once_with()


@pytest.mark.parametrize("if_range", ['"clip-version"', "Mon, 26 Oct 2026 12:00:00 GMT"])
def test_frigate_if_range_can_return_full_response(
    frigate_client, frigate_upstream, if_range
):
    upstream, get = frigate_upstream
    upstream.headers.update({"Content-Type": "video/mp4", "Content-Length": "5"})
    response = frigate_client.get(
        "/frigate/evt-test/clip.mp4",
        headers={"Range": "bytes=3-", "If-Range": if_range},
    )
    assert get.call_args.kwargs["headers"]["If-Range"] == if_range
    assert response.status_code == 200
    assert response.content_length == 5
    assert "Content-Range" not in response.headers
    assert response.data == b"media"
    response.close()
    upstream.close.assert_called_once_with()


@pytest.mark.parametrize("byte_range", [
    "bytes=abc", "bytes=10-2", "bytes=-0", "bytes=0-2,4-6",
    "items=0-4", "bytes=" + "9" * 130 + "-",
])
def test_frigate_unsupported_ranges_use_full_response(
    frigate_client, frigate_upstream, byte_range
):
    _, get = frigate_upstream
    response = frigate_client.get(
        "/frigate/evt-test/clip.mp4", headers={"Range": byte_range, "If-Range": '"v1"'}
    )
    assert get.call_args.kwargs["headers"] == {"Accept-Encoding": "identity"}
    assert response.status_code == 200
    assert response.data == b"media"
    response.close()


def test_frigate_oversized_if_range_uses_full_response(frigate_client, frigate_upstream):
    _, get = frigate_upstream
    response = frigate_client.get(
        "/frigate/evt-test/clip.mp4",
        headers={"Range": "bytes=0-4", "If-Range": '"' + "x" * 256 + '"'},
    )
    assert get.call_args.kwargs["headers"] == {"Accept-Encoding": "identity"}
    assert response.data == b"media"
    response.close()


@pytest.mark.parametrize("media,method", [
    ("thumbnail.jpg", "GET"), ("snapshot.jpg", "GET"), ("clip.mp4", "HEAD"),
])
def test_frigate_ranges_only_forwarded_for_clip_get(
    frigate_client, frigate_upstream, media, method
):
    upstream, get = frigate_upstream
    response = frigate_client.open(
        f"/frigate/evt-test/{media}", method=method, headers={"Range": "bytes=0-4"}
    )
    assert "Range" not in get.call_args.kwargs["headers"]
    response.close()
    upstream.close.assert_called_once_with()


@pytest.mark.parametrize("status", [206, 416])
def test_frigate_unsolicited_range_status_uses_fallback(
    frigate_client, frigate_upstream, status
):
    upstream, _ = frigate_upstream
    upstream.status_code = status
    _assert_frigate_fallback(frigate_client.get("/frigate/evt-test/clip.mp4"))
    upstream.close.assert_called_once_with()


def test_frigate_encoded_representation_preserves_bytes(frigate_client, frigate_upstream):
    import gzip

    upstream, _ = frigate_upstream
    encoded = gzip.compress(b"media")
    upstream.raw.stream.return_value = iter([encoded])
    upstream.headers.update({"Content-Encoding": "gzip", "Content-Length": str(len(encoded))})
    response = frigate_client.get("/frigate/evt-test/clip.mp4")
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.content_length == len(encoded)
    assert response.data == encoded
    upstream.raw.stream.assert_called_once_with(8192, decode_content=False)
    upstream.iter_content.assert_not_called()
    response.close()
    upstream.close.assert_called_once_with()


def test_frigate_connection_nominated_headers_are_not_forwarded(
    frigate_client, frigate_upstream
):
    upstream, _ = frigate_upstream
    upstream.headers.update({"Connection": "ETag", "ETag": '"private-hop-value"'})
    response = frigate_client.get("/frigate/evt-test/clip.mp4")
    assert "ETag" not in response.headers
    response.close()


@pytest.mark.parametrize("failure", ["initial", "midstream", "cancelled"])
def test_frigate_partial_stream_cleanup(frigate_client, frigate_upstream, failure):
    from urllib3.exceptions import ReadTimeoutError

    upstream, _ = frigate_upstream
    upstream.status_code = 206
    upstream.headers.update({"Content-Type": "video/mp4", "Content-Range": "bytes 0-9/20"})

    def chunks():
        if failure == "initial":
            raise ReadTimeoutError(None, "http://user:secret@private.example/", "timeout")
        yield b"first"
        if failure == "cancelled":
            pytest.fail("Cancelled partial responses must not read more data")
        raise ReadTimeoutError(None, "http://user:secret@private.example/", "timeout")

    upstream.raw.stream.return_value = chunks()
    response = frigate_client.get(
        "/frigate/evt-test/clip.mp4", headers={"Range": "bytes=0-9"}, buffered=False
    )
    if failure == "initial":
        _assert_frigate_fallback(response)
    elif failure == "midstream":
        with pytest.raises(requests.RequestException, match="^Frigate media stream failed$"):
            response.get_data()
    response.close()
    upstream.close.assert_called_once_with()


@pytest.mark.parametrize("status", [None, 404])
def test_frigate_logs_exclude_request_input(frigate_client, frigate_upstream, caplog, status):
    upstream, get = frigate_upstream
    if status is None:
        get.side_effect = requests.Timeout("http://user:secret@private.example/")
    else:
        upstream.status_code = status
    with caplog.at_level("INFO", logger="app.frigate_proxy"):
        _assert_frigate_fallback(frigate_client.get("/frigate/event-LOG-MARKER/clip.mp4"))
    assert "event-LOG-MARKER" not in caplog.text
    assert "secret" not in caplog.text
    assert "private.example" not in caplog.text
    assert len(caplog.records) == 1


@pytest.mark.parametrize("status", [302, 404, 500])
def test_frigate_range_failure_returns_complete_png(
    frigate_client, frigate_upstream, status
):
    upstream, _ = frigate_upstream
    upstream.status_code = status
    response = frigate_client.get(
        "/frigate/evt-test/clip.mp4", headers={"Range": "bytes=1000-"}
    )
    _assert_frigate_fallback(response)
    upstream.close.assert_called_once_with()


def test_frigate_invalid_upstream_header_closes(frigate_client, frigate_upstream):
    upstream, _ = frigate_upstream
    upstream.headers["ETag"] = "bad\r\nInjected: header"
    with pytest.raises(ValueError):
        frigate_client.get("/frigate/evt-test/clip.mp4")
    upstream.close.assert_called_once_with()


def test_frigate_log_injection_event_is_rejected(frigate_client, frigate_upstream, caplog):
    _, get = frigate_upstream
    _assert_frigate_fallback(frigate_client.get("/frigate/event%0D%0AFORGED/clip.mp4"))
    get.assert_not_called()
    assert "FORGED" not in caplog.text


def test_wamf_media_routes_do_not_allow_path_traversal(flask_client):
    response = flask_client.get("/wamf/snapshot/../../config/config.yml")
    assert response.status_code == 404


def test_configured_snapshot_route_serves_media(flask_client, tmp_path, monkeypatch):
    import routes.media

    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    (snapshot_root / "event.jpg").write_bytes(b"configured snapshot")
    monkeypatch.setattr(
        routes.media, "get_snapshots_path", lambda: snapshot_root
    )

    response = flask_client.get("/media/snapshots/event.jpg")

    assert response.status_code == 200
    assert response.data == b"configured snapshot"


# ---------------------------------------------------------------------------
# /detections/by_scientific_name — end_date handling
# ---------------------------------------------------------------------------

def test_by_scientific_name_no_end_date_returns_200(flask_client):
    response = flask_client.get("/detections/by_scientific_name/Turdus%20migratorius/2024-06-01")
    assert response.status_code == 200


def test_species_page_queues_metadata_refresh_without_fetching(flask_client, monkeypatch):
    import webui

    queued = []
    monkeypatch.setattr(webui, "get_species_info", lambda _name: None)
    monkeypatch.setattr(webui, "queue_metadata_refresh", lambda name: queued.append(name))
    monkeypatch.setattr(
        webui,
        "refresh_species_metadata_task",
        lambda _name: (_ for _ in ()).throw(AssertionError("metadata fetch should not run during page render"))
    )

    response = flask_client.get("/detections/by_scientific_name/Turdus%20migratorius/2024-06-01")
    assert response.status_code == 200
    assert queued == ["Turdus migratorius"]


def test_by_scientific_name_with_end_date_returns_501(flask_client):
    """end_date path is not implemented — must return 501, not 200/500."""
    response = flask_client.get("/detections/by_scientific_name/Turdus%20migratorius/2024-06-01/2024-06-07")
    assert response.status_code == 501

# ---------------------------------------------------------------------------
# Admin authentication
# ---------------------------------------------------------------------------

def test_public_recent_api_not_protected_when_admin_auth_enabled(flask_client, monkeypatch):
    from werkzeug.security import generate_password_hash
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
    })
    webui.app.secret_key = "test-secret"

    response = flask_client.get("/api/detections/recent")
    assert response.status_code == 200


def test_admin_api_requires_authentication(flask_client, monkeypatch):
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    _set_admin_api_auth(webui, monkeypatch)

    response = flask_client.get("/admin/api/health?check=1")
    assert response.status_code == 401
    data = json.loads(response.data)
    assert data["success"] is False


def test_admin_login_allows_requested_admin_url(flask_client, monkeypatch):
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    _set_admin_api_auth(webui, monkeypatch)
    _stub_admin_api_dependencies(webui, monkeypatch)

    response = flask_client.post(
        "/login?next=/admin/api/health",
        data={"password": "secret"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/api/health")

    response = flask_client.get("/admin/api/health")
    assert response.status_code == 200


@pytest.mark.parametrize("flow", ["auth_disabled", "authenticated", "login_post"])
@pytest.mark.parametrize("destination,expected", [
    ("https://evil.example/admin", "/admin"),
    ("//evil.example/admin", "/admin"),
    ("/\\evil.example", "/admin"),
    ("/\t/evil.example", "/admin"),
    ("/%2Fevil.example", "/admin"),
    ("/%5cevil.example", "/admin"),
    ("/%252Fevil.example", "/admin"),
    ("/%25252Fevil.example", "/admin"),
    ("/%255Cevil.example", "/admin"),
    ("/%25255Cevil.example", "/admin"),
    ("/%2509/evil.example", "/admin"),
    ("/%25250D%25250A/evil.example", "/admin"),
    ("/admin?value=%0D%0A", "/admin?value=%0D%0A"),
    ("/admin?next=%252F%252Fevil.example&value=%255C%250A", "/admin?next=%252F%252Fevil.example&value=%255C%250A"),
    ("/admin?next=%25252F%25252Fevil.example&value=%25255C%25250A", "/admin?next=%25252F%25252Fevil.example&value=%25255C%25250A"),
    ("//[invalid", "/admin"),
    ("/admin?tag=one&tag=two&empty=&q=blue%20tit", "/admin?tag=one&tag=two&empty=&q=blue%20tit"),
    ("/admin?next=https%3A%2F%2Fexample.com", "/admin?next=https%3A%2F%2Fexample.com"),
    ("/media/blue%2520tit.jpg?label=blue%2520tit", "/media/blue%2520tit.jpg?label=blue%2520tit"),
])
def test_login_redirect_destination_validation(
    flask_client, monkeypatch, flow, destination, expected
):
    import webui

    _set_admin_api_auth(webui, monkeypatch)
    if flow == "auth_disabled":
        monkeypatch.setitem(webui.config["admin"], "auth_enabled", False)
    with flask_client.session_transaction() as sess:
        sess.clear()
        if flow == "authenticated":
            sess["admin_authenticated"] = True

    if flow == "login_post":
        response = flask_client.post(
            "/login", data={"password": "secret", "next": destination}
        )
    else:
        response = flask_client.get("/login", query_string={"next": destination})

    assert response.status_code == 302
    assert response.headers["Location"] == expected
    if flow != "auth_disabled":
        with flask_client.session_transaction() as sess:
            assert sess["admin_authenticated"] is True


def test_admin_api_accepts_valid_session(flask_client, monkeypatch):
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    _set_admin_api_auth(webui, monkeypatch, api_token="api-secret")
    _stub_admin_api_dependencies(webui, monkeypatch)
    _login_as_admin(flask_client)

    response = flask_client.get("/admin/api/health")
    assert response.status_code == 200


def test_admin_api_accepts_valid_api_token(flask_client, monkeypatch):
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    _set_admin_api_auth(webui, monkeypatch, api_token="api-secret")
    _stub_admin_api_dependencies(webui, monkeypatch)

    response = flask_client.get(
        "/admin/api/health",
        headers={"X-WAMF-API-Key": "api-secret"},
    )
    assert response.status_code == 200


def test_admin_api_rejects_invalid_api_token(flask_client, monkeypatch):
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    _set_admin_api_auth(webui, monkeypatch, api_token="api-secret")

    response = flask_client.get(
        "/admin/api/health",
        headers={"X-WAMF-API-Key": "wrong-secret"},
    )
    assert response.status_code == 401


def test_admin_api_rejects_missing_api_token(flask_client, monkeypatch):
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    _set_admin_api_auth(webui, monkeypatch, api_token="api-secret")

    response = flask_client.get("/admin/api/health")
    assert response.status_code == 401


def test_regenerated_api_token_invalidates_old_token(flask_client, monkeypatch, tmp_path):
    from werkzeug.security import check_password_hash, generate_password_hash
    import yaml
    import webui

    config_path = tmp_path / "config.yml"
    config_path.write_text("""
frigate:
  mqtt_server: localhost
api:
  token_auth_enabled: true
  token_hash: ""
admin:
  auth_enabled: true
  session_secret: test-secret
  password_hash: old-hash
""".lstrip())
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))
    monkeypatch.setattr(webui, "config", {
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
        "api": {
            "token_auth_enabled": True,
            "token_hash": "",
        },
    })
    webui.app.secret_key = "test-secret"
    _stub_admin_api_dependencies(webui, monkeypatch)

    with flask_client.session_transaction() as sess:
        sess.clear()
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "csrf"

    response = flask_client.post(
        "/admin/api-token",
        data={"csrf_token": "csrf"},
    )
    assert response.status_code == 200
    first_token = _extract_generated_token(response)
    updated = yaml.safe_load(config_path.read_text())
    assert updated["api"]["token_hash"] != first_token
    assert check_password_hash(updated["api"]["token_hash"], first_token)

    with flask_client.session_transaction() as sess:
        sess.clear()

    response = flask_client.get(
        "/admin/api/health",
        headers={"X-WAMF-API-Key": first_token},
    )
    assert response.status_code == 200

    with flask_client.session_transaction() as sess:
        sess.clear()
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "csrf"

    response = flask_client.post(
        "/admin/api-token",
        data={"csrf_token": "csrf"},
    )
    assert response.status_code == 200
    second_token = _extract_generated_token(response)
    assert second_token != first_token

    with flask_client.session_transaction() as sess:
        sess.clear()

    old_response = flask_client.get(
        "/admin/api/health",
        headers={"X-WAMF-API-Key": first_token},
    )
    new_response = flask_client.get(
        "/admin/api/health",
        headers={"X-WAMF-API-Key": second_token},
    )
    assert old_response.status_code == 401
    assert new_response.status_code == 200


def test_admin_login_rejects_external_next_url(flask_client, monkeypatch):
    from werkzeug.security import generate_password_hash
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
    })
    webui.app.secret_key = "test-secret"

    response = flask_client.post(
        "/login?next=https://example.com/admin",
        data={"password": "secret"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin")


def test_delete_detection_auth_enabled_requires_admin_session(flask_client, tmp_dbs, monkeypatch):
    from werkzeug.security import generate_password_hash
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
    })
    webui.app.secret_key = "test-secret"
    _insert_detection(tmp_dbs["det_db"], frigate_event="evt-auth-delete")

    response = flask_client.delete("/detections/evt-auth-delete")
    assert response.status_code == 401


def test_delete_detection_auth_enabled_requires_csrf(flask_client, tmp_dbs, monkeypatch):
    from werkzeug.security import generate_password_hash
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
    })
    webui.app.secret_key = "test-secret"
    _login_as_admin(flask_client)
    _insert_detection(tmp_dbs["det_db"], frigate_event="evt-csrf-delete")

    response = flask_client.delete("/detections/evt-csrf-delete")
    assert response.status_code == 400


def test_delete_detection_auth_enabled_accepts_csrf(flask_client, tmp_dbs, monkeypatch):
    from werkzeug.security import generate_password_hash
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
    })
    webui.app.secret_key = "test-secret"
    csrf_token = _login_as_admin(flask_client)
    _insert_detection(tmp_dbs["det_db"], frigate_event="evt-csrf-ok")

    response = flask_client.delete(
        "/detections/evt-csrf-ok",
        headers={"X-CSRFToken": csrf_token},
    )
    assert response.status_code == 200


def test_config_editor_hides_admin_block(flask_client, monkeypatch, tmp_path):
    import webui

    config_path = tmp_path / "config.yml"
    config_path.write_text("""
frigate:
  mqtt_server: localhost
admin:
  auth_enabled: true
  session_secret: hidden
  password_hash: hidden-hash
api:
  token_auth_enabled: true
  token_hash: hidden-token-hash
webui:
  port: 7766
""".lstrip())
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))
    monkeypatch.setattr(webui, "get_system_health", lambda: {
        "frigate_online": True,
        "mqtt_online": True,
        "database_healthy": True,
        "archive_writable": True,
        "disk_used_percent": 10,
    })
    monkeypatch.setattr(webui, "get_retention_status", lambda: None)

    with flask_client.session_transaction() as sess:
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "csrf"

    monkeypatch.setattr(webui, "config", {
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": "hash",
        }
    })
    webui.app.secret_key = "test-secret"

    response = flask_client.get("/admin/config")
    assert response.status_code == 200
    assert b"mqtt_server" in response.data
    assert b"password_hash" not in response.data
    assert b"hidden-hash" not in response.data
    assert b"token_hash" not in response.data
    assert b"hidden-token-hash" not in response.data


def test_config_editor_restart_schedules_process_restart(flask_client, monkeypatch):
    import routes.admin as admin_routes

    scheduled = []
    monkeypatch.setattr(admin_routes, "schedule_restart", lambda: scheduled.append(True))

    response = flask_client.post("/admin/config/restart", json={})

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert scheduled == [True]


def test_config_editor_restart_requires_csrf_when_auth_enabled(
    flask_client, monkeypatch
):
    from werkzeug.security import generate_password_hash
    import routes.admin as admin_routes
    import webui

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
    })
    webui.app.secret_key = "test-secret"
    _login_as_admin(flask_client)
    scheduled = []
    monkeypatch.setattr(admin_routes, "schedule_restart", lambda: scheduled.append(True))

    response = flask_client.post("/admin/config/restart", json={})

    assert response.status_code == 400
    assert response.is_json
    assert scheduled == []


def test_config_editor_save_and_restart_writes_config(
    flask_client, monkeypatch, tmp_path
):
    import routes.admin as admin_routes

    config_path = tmp_path / "config.yml"
    config_path.write_text("webui:\n  port: 7766\n")
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))
    scheduled = []
    def schedule_after_save():
        assert yaml.safe_load(config_path.read_text())["webui"]["port"] == 8877
        scheduled.append(True)
    monkeypatch.setattr(admin_routes, "schedule_restart", schedule_after_save)

    response = flask_client.post(
        "/admin/config/save-and-restart",
        json={"config_content": "webui:\n  port: 8877\n"},
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert yaml.safe_load(config_path.read_text())["webui"]["port"] == 8877
    assert scheduled == [True]


@pytest.fixture
def config_action_client(flask_client, monkeypatch):
    import routes.admin as admin_routes
    import webui

    _set_admin_api_auth(webui, monkeypatch)
    # Use a separate session so authorization tests cannot inherit a prior login.
    client = webui.app.test_client()
    writer = Mock()
    restart = Mock()
    events = Mock()
    monkeypatch.setattr(webui, "write_config_preserving_admin", writer)
    monkeypatch.setattr(admin_routes, "schedule_restart", restart)
    monkeypatch.setattr(admin_routes, "log_system_event", events)
    return client, writer, restart, events


@pytest.mark.parametrize("path", ["/admin/config/save", "/admin/config/save-and-restart"])
@pytest.mark.parametrize("kind", ["plain", "problem_mark", "context_mark", "invalid_mark", "parser"])
def test_config_yaml_errors_do_not_expose_sensitive_text(config_action_client, path, kind):
    from types import SimpleNamespace
    from yaml.error import Mark, MarkedYAMLError

    client, writer, restart, events = config_action_client
    csrf = _login_as_admin(client)
    sensitive = "password=private-password token=private-token /private/wamf/config.yml"
    if kind == "parser":
        # Exercise a real PyYAML failure whose string includes source contents.
        try:
            yaml.safe_load("token: [private-token\n")
        except yaml.YAMLError as exc:
            error = exc
        assert "private-token" in str(error)
    elif kind == "plain":
        error = yaml.YAMLError(sensitive)
    else:
        mark = Mark("/private/wamf/config.yml", 0, 3, 7, sensitive, 0)
        if kind == "invalid_mark":
            mark = SimpleNamespace(line=sensitive, column=sensitive)
        error = MarkedYAMLError(
            context=sensitive,
            problem=sensitive,
            note=sensitive,
            **{"context_mark" if kind == "context_mark" else "problem_mark": mark},
        )
    writer.side_effect = error

    response = client.post(
        path, json={"config_content": sensitive}, headers={"X-CSRFToken": csrf}
    )

    assert response.status_code == 200
    result = response.get_json()
    assert set(result) == {"success", "error"}
    assert result["success"] is False
    if kind in {"problem_mark", "context_mark"}:
        assert result["error"] == "Invalid YAML configuration at line 4, column 8."
    elif kind == "parser":
        mark = error.problem_mark
        assert result["error"] == (
            f"Invalid YAML configuration at line {mark.line + 1}, column {mark.column + 1}."
        )
    else:
        assert result["error"] == "Invalid YAML configuration. Check the syntax and try again."
    for forbidden in ("private-password", "private-token", "/private/wamf/config.yml", "Traceback"):
        assert forbidden not in response.get_data(as_text=True)
    writer.assert_called_once()
    restart.assert_not_called()
    events.assert_not_called()


@pytest.mark.parametrize("path", ["/admin/config/restart", "/admin/config/save-and-restart"])
def test_config_restart_errors_do_not_expose_sensitive_text(config_action_client, path):
    client, writer, restart, _ = config_action_client
    csrf = _login_as_admin(client)
    restart.side_effect = RuntimeError(
        "Traceback: /private/wamf/config.yml password=private-password token=private-token"
    )

    response = client.post(
        path, json={"config_content": "webui: {}"}, headers={"X-CSRFToken": csrf}
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "success": False,
        "error": "Unable to restart WAMF automatically. Restart WAMF manually.",
    }
    for forbidden in ("private-password", "private-token", "/private/wamf/config.yml", "Traceback"):
        assert forbidden not in response.get_data(as_text=True)
    restart.assert_called_once_with()
    assert writer.call_count == (1 if path.endswith("save-and-restart") else 0)


@pytest.mark.parametrize("path,expected_endpoint", [
    ("/admin/config/save", "admin.save_config"),
    ("/admin/config/restart", "admin.restart_wamf"),
    ("/admin/config/save-and-restart", "admin.save_and_restart_wamf"),
])
@pytest.mark.parametrize("authorization", ["anonymous", "missing_csrf", "invalid_csrf", "valid_csrf"])
def test_config_actions_enforce_registered_auth_and_csrf(
    config_action_client, path, expected_endpoint, authorization
):
    import webui

    client, writer, restart, events = config_action_client
    endpoint, _ = webui.app.url_map.bind("localhost").match(path, method="POST")
    assert endpoint == expected_endpoint
    assert endpoint in webui.CSRF_PROTECTED_ENDPOINTS

    headers = {}
    if authorization != "anonymous":
        csrf = _login_as_admin(client)
        if authorization == "valid_csrf":
            headers["X-CSRFToken"] = csrf
        elif authorization == "invalid_csrf":
            headers["X-CSRFToken"] = "invalid-token"
    response = client.post(path, json={"config_content": "webui: {}"}, headers=headers)

    if authorization == "valid_csrf":
        assert response.status_code == 200
        assert response.get_json()["success"] is True
        assert writer.call_count == (0 if path.endswith("/restart") else 1)
        assert restart.call_count == (0 if path.endswith("/save") else 1)
        events.assert_called_once()
    else:
        if authorization == "anonymous":
            from urllib.parse import parse_qs, urlsplit

            assert response.status_code == 302
            destination = urlsplit(response.headers["Location"])
            assert destination.path == "/login"
            assert parse_qs(destination.query)["next"] == [path]
        else:
            assert response.status_code == 400
            assert response.get_json()["success"] is False
        writer.assert_not_called()
        restart.assert_not_called()
        events.assert_not_called()


def test_change_password_updates_hidden_admin_block(flask_client, monkeypatch, tmp_path):
    from werkzeug.security import check_password_hash, generate_password_hash
    import yaml
    import webui

    config_path = tmp_path / "config.yml"
    config_path.write_text("""
frigate:
  mqtt_server: localhost
admin:
  auth_enabled: true
  session_secret: test-secret
  password_hash: old-hash
""".lstrip())
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))
    monkeypatch.setattr(webui, "config", {
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        }
    })
    webui.app.secret_key = "test-secret"

    with flask_client.session_transaction() as sess:
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "csrf"

    response = flask_client.post(
        "/admin/password",
        data={
            "csrf_token": "csrf",
            "current_password": "secret",
            "new_password": "new-secret",
            "confirm_password": "new-secret",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    updated = yaml.safe_load(config_path.read_text())
    assert updated["admin"]["session_secret"] == "test-secret"
    assert check_password_hash(updated["admin"]["password_hash"], "new-secret")


def test_change_password_page_renders_admin_status_footer(flask_client, monkeypatch):
    from werkzeug.security import generate_password_hash
    import webui

    with flask_client.session_transaction() as sess:
        sess.clear()
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "csrf"

    monkeypatch.setattr(webui, "config", {
        **webui.config,
        "admin": {
            "auth_enabled": True,
            "session_secret": "test-secret",
            "password_hash": generate_password_hash("secret"),
        },
    })
    webui.app.secret_key = "test-secret"
    monkeypatch.setattr(webui, "get_system_health", lambda: {
        "frigate_online": True,
        "mqtt_online": True,
        "database_healthy": True,
        "archive_writable": True,
        "disk_used_percent": 10,
    })
    monkeypatch.setattr(webui, "get_retention_status", lambda: None)

    response = flask_client.get("/admin/password")
    assert response.status_code == 200
    assert b"Admin Password" in response.data
