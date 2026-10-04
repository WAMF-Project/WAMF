"""Read-only formatting for the Admin Retention card."""

from collections.abc import Mapping
from datetime import datetime, timezone

from app.retention_schedule import parse_retention_schedule
from app.retention_state import deserialize_diagnostics, serialize_diagnostics


def unavailable_retention_presentation():
    return {
        "status": "Status unavailable",
        "status_tone": "neutral",
        "show_last_success": True,
        "schedule": "Unavailable",
        "timezone_note": "Times shown in UTC (fallback)",
        "primary": [],
        "results": [],
        "details": [],
        "diagnostics": [],
        "legacy_scan": None,
    }


def build_retention_presentation(state, config, *, status_available=True):
    """Format native fields without inferring execution or scheduler state."""
    view = unavailable_retention_presentation()
    display_zone = timezone.utc
    try:
        schedule = parse_retention_schedule(config)
        display_zone = schedule.timezone
        view["timezone_note"] = f"Times shown in {schedule.timezone_name}"
        view["schedule"] = (
            f"Daily at {schedule.daily_time:%H:%M} · {schedule.timezone_name}"
            if schedule.enabled else "Disabled"
        )
    except (AttributeError, TypeError, ValueError, OSError):
        pass

    def timestamp(value):
        if value is None:
            return "Unknown"
        if not isinstance(value, str):
            return "Unknown"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Legacy naive times have no reliable timezone provenance.
                return f"{parsed:%d %b %Y} · {parsed:%H:%M} (timezone unknown)"
            local = parsed.astimezone(display_zone)
            return f"{local:%d %b %Y} · {local:%H:%M %Z}"
        except (ValueError, OverflowError):
            return "Unknown"

    def count(key):
        value = state.get(key)
        return str(value) if type(value) is int and value >= 0 else "Unknown"

    if not status_available or (state is not None and not isinstance(state, Mapping)):
        return view
    if state is None:
        view["status"] = "Never run — No native retention run recorded"
        return view
    if "last_attempt_completed_at" not in state and "legacy_orphan_scan_at" not in state:
        return view

    completed = state.get("last_attempt_completed_at")
    outcome = state.get("last_attempt_outcome")
    if completed is None and outcome is None:
        view["status"] = "Never run — No native retention run recorded"
    elif outcome in ("success", "partial", "failed") and isinstance(completed, str):
        try:
            parsed = datetime.fromisoformat(completed.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return view
        except ValueError:
            return view
        view["status"] = {
            "success": "Success",
            "partial": "Partial",
            "failed": "Failed",
        }[outcome]
        view["status_tone"] = {
            "success": "success", "partial": "warning", "failed": "danger",
        }[outcome]
        if outcome == "success":
            try:
                last_success = datetime.fromisoformat(
                    state.get("last_success_completed_at").replace("Z", "+00:00")
                )
                view["show_last_success"] = last_success != parsed
            except (AttributeError, TypeError, ValueError):
                pass
    else:
        return view

    if state.get("legacy_orphan_scan_at") is not None:
        view["legacy_scan"] = timestamp(state["legacy_orphan_scan_at"])

    duration = count("last_attempt_duration_ms")
    trigger = state.get("last_attempt_trigger")
    trigger_label = {
        "scheduled": "Scheduled", "direct": "Direct", "manual": "Manual",
    }.get(trigger) if isinstance(trigger, str) else None
    if trigger_label is None and isinstance(trigger, str) and trigger.strip():
        # The operator facility supports custom historical trigger names.
        safe_trigger = deserialize_diagnostics(serialize_diagnostics([trigger]))
        trigger_label = safe_trigger[0] if safe_trigger else None
    view["primary"] = [
        ("Latest attempt completed", timestamp(completed) if completed else "Not recorded"),
        ("Last successful run", timestamp(state.get("last_success_completed_at"))),
        ("Trigger", trigger_label or "Unknown"),
        ("Duration", f"{duration} ms" if duration != "Unknown" else duration),
        ("Snapshots deleted", count("deleted_snapshot_count")),
        ("Clips deleted", count("deleted_clip_count")),
        ("Orphans found", count("orphan_count")),
        ("Orphan files deleted", count("orphan_deletion_count")),
        ("Missing references found", count("missing_reference_count")),
        ("System events pruned", count("system_events_pruned")),
        ("Error count", count("error_count")),
    ]
    view["results"] = view["primary"][4:]
    actions = {"delete": "Delete", "report_only": "Report only", "disabled": "Disabled"}
    phases = {"success": "Success", "partial": "Partial", "failed": "Failed", "skipped": "Skipped"}
    for label, key, labels in (
        ("Expired-media action", "expired_media_action", actions),
        ("Orphan-media action", "orphan_media_action", actions),
        ("Expired-media phase", "expired_media_outcome", phases),
        ("Orphan-scan phase", "orphan_scan_outcome", phases),
        ("System-events phase", "system_events_outcome", phases),
    ):
        value = state.get(key)
        view["details"].append((label, labels.get(value, "Unknown") if isinstance(value, str) else "Unknown"))
    view["details"].extend([
        ("Latest attempt started", timestamp(state.get("last_attempt_started_at"))),
        ("Rows scanned", count("rows_scanned")),
        ("Expired snapshots identified", count("expired_snapshot_count")),
        ("Expired clips identified", count("expired_clip_count")),
    ])
    summaries = state.get("error_summaries")
    if isinstance(summaries, list):
        view["diagnostics"] = deserialize_diagnostics(serialize_diagnostics(
            [item for item in summaries if isinstance(item, str)]
        ))
    return view
