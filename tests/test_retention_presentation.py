"""Native retention status and timezone regression coverage for System Health."""

import re

import pytest

from app.retention_presentation import build_retention_presentation
from app.retention_state import MAX_DIAGNOSTICS, MAX_DIAGNOSTIC_CHARS


def schedule_config(enabled=True):
    return {"retention": {"schedule": {
        "enabled": enabled, "time": "03:00", "timezone": "Europe/London",
    }}}


def native_state(**overrides):
    state = {
        "last_attempt_started_at": "2026-10-04T02:00:00+00:00",
        "last_attempt_completed_at": "2026-10-04T02:00:02+00:00",
        "last_attempt_duration_ms": 2000,
        "last_attempt_trigger": "scheduled",
        "last_attempt_outcome": "success",
        "last_success_completed_at": "2026-10-04T02:00:02+00:00",
        "expired_media_action": "report_only",
        "orphan_media_action": "disabled",
        "expired_media_outcome": "success",
        "orphan_scan_outcome": "skipped",
        "system_events_outcome": "success",
        "rows_scanned": 393,
        "expired_snapshot_count": 0,
        "expired_clip_count": 0,
        "deleted_snapshot_count": 0,
        "deleted_clip_count": 0,
        "orphan_count": 0,
        "orphan_deletion_count": 0,
        "missing_reference_count": 0,
        "system_events_pruned": 12,
        "error_count": 0,
        "error_summaries": [],
        "legacy_orphan_scan_at": None,
        # These compatibility fields must never drive the new card.
        "last_run": "WRONG COMPATIBILITY TIME",
        "missing_count": 999,
    }
    state.update(overrides)
    return state


@pytest.mark.parametrize("outcome,label,errors", [
    ("success", "Success", 0),
    ("partial", "Partial", 2),
    ("failed", "Failed", 3),
])
def test_native_outcomes_and_older_success(outcome, label, errors):
    view = build_retention_presentation(native_state(
        last_attempt_outcome=outcome, error_count=errors,
        last_success_completed_at="2026-10-03T02:00:00Z",
    ), schedule_config())
    primary = dict(view["primary"])
    assert view["status"] == label
    assert primary["Error count"] == str(errors)
    assert primary["Latest attempt completed"] == "04 Oct 2026 · 03:00 BST"
    assert primary["Last successful run"] == "03 Oct 2026 · 03:00 BST"


@pytest.mark.parametrize("value,expected", [
    ("2026-10-04T02:00:00Z", "04 Oct 2026 · 03:00 BST"),
    ("2026-12-04T03:00:00+00:00", "04 Dec 2026 · 03:00 GMT"),
])
def test_london_dst_conversion_preserves_source(value, expected):
    state = native_state(last_attempt_completed_at=value, last_success_completed_at=value)
    view = build_retention_presentation(state, schedule_config())
    assert dict(view["primary"])["Latest attempt completed"] == expected
    assert dict(view["primary"])["Last successful run"] == expected
    assert dict(view["details"])["Latest attempt started"] == "04 Oct 2026 · 03:00 BST"
    assert state["last_attempt_completed_at"] == value
    assert view["timezone_note"] == "Times shown in Europe/London"


def test_persisted_native_query_uses_utc_fields_instead_of_formatted_alias(tmp_path, monkeypatch):
    from app import queries
    from app.db import ensure_schema
    from app.retention_state import RetentionStateRepository
    from tests.test_retention_state import _result

    database = tmp_path / "retention.db"
    ensure_schema(database)
    result = _result(completed="2026-10-04T02:00:02+00:00")
    RetentionStateRepository(database).persist(result)
    monkeypatch.setattr(queries, "DBPATH", database)
    state = queries.get_retention_status()
    assert state["last_run"] == "04 Oct 2026 02:00"
    view = build_retention_presentation(state, schedule_config())
    assert dict(view["primary"])["Latest attempt completed"] == "04 Oct 2026 · 03:00 BST"
    assert view["status"] == "Success"
    assert queries.get_retention_status()["last_attempt_completed_at"] == result.completed_at.isoformat()


@pytest.mark.parametrize("enabled,expected", [
    (True, "Daily at 03:00 · Europe/London"), (False, "Disabled"),
])
def test_configured_schedule(enabled, expected):
    view = build_retention_presentation(native_state(), schedule_config(enabled))
    assert view["schedule"] == expected
    assert "next" not in str(view).lower()


