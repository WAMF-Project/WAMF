"""Unit tests for webui.py Flask routes."""
import json
import re
import sqlite3
from datetime import datetime
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


def test_index_renders_overview_dashboard_regions_and_existing_data(flask_client):
    response = flask_client.get("/")

    assert response.status_code == 200
    assert b'class="overview-dashboard"' in response.data
    assert b'class="overview-main"' in response.data
    assert b'class="overview-context-rail"' in response.data
    assert b"Recent detections" in response.data
    assert b"Detection summary" in response.data
    assert b"Latest visitor" in response.data
    assert b"Most active species" in response.data
    assert b"American Robin" in response.data
    assert b"Turdus migratorius" in response.data
    assert b"birdcam" in response.data
    assert b"92% confidence" in response.data


def test_index_preserves_overview_links_modals_and_live_polling(flask_client):
    response = flask_client.get("/")

    assert response.status_code == 200
    assert b'href="/recent"' in response.data
    assert b'href="/activity"' in response.data
    assert b'href="/species/Turdus%20migratorius"' in response.data
    assert b'id="date-picker"' in response.data
    assert b'id="snapshotModal"' in response.data
    assert b'id="videoModal"' in response.data
    assert b"/api/detections/recent?limit=${LIMIT}" in response.data
    assert b"setInterval(poll, POLL_MS)" in response.data


def test_overview_species_names_link_to_profiles(flask_client):
    response = flask_client.get("/")

    assert response.status_code == 200
    assert b'href="/species/Turdus%20migratorius"' in response.data
    assert b"View American Robin species profile" in response.data
    assert b"data-tooltip=" in response.data


def test_public_shell_uses_canonical_stylesheet_and_mobile_navigation(flask_client):
    response = flask_client.get("/")

    assert response.status_code == 200
    assert b"css/styles.css" in response.data
    assert b"styles-dev.css" not in response.data
    assert b'aria-controls="app-sidebar"' in response.data
    assert b'data-nav-open' in response.data


def test_public_shell_shows_only_admin_login_entry_when_logged_out(flask_client):
    with flask_client.session_transaction() as sess:
        sess.clear()

    response = flask_client.get("/")

    assert response.status_code == 200
    assert re.search(rb'href="/login"[^>]*>\s*Admin\s*</a>', response.data)
    assert b"Administration" not in response.data
    assert b"Admin dashboard" not in response.data
    assert b'href="/admin"' not in response.data
    assert b'href="/admin/species"' not in response.data
    assert b'href="/admin/logs"' not in response.data
    assert b'href="/admin/config"' not in response.data
    assert b'href="/admin/password"' not in response.data
    assert b'href="/admin/api-token"' not in response.data


def test_public_shell_shows_admin_navigation_when_authenticated(flask_client):
    with flask_client.session_transaction() as sess:
        sess.clear()
        sess["admin_authenticated"] = True

    response = flask_client.get("/")

    assert response.status_code == 200
    assert b"Administration" in response.data
    assert b"Admin dashboard" in response.data
    assert b'href="/admin"' in response.data
    assert b'href="/login"' not in response.data
    assert b'href="/logout"' in response.data

    with flask_client.session_transaction() as sess:
        sess.clear()


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
# Activity
# ---------------------------------------------------------------------------

def test_activity_default_redirects_to_today(flask_client):
    response = flask_client.get("/activity")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        f"/activity/{datetime.now().strftime('%Y-%m-%d')}"
    )


def test_activity_historical_date_uses_selected_date_data(flask_client):
    response = flask_client.get("/activity/2024-06-01")

    assert response.status_code == 200
    assert b"Saturday, 01 June 2024" in response.data
    assert b'value="2024-06-01"' in response.data
    assert b"Total detections" in response.data
    assert re.search(rb"Total detections.*?<strong>3</strong>", response.data, re.DOTALL)
    assert re.search(rb"Species seen.*?<strong>2</strong>", response.data, re.DOTALL)
    assert re.search(rb"Peak activity.*?<strong>09:00</strong>", response.data, re.DOTALL)
    assert b'href="/daily_summary/2024-06-01"' in response.data
    assert b'href="/detections/by_hour/2024-06-01/9"' in response.data
    assert b"/detections/by_scientific_name/Turdus%20migratorius/2024-06-01" in response.data
    assert b'href="/species/Turdus%20migratorius"' in response.data


