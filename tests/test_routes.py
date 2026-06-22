"""Unit tests for webui.py Flask routes."""
import json
import re
import sqlite3
from pathlib import Path

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
# /frigate proxy routes — timeout / error handling
# ---------------------------------------------------------------------------

def test_frigate_thumbnail_timeout_returns_fallback(flask_client, monkeypatch):
    """A timeout on the Frigate request returns the 1x1 fallback, not 500."""
    import requests as req
    def fake_get(*a, **kw):
        raise req.exceptions.Timeout("timed out")
    monkeypatch.setattr("app.frigate_proxy.requests.get", fake_get)
    response = flask_client.get("/frigate/evt-test/thumbnail.jpg")
    assert response.status_code == 200
    assert response.content_type == "image/png"


def test_frigate_snapshot_timeout_returns_fallback(flask_client, monkeypatch):
    import requests as req
    def fake_get(*a, **kw):
        raise req.exceptions.Timeout("timed out")
    monkeypatch.setattr("app.frigate_proxy.requests.get", fake_get)
    response = flask_client.get("/frigate/evt-test/snapshot.jpg")
    assert response.status_code == 200
    assert response.content_type == "image/png"


def test_frigate_clip_timeout_returns_fallback(flask_client, monkeypatch):
    import requests as req
    def fake_get(*a, **kw):
        raise req.exceptions.Timeout("timed out")
    monkeypatch.setattr("app.frigate_proxy.requests.get", fake_get)
    response = flask_client.get("/frigate/evt-test/clip.mp4")
    assert response.status_code == 200


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
    monkeypatch.setattr(admin_routes, "schedule_restart", lambda: scheduled.append(True))

    response = flask_client.post(
        "/admin/config/save-and-restart",
        json={"config_content": "webui:\n  port: 8877\n"},
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert yaml.safe_load(config_path.read_text())["webui"]["port"] == 8877
    assert scheduled == [True]


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