@pytest.mark.parametrize("config", [
    None, [], {"retention": None},
    {"retention": {"schedule": {"timezone": "invalid/timezone"}}},
    {"retention": {"schedule": {"time": "invalid"}}},
])
def test_invalid_or_unavailable_schedule_uses_explicit_utc(config):
    view = build_retention_presentation(native_state(), config)
    assert view["status"] == "Success"
    assert view["schedule"] == "Unavailable"
    assert view["timezone_note"] == "Times shown in UTC (fallback)"
    assert dict(view["primary"])["Latest attempt completed"] == "04 Oct 2026 · 02:00 UTC"


def test_missing_schedule_uses_canonical_defaults():
    view = build_retention_presentation(native_state(), {})
    assert view["schedule"] == "Disabled"
    assert view["timezone_note"] == "Times shown in UTC"


@pytest.mark.parametrize("state", [None, {
    "last_attempt_completed_at": None, "last_attempt_outcome": None,
}])
def test_no_native_run(state):
    view = build_retention_presentation(state, schedule_config())
    assert view["status"] == "Never run — No native retention run recorded"
    assert view["legacy_scan"] is None


def test_legacy_only_is_not_native_success_and_unknown_counts_stay_unknown():
    view = build_retention_presentation({
        "last_attempt_completed_at": None,
        "legacy_orphan_scan_at": "2025-01-01 03:00:00",
        "rows_scanned": 42, "orphan_count": 4,
        "deleted_snapshot_count": None, "missing_reference_count": None,
    }, schedule_config())
    assert "Never run" in view["status"]
    assert view["legacy_scan"] == "01 Jan 2025 · 03:00 (timezone unknown)"
    assert dict(view["primary"])["Snapshots deleted"] == "Unknown"
    assert dict(view["primary"])["Missing references found"] == "Unknown"
    assert dict(view["details"])["Rows scanned"] == "42"


@pytest.mark.parametrize("state", [
    [], "invalid", {},
    native_state(last_attempt_outcome="running"),
    native_state(last_attempt_outcome=[]),
    native_state(last_attempt_completed_at="bad"),
    native_state(last_attempt_completed_at="2026-10-04 02:00:00"),
    native_state(last_attempt_completed_at=None),
])
def test_malformed_status_is_unavailable_not_never_run(state):
    view = build_retention_presentation(state, schedule_config())
    assert view["status"] == "Status unavailable"
    assert view["schedule"] == "Daily at 03:00 · Europe/London"


@pytest.mark.parametrize("value", [None, "0", -1, True, []])
def test_unknown_counters_and_duration_are_not_coerced_to_zero(value):
    view = build_retention_presentation(native_state(
        deleted_snapshot_count=value, last_attempt_duration_ms=value,
        error_count=value, rows_scanned=value,
    ), schedule_config())
    assert dict(view["primary"])["Snapshots deleted"] == "Unknown"
    assert dict(view["primary"])["Duration"] == "Unknown"
    assert dict(view["primary"])["Error count"] == "Unknown"
    assert dict(view["details"])["Rows scanned"] == "Unknown"


def test_disabled_and_report_only_actions_do_not_override_success():
    view = build_retention_presentation(native_state(), schedule_config())
    assert view["status"] == "Success"
    assert dict(view["primary"])["Snapshots deleted"] == "0"
    assert dict(view["primary"])["System events pruned"] == "12"
    assert dict(view["primary"])["Missing references found"] == "0"
    assert dict(view["details"])["Expired-media action"] == "Report only"
    assert dict(view["details"])["Orphan-media action"] == "Disabled"
    assert dict(view["details"])["Orphan-scan phase"] == "Skipped"


def test_custom_operator_trigger_is_preserved_with_redaction():
    view = build_retention_presentation(native_state(
        last_attempt_trigger="operator-check token=private-trigger-secret",
    ), schedule_config())
    assert dict(view["primary"])["Trigger"] == "operator-check credential=[redacted]"
    assert "private-trigger-secret" not in str(view)


def test_diagnostics_reuse_bounding_and_redaction_and_ignore_raw_json():
    view = build_retention_presentation(native_state(
        error_summaries=["token=hidden /private/media/file https://private.example/x " + "x" * 500] * 20,
        error_summaries_json="raw-secret-must-not-render",
    ), schedule_config())
    assert 0 < len(view["diagnostics"]) <= MAX_DIAGNOSTICS
    assert all(len(item) <= MAX_DIAGNOSTIC_CHARS for item in view["diagnostics"])
    assert "hidden" not in str(view)
    assert "/private" not in str(view)
    assert "private.example" not in str(view)
    assert "raw-secret" not in str(view)