def test_activity_queries_use_selected_date_not_today(flask_client, monkeypatch):
    import webui

    selected_dates = []
    monkeypatch.setattr(
        webui,
        "get_activity_by_hour",
        lambda date: selected_dates.append(("hours", date)) or [],
    )
    monkeypatch.setattr(
        webui,
        "get_top_species",
        lambda date: selected_dates.append(("top", date)) or [],
    )
    monkeypatch.setattr(
        webui,
        "get_species_peak_hours",
        lambda date: selected_dates.append(("peaks", date)) or [],
    )
    monkeypatch.setattr(
        webui,
        "get_daily_summary",
        lambda date: selected_dates.append(("summary", date.strftime("%Y-%m-%d"))) or {},
    )
    monkeypatch.setattr(
        webui,
        "get_adjacent_activity_dates",
        lambda date: selected_dates.append(("adjacent", date)) or {
            "previous_date": None,
            "next_date": None,
        },
    )

    response = flask_client.get("/activity/2024-06-01")

    assert response.status_code == 200
    assert selected_dates == [
        ("hours", "2024-06-01"),
        ("top", "2024-06-01"),
        ("peaks", "2024-06-01"),
        ("summary", "2024-06-01"),
        ("adjacent", "2024-06-01"),
    ]


def test_activity_empty_historical_date_is_safe(flask_client):
    response = flask_client.get("/activity/2000-01-01")

    assert response.status_code == 200
    assert b"No activity was recorded" in response.data
    assert re.search(rb"Total detections.*?<strong>0</strong>", response.data, re.DOTALL)
    assert re.search(rb"Species seen.*?<strong>0</strong>", response.data, re.DOTALL)


def test_activity_malformed_date_returns_404(flask_client):
    assert flask_client.get("/activity/not-a-date").status_code == 404
    assert flask_client.get("/activity/2024-6-1").status_code == 404
    assert flask_client.get("/activity/2024-02-30").status_code == 404


def test_activity_future_date_redirects_to_today(flask_client):
    response = flask_client.get("/activity/2999-01-01")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        f"/activity/{datetime.now().strftime('%Y-%m-%d')}"
    )


def test_activity_navigation_uses_dates_with_detections(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(
        webui,
        "get_adjacent_activity_dates",
        lambda _date: {
            "previous_date": "2024-05-28",
            "next_date": "2024-06-03",
        },
    )

    response = flask_client.get("/activity/2024-06-01")

    assert response.status_code == 200
    assert b'href="/activity/2024-05-28"' in response.data
    assert b'href="/activity/2024-06-03"' in response.data
    assert b"Previous activity" in response.data
    assert b"Next activity" in response.data


def test_activity_renders_compact_24_hour_chart(flask_client):
    response = flask_client.get("/activity/2024-06-01")

    assert response.status_code == 200
    assert b'class="activity-chart-scroll"' in response.data
    assert response.data.count(b'class="activity-hour-column"') == 24
    assert b"--activity-level:" in response.data
    assert b"navigateToActivityDate" in response.data


# ---------------------------------------------------------------------------
# Recent Feed
# ---------------------------------------------------------------------------

def _recent_feed_record(index):
    return {
        "display_name": f"Species {index:02d}",
        "common_name": f"Bird {index:02d}",
        "score": 0.9,
        "detection_time": f"2024-06-{index:02d} 10:00:00.000000",
        "camera_name": "birdcam",
        "frigate_event": f"evt-feed-{index:02d}",
        "snapshot_file": None,
        "clip_file": None,
    }


def test_recent_feed_first_page_uses_server_pagination(flask_client, monkeypatch):
    import webui

    calls = []
    records = [_recent_feed_record(index) for index in range(26, 0, -1)]
    monkeypatch.setattr(webui, "get_detection_count", lambda: len(records))
    monkeypatch.setattr(
        webui,
        "recent_detections",
        lambda limit, offset=0: calls.append((limit, offset)) or records[offset:offset + limit],
    )

    response = flask_client.get("/recent")

    assert response.status_code == 200
    assert calls == [(25, 0)]
    assert b"Bird 26" in response.data
    assert b"Bird 01" not in response.data
    assert b"Page 1 of 2" in response.data
    assert b'href="/recent?page=2"' in response.data
    assert b'rel="prev"' not in response.data


def test_recent_feed_subsequent_page_and_controls(flask_client, monkeypatch):
    import webui

    calls = []
    records = [_recent_feed_record(index) for index in range(26, 0, -1)]
    monkeypatch.setattr(webui, "get_detection_count", lambda: len(records))
    monkeypatch.setattr(
        webui,
        "recent_detections",
        lambda limit, offset=0: calls.append((limit, offset)) or records[offset:offset + limit],
    )

    response = flask_client.get("/recent?page=2")

    assert response.status_code == 200
    assert calls == [(25, 25)]
    assert b"Bird 01" in response.data
    assert b"Bird 26" not in response.data
    assert b"Page 2 of 2" in response.data
    assert b'href="/recent?page=1"' in response.data
    assert b'rel="next"' not in response.data


def test_recent_feed_empty_state(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(webui, "get_detection_count", lambda: 0)
    monkeypatch.setattr(webui, "recent_detections", lambda _limit, _offset=0: [])

    response = flask_client.get("/recent")

    assert response.status_code == 200
    assert b"No detections have been recorded yet." in response.data
    assert b"Page 1 of 1" in response.data


def test_recent_feed_redirects_invalid_pages_safely(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(webui, "get_detection_count", lambda: 26)

    invalid = flask_client.get("/recent?page=invalid")
    zero = flask_client.get("/recent?page=0")
    out_of_range = flask_client.get("/recent?page=999&future=kept")

    assert invalid.status_code == 302
    assert invalid.headers["Location"].endswith("/recent?page=1")
    assert zero.status_code == 302
    assert zero.headers["Location"].endswith("/recent?page=1")
    assert out_of_range.status_code == 302
    assert "page=2" in out_of_range.headers["Location"]
    assert "future=kept" in out_of_range.headers["Location"]


def test_recent_feed_preserves_detection_card_actions(flask_client, monkeypatch):
    import webui

    record = _recent_feed_record(1)
    record.update({
        "display_name": "Turdus migratorius",
        "common_name": "American Robin",
        "snapshot_file": "event.jpg",
        "clip_file": "event.mp4",
    })
    monkeypatch.setattr(webui, "get_detection_count", lambda: 1)
    monkeypatch.setattr(webui, "recent_detections", lambda _limit, _offset=0: [record])

    response = flask_client.get("/recent")

    assert response.status_code == 200
    assert b'class="overview-detection-grid recent-feed-grid"' in response.data
    assert b"American Robin" in response.data
    assert b"Turdus migratorius" in response.data
    assert b"birdcam" in response.data
    assert b"event.jpg" in response.data
    assert b"event.mp4" in response.data
    assert b"Open snapshot for American Robin" in response.data
    assert b"showSnapshot(" in response.data
    assert b'href="/species/Turdus%20migratorius"' in response.data
    assert b"View American Robin species profile" in response.data
    assert b'id="snapshotModal"' in response.data
    assert b'id="videoModal"' in response.data
    assert b"deleteDetection()" in response.data


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


def test_daily_summary_selected_date_metrics_and_species_cards(flask_client):
    response = flask_client.get("/daily_summary/2024-06-01")

    assert response.status_code == 200
    assert b"Saturday, 01 June 2024" in response.data
    assert b'value="2024-06-01"' in response.data
    assert b"3 detections" in response.data
    assert b"2 species" in response.data
    assert b"Peak activity 09:00" in response.data
    assert b"American Robin" in response.data
    assert b"Turdus migratorius" in response.data
    assert b"Blue Jay" in response.data
    assert b"Cyanocitta cristata" in response.data
    assert b'href="/species/Turdus%20migratorius"' in response.data
    assert b"View American Robin species profile" in response.data


def test_daily_summary_renders_24_hours_per_species(flask_client):
    response = flask_client.get("/daily_summary/2024-06-01")

    assert response.status_code == 200
    assert response.data.count(b'class="daily-hour-cell') == 48
    assert response.data.count(b"daily-hour-active") == 3
    assert response.data.count(b"daily-hour-inactive") == 45


def test_daily_summary_active_hours_link_and_inactive_hours_do_not(flask_client):
    response = flask_client.get("/daily_summary/2024-06-01")

    assert response.status_code == 200
    assert b'href="/detections/by_hour/2024-06-01/8"' in response.data
    assert b'href="/detections/by_hour/2024-06-01/9"' in response.data
    assert b"1 detection of American Robin at 08:00" in response.data
    assert b'href="/detections/by_hour/2024-06-01/0"' not in response.data


def test_daily_summary_navigation_uses_activity_dates(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(
        webui,
        "get_adjacent_activity_dates",
        lambda _date: {
            "previous_date": "2024-05-28",
            "next_date": "2024-06-03",
        },
    )

    response = flask_client.get("/daily_summary/2024-06-01")

    assert response.status_code == 200
    assert b'href="/daily_summary/2024-05-28"' in response.data
    assert b'href="/daily_summary/2024-06-03"' in response.data
    assert b'href="/activity/2024-06-01"' in response.data
    assert b"Previous activity" in response.data
    assert b"Next activity" in response.data
    assert b"Activity overview" in response.data


def test_daily_summary_empty_date_has_deliberate_empty_state(flask_client):
    response = flask_client.get("/daily_summary/2000-01-01")

    assert response.status_code == 200
    assert b"0 detections" in response.data
    assert b"0 species" in response.data
    assert b"No peak activity" in response.data
    assert b"No wildlife activity was recorded" in response.data
    assert b'class="daily-species-grid"' not in response.data


def test_daily_summary_invalid_and_future_dates_are_safe(flask_client):
    assert flask_client.get("/daily_summary/not-a-date").status_code == 404
    assert flask_client.get("/daily_summary/2024-6-1").status_code == 404

    future = flask_client.get("/daily_summary/2999-01-01")
    assert future.status_code == 302
    assert future.headers["Location"].endswith(
        f"/daily_summary/{datetime.now().strftime('%Y-%m-%d')}"
    )


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
# /species/<scientific_name>
# ---------------------------------------------------------------------------

def test_species_profile_renders_identity_enrichment_and_statistics(flask_client):
    response = flask_client.get("/species/Turdus%20migratorius")

    assert response.status_code == 200
    assert b"American Robin" in response.data
    assert b"Turdus migratorius" in response.data
    assert b"A familiar thrush." in response.data
    assert b"https://example.com/robin.jpg" in response.data
    assert b"https://example.com/robin" in response.data
    assert re.search(rb"Total detections.*?<strong>2</strong>", response.data, re.DOTALL)
    assert b"01 Jun 2024" in response.data
    assert b"08:30" in response.data
    assert b"09:45" in response.data
    assert re.search(rb"Most active in WAMF.*?<strong>08:00</strong>", response.data, re.DOTALL)
    assert re.search(rb"Active days.*?<strong>1</strong>", response.data, re.DOTALL)
    assert re.search(rb"Cameras.*?<strong>1</strong>", response.data, re.DOTALL)
    assert response.data.count(b'class="daily-hour-cell') == 24


def test_species_profile_missing_enrichment_still_renders(flask_client, monkeypatch):
    import webui

    queued = []
    monkeypatch.setattr(webui, "get_species_info", lambda _name: None)
    monkeypatch.setattr(webui, "queue_metadata_refresh", lambda name: queued.append(name))

    response = flask_client.get("/species/Turdus%20migratorius")

    assert response.status_code == 200
    assert b"American Robin" in response.data
    assert b"Total detections" in response.data
    assert b"species-profile-reference" not in response.data
    assert queued == ["Turdus migratorius"]


def test_species_profile_unknown_or_malformed_species_returns_404(flask_client):
    assert flask_client.get("/species/Unknown%20species").status_code == 404
    assert flask_client.get("/species/%20Turdus%20migratorius").status_code == 404


def test_species_profile_view_detections_uses_all_history_destination(flask_client):
    profile = flask_client.get("/species/Turdus%20migratorius")

    assert profile.status_code == 200
    assert b'href="/species/Turdus%20migratorius/detections"' in profile.data
    assert b'aria-label="View detections of American Robin"' in profile.data

    history = flask_client.get("/species/Turdus%20migratorius/detections")
    assert history.status_code == 200
    assert b"Complete detection history" in history.data
    assert b'href="/species/Turdus%20migratorius"' in history.data


def test_species_navigation_contract_across_redesigned_pages(flask_client):
    responses = [
        flask_client.get("/"),
        flask_client.get("/recent"),
        flask_client.get("/activity/2024-06-01"),
        flask_client.get("/daily_summary/2024-06-01"),
    ]

    for response in responses:
        assert response.status_code == 200
        assert b'href="/species/Turdus%20migratorius"' in response.data

    daily_summary = responses[-1]
    assert b'href="/detections/by_hour/2024-06-01/8"' in daily_summary.data
    assert b"data-tooltip=\"View 1 American Robin detection at 08:00\"" in daily_summary.data


# ---------------------------------------------------------------------------
# /detections/by_scientific_name — end_date handling
# ---------------------------------------------------------------------------

def test_by_scientific_name_no_end_date_returns_200(flask_client):
    response = flask_client.get("/detections/by_scientific_name/Turdus%20migratorius/2024-06-01")
    assert response.status_code == 200


def test_species_results_do_not_load_species_enrichment(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(
        webui,
        "get_species_info",
        lambda _name: (_ for _ in ()).throw(
            AssertionError("results page should not load species enrichment")
        ),
    )
    monkeypatch.setattr(
        webui,
        "queue_metadata_refresh",
        lambda _name: (_ for _ in ()).throw(
            AssertionError("results page should not queue species enrichment")
        ),
    )

    response = flask_client.get(
        "/detections/by_scientific_name/Turdus%20migratorius/2024-06-01"
    )

    assert response.status_code == 200
    assert b"A familiar thrush." not in response.data
    assert b"https://example.com/robin" not in response.data


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


# ---------------------------------------------------------------------------
# Kingfisher detection-result presentation
# ---------------------------------------------------------------------------

def test_species_detection_results_are_focused_and_link_to_profile(flask_client):
    response = flask_client.get(
        "/detections/by_scientific_name/Turdus%20migratorius/2024-06-01"
    )

    assert response.status_code == 200
    assert b"Detection results" in response.data
    assert b'href="/species/Turdus%20migratorius"' in response.data
    assert b"View American Robin species profile" in response.data
    assert b"A familiar thrush." not in response.data
    assert b"https://example.com/robin.jpg" not in response.data
    assert response.data.count(b'class="daily-hour-cell') == 24
    assert b'href="/detections/by_hour/2024-06-01/8"' in response.data
    assert b'href="/detections/by_hour/2024-06-01/9"' in response.data
    assert b"detection-results-card" in response.data
    assert b"92% confidence" in response.data


def test_hour_detection_results_use_shared_cards_and_profile_links(flask_client):
    response = flask_client.get("/detections/by_hour/2024-06-01/8")

    assert response.status_code == 200
    assert b"detection-results-card" in response.data
    assert b'href="/species/Turdus%20migratorius"' in response.data
    assert b"View American Robin species profile" in response.data
    assert b"92% confidence" in response.data
    assert b'href="/activity/2024-06-01"' in response.data


def test_detection_result_empty_states_remain_sensible(flask_client):
    species_response = flask_client.get(
        "/detections/by_scientific_name/Turdus%20migratorius/1999-01-01"
    )
    hour_response = flask_client.get("/detections/by_hour/2024-06-01/23")

    assert species_response.status_code == 200
    assert b"No American Robin detections were recorded on 1999-01-01." in species_response.data
    assert b"Page 1 of 1" in species_response.data
    assert hour_response.status_code == 200
    assert b"No detections were recorded during this hour." in hour_response.data


def test_species_detection_results_preserve_pagination(flask_client, monkeypatch):
    import webui

    monkeypatch.setattr(
        webui,
        "get_detection_count_for_scientific_name_and_date",
        lambda _name, _date: 26,
    )

    response = flask_client.get(
        "/detections/by_scientific_name/Turdus%20migratorius/2024-06-01"
    )

    assert response.status_code == 200
    assert b"Page 1 of 2" in response.data
    assert (
        b'href="/detections/by_scientific_name/'
        b'Turdus%20migratorius/2024-06-01?page=2"'
    ) in response.data
    assert b'rel="prev"' not in response.data

    second_page = flask_client.get(
        "/detections/by_scientific_name/Turdus%20migratorius/2024-06-01?page=2"
    )

    assert second_page.status_code == 200
    assert b"Page 2 of 2" in second_page.data
    assert b'href="/detections/by_scientific_name/Turdus%20migratorius/2024-06-01?page=1"' in second_page.data
    assert b'rel="prev"' in second_page.data
    assert b'rel="next"' not in second_page.data


def test_recent_feed_still_uses_shared_detection_card_behaviour(flask_client):
    response = flask_client.get("/recent")

    assert response.status_code == 200
    assert b"recent-feed-card" in response.data
    assert b"Open snapshot for American Robin" in response.data or b"No snapshot available" in response.data
    assert b'href="/species/Turdus%20migratorius"' in response.data


# ---------------------------------------------------------------------------
# Canonical all-history species detections
# ---------------------------------------------------------------------------

def test_species_detection_history_includes_all_dates_newest_first(
    flask_client,
    tmp_dbs,
):
    event = "evt-species-history-newest"
    _insert_detection(tmp_dbs["det_db"], event)

    try:
        history = flask_client.get("/species/Turdus%20migratorius/detections")
        dated = flask_client.get(
            "/detections/by_scientific_name/Turdus%20migratorius/2024-06-01"
        )

        assert history.status_code == 200
        assert history.data.count(b"detection-results-card") >= 3
        assert history.data.index(event.encode()) < history.data.index(b"evt-003")
        assert b"Newest first" in history.data
        assert b"Complete detection history" in history.data
        assert b'href="/species/Turdus%20migratorius"' in history.data
        assert dated.status_code == 200
        assert event.encode() not in dated.data
        assert dated.data.count(b'class="daily-hour-cell') == 24
    finally:
        _delete_detection(tmp_dbs["det_db"], event)


def test_species_detection_history_pagination_uses_canonical_route(
    flask_client,
    monkeypatch,
):
    import webui

    monkeypatch.setattr(
        webui,
        "get_species_stats",
        lambda _name: {
            "total_detections": 26,
            "first_seen": "2024-06-01 08:30:00.000000",
            "last_seen": "2024-06-02 10:00:00.000000",
            "active_days": 2,
            "camera_count": 1,
        },
    )

    first_page = flask_client.get("/species/Turdus%20migratorius/detections")
    second_page = flask_client.get(
        "/species/Turdus%20migratorius/detections?page=2"
    )

    assert first_page.status_code == 200
    assert b"Page 1 of 2" in first_page.data
    assert b'href="/species/Turdus%20migratorius/detections?page=2"' in first_page.data
    assert b'rel="prev"' not in first_page.data
    assert second_page.status_code == 200
    assert b"Page 2 of 2" in second_page.data
    assert b'href="/species/Turdus%20migratorius/detections?page=1"' in second_page.data
    assert b'rel="prev"' in second_page.data
    assert b'rel="next"' not in second_page.data


def test_species_detection_history_canonicalizes_invalid_pages(flask_client):
    zero = flask_client.get("/species/Turdus%20migratorius/detections?page=0")
    invalid = flask_client.get(
        "/species/Turdus%20migratorius/detections?page=invalid"
    )
    too_high = flask_client.get(
        "/species/Turdus%20migratorius/detections?page=999"
    )

    assert zero.status_code == 302
    assert zero.headers["Location"].endswith(
        "/species/Turdus%20migratorius/detections?page=1"
    )
    assert invalid.status_code == 302
    assert invalid.headers["Location"].endswith(
        "/species/Turdus%20migratorius/detections?page=1"
    )
    assert too_high.status_code == 302
    assert too_high.headers["Location"].endswith(
        "/species/Turdus%20migratorius/detections?page=1"
    )


def test_species_detection_history_has_empty_state_and_unknown_is_404(
    flask_client,
    monkeypatch,
):
    import webui

    monkeypatch.setattr(
        webui,
        "get_records_for_scientific_name",
        lambda _name, _page, _per_page: [],
    )

    empty = flask_client.get("/species/Turdus%20migratorius/detections")
    unknown = flask_client.get("/species/Unknown%20species/detections")

    assert empty.status_code == 200
    assert b"No detections have been recorded for American Robin." in empty.data
    assert unknown.status_code == 404