@pytest.fixture
def admin_page(flask_client, monkeypatch):
    import webui
    import routes.admin as admin_routes

    health = {
        "frigate_online": True, "mqtt_online": True,
        "database_healthy": True, "archive_writable": True, "disk_used_percent": 10,
    }
    monkeypatch.setattr(webui, "get_system_health", lambda: health)
    monkeypatch.setattr(webui, "get_admin_stats", lambda: {
        "total_detections": 3, "archived_snapshots": 2,
        "archived_clips": 1, "archive_size_mb": 7,
    })
    monkeypatch.setattr(webui, "get_recent_system_events", lambda: [])
    monkeypatch.setattr(webui, "get_retention_status", lambda: native_state())
    monkeypatch.setattr(admin_routes, "load_runtime_config", schedule_config)
    with flask_client.session_transaction() as session:
        session["admin_authenticated"] = True
    return flask_client, webui, admin_routes


def test_admin_renders_native_card_and_escaped_diagnostics(admin_page, monkeypatch):
    client, webui, _ = admin_page
    monkeypatch.setattr(webui, "get_retention_status", lambda: native_state(
        error_summaries=["<script>alert('diagnostic')</script>"],
    ))
    response = client.get("/admin")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<h2 id="retention-heading">Retention</h2>' in html
    assert "Success" in html
    assert "04 Oct 2026 · 03:00 BST" in html
    assert "Configured automatic cleanup" in html
    assert "Daily at 03:00 · Europe/London" in html
    assert "Attempt details and diagnostics" in html
    assert "<script>alert('diagnostic')" not in html
    assert "&lt;script&gt;" in html
    assert "WRONG COMPATIBILITY TIME" not in html
    assert "Archive Retention" not in html
    assert "Run Now" not in html
    assert "Next run" not in html


@pytest.mark.parametrize("failure", ["query", "config", "presentation", "malformed"])
def test_retention_failure_leaves_system_health_and_archive_usable(admin_page, monkeypatch, caplog, failure):
    client, webui, admin_routes = admin_page

    def fail(*args, **kwargs):
        raise RuntimeError("token=private-exception-secret")

    if failure == "query":
        monkeypatch.setattr(webui, "get_retention_status", fail)
    elif failure == "config":
        monkeypatch.setattr(admin_routes, "load_runtime_config", fail)
    elif failure == "presentation":
        monkeypatch.setattr(admin_routes, "build_retention_presentation", fail)
    else:
        monkeypatch.setattr(webui, "get_retention_status", lambda: {"last_attempt_completed_at": []})
    response = client.get("/admin")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert ">Online</span>" in html
    assert "Total Detections" in html
    if failure == "config":
        assert "Success" in html
        assert "Times shown in UTC (fallback)" in html
    else:
        assert "Status unavailable" in html
        assert "Never run" not in html
    assert "private-exception-secret" not in html + caplog.text


@pytest.mark.parametrize("state,expected", [
    (None, "Never run"),
    ({"legacy_orphan_scan_at": "2025-01-01T03:00:00Z"}, "Legacy orphan scan"),
    (native_state(last_attempt_outcome="partial", error_count=2), "Partial"),
    (native_state(last_attempt_outcome="failed", error_count=3), "Failed"),
])
def test_admin_status_variants_render(admin_page, monkeypatch, state, expected):
    client, webui, _ = admin_page
    monkeypatch.setattr(webui, "get_retention_status", lambda: state)
    response = client.get("/admin")
    assert response.status_code == 200
    assert expected in response.get_data(as_text=True)


@pytest.mark.parametrize("outcome,tone", [
    ("success", "success"), ("partial", "warning"), ("failed", "danger"),
])
def test_retention_status_treatment_preserves_semantic_text(outcome, tone):
    view = build_retention_presentation(native_state(last_attempt_outcome=outcome), schedule_config())
    assert view["status"] == outcome.capitalize()
    assert view["status_tone"] == tone


@pytest.mark.parametrize("last_success", [
    "2026-10-04T02:00:02+00:00", "2026-10-04T02:00:02Z",
    "2026-10-04T03:00:02+01:00",
])
def test_duplicate_success_completion_is_hidden_in_primary_only(admin_page, monkeypatch, last_success):
    client, webui, _ = admin_page
    state = native_state(last_success_completed_at=last_success)
    monkeypatch.setattr(webui, "get_retention_status", lambda: state)
    view = build_retention_presentation(state, schedule_config())
    assert view["show_last_success"] is False
    assert dict(view["primary"])["Last successful run"] == "04 Oct 2026 · 03:00 BST"
    html = client.get("/admin").get_data(as_text=True)
    retention_card = html.split('id="retention-heading"', 1)[1]
    primary, details = retention_card.split('<details class="admin-retention-details">', 1)
    assert "Last successful run" not in primary
    assert "Last successful run" in details
    assert ' open' not in details.split('>', 1)[0]


@pytest.mark.parametrize("outcome", ["partial", "failed"])
def test_older_success_remains_visible_before_disclosure(admin_page, monkeypatch, outcome):
    client, webui, _ = admin_page
    monkeypatch.setattr(webui, "get_retention_status", lambda: native_state(
        last_attempt_outcome=outcome,
        last_success_completed_at="2026-10-03T02:00:00Z", error_count=2,
    ))
    html = client.get("/admin").get_data(as_text=True)
    primary = html.split('id="retention-heading"', 1)[1].split('<details', 1)[0]
    assert outcome.capitalize() in primary
    assert "Last successful run" in primary
    assert "03 Oct 2026 · 03:00 BST" in primary
    assert "Error count</dt><dd>2" in primary


def test_compact_results_keep_found_deleted_and_unknown_distinctions(admin_page, monkeypatch):
    client, webui, _ = admin_page
    monkeypatch.setattr(webui, "get_retention_status", lambda: native_state(
        orphan_count=8, orphan_deletion_count=3, missing_reference_count=None,
    ))
    html = client.get("/admin").get_data(as_text=True)
    results = html.split('aria-label="Cleanup results">', 1)[1].split('</dl>', 1)[0]
    assert "Orphans found</dt><dd>8" in results
    assert "Orphan files deleted</dt><dd>3" in results
    assert "Missing references found</dt><dd>Unknown" in results
    assert "System events pruned</dt><dd>12" in results
    assert "Error count</dt><dd>0" in results
    assert "admin-health-value" not in results


def test_secondary_fields_and_diagnostics_stay_in_collapsed_disclosure(admin_page, monkeypatch):
    client, webui, _ = admin_page
    monkeypatch.setattr(webui, "get_retention_status", lambda: native_state(
        error_count=1, error_summaries=["Controlled diagnostic"],
        last_attempt_outcome="partial",
    ))
    html = client.get("/admin").get_data(as_text=True)
    primary, details = html.split('<details class="admin-retention-details">', 1)
    for label in ("Latest attempt started", "Rows scanned", "Expired snapshots identified",
                  "Expired clips identified", "Expired-media action", "Orphan-media action",
                  "Expired-media phase", "Orphan-scan phase", "System-events phase"):
        assert label not in primary
        assert label in details
    assert "Diagnostics recorded" in primary
    assert "Controlled diagnostic" not in primary
    assert "Controlled diagnostic" in details
    assert "Error count</dt><dd>1" in primary
    assert "<summary>" in details
    assert not re.search(r'<details[^>]*\bopen\b', html)


@pytest.mark.parametrize("overall,label,icon", [
    ("healthy", "All systems OK", "✓"),
    ("degraded", "Attention needed", "!"),
    ("setup_required", "Setup required", "!"),
    ("unhealthy", "Service issue", "×"),
])
def test_compact_sidebar_links_to_health_and_preserves_overall_state(admin_page, monkeypatch, overall, label, icon):
    client, webui, _ = admin_page
    monkeypatch.setattr(webui, "get_system_health", lambda: {
        "overall_state": overall, "setup_required": overall == "setup_required",
        "frigate_online": True, "mqtt_online": True, "database_healthy": True,
        "archive_writable": True, "disk_used_percent": 10,
    })
    html = client.get("/admin").get_data(as_text=True)
    sidebar = re.search(r'<section class="admin-system-status".*?</section>', html, re.S).group(0)
    assert f'status-{overall}' in sidebar
    assert label in sidebar
    assert f'aria-hidden="true">{icon}</span>' in sidebar
    assert 'href="/admin"' in sidebar
    assert "admin-system-checks" not in sidebar
    assert "Bridge" not in sidebar
    assert "Frigate" not in sidebar
    assert "MQTT" not in sidebar


@pytest.mark.parametrize("percent,label", [(20, "Normal"), (75, "Elevated"), (90, "High")])
def test_service_status_text_and_disk_resource_metric(admin_page, monkeypatch, percent, label):
    client, webui, _ = admin_page
    monkeypatch.setattr(webui, "get_system_health", lambda: {
        "frigate_online": False, "mqtt_online": True, "database_healthy": False,
        "archive_writable": True, "disk_used_percent": percent,
    })
    html = client.get("/admin").get_data(as_text=True)
    services = html.split('<dl class="admin-service-grid">', 1)[1].split('</dl>', 1)[0]
    for text in ("Offline", "Online", "Error", "Writable"):
        assert f'>{text}</span>' in services
    assert f'{percent}%</span>' in services
    assert f'{label}</span>' in services
    assert f'value="{percent}" aria-label="System disk utilisation"' in services
    assert 'aria-hidden="true"' in services
    assert "🟢" not in html
    assert "🔴" not in html
    assert "🟡" not in html
