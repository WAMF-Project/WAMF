"""Persistence boundary for the latest completed retention invocation."""

from __future__ import annotations

import json
import re

from app.db import connect_db


RECORD_VERSION = 1
MAX_DIAGNOSTICS = 10
MAX_DIAGNOSTIC_CHARS = 240
MAX_DIAGNOSTICS_JSON_BYTES = 2048

_URI_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s,;]+", re.IGNORECASE)
_AUTHORIZATION_RE = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;]+"
)
_UNC_PATH_RE = re.compile(r"(?<!\\)\\\\[^\s,;]+")
_WINDOWS_PATH_RE = re.compile(r"(?<!\w)[A-Za-z]:[\\/][^\s,;]+")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.])/[^\s,;]+")
_SECRET_RE = re.compile(
    r"(?i)\b[\w-]*(?:password|passwd|token|secret|credential|api[_-]?key)"
    r"[\w-]*\b(?:\s*[:=]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_OUTCOMES = {"success", "partial", "failed"}
_PHASE_OUTCOMES = _OUTCOMES | {"skipped"}
_ACTIONS = {"delete", "report_only", "disabled", None}
_COUNTERS = (
    "rows_scanned",
    "expired_snapshot_count",
    "expired_clip_count",
    "deleted_snapshot_count",
    "deleted_clip_count",
    "orphan_count",
    "orphan_deletion_count",
    "missing_reference_count",
    "system_events_pruned",
)


def _sanitize_diagnostic(summary):
    """Return a bounded durable diagnostic without volatile sensitive details."""

    text = str(summary).encode("utf-8", errors="replace").decode("utf-8")
    text = " ".join(text.split())
    text = _URI_RE.sub("[redacted-uri]", text)
    text = _AUTHORIZATION_RE.sub("authorization=[redacted]", text)
    text = _SECRET_RE.sub("credential=[redacted]", text)
    text = _UNC_PATH_RE.sub("[redacted-path]", text)
    text = _WINDOWS_PATH_RE.sub("[redacted-path]", text)
    text = _ABSOLUTE_PATH_RE.sub("[redacted-path]", text)
    return text[:MAX_DIAGNOSTIC_CHARS]


def serialize_diagnostics(summaries):
    """Serialize at most ten diagnostics within a fixed UTF-8 byte budget."""

    durable = []
    for summary in list(summaries)[:MAX_DIAGNOSTICS]:
        sanitized = _sanitize_diagnostic(summary)
        candidate = durable + [sanitized]
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_DIAGNOSTICS_JSON_BYTES:
            break
        durable = candidate
    return json.dumps(durable, ensure_ascii=False, separators=(",", ":"))


def deserialize_diagnostics(value):
    """Safely decode and re-bound durable diagnostics from persisted state."""

    if value is None:
        return []
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return []
    if not isinstance(decoded, list):
        return []
    strings = [item for item in decoded if isinstance(item, str)]
    return json.loads(serialize_diagnostics(strings))


def _validate_result(result):
    if result.outcome not in _OUTCOMES:
        raise ValueError("retention outcome is not persistable")
    if result.started_at.tzinfo is None or result.completed_at.tzinfo is None:
        raise ValueError("retention operational timestamps must be timezone-aware")
    if not isinstance(result.duration_ms, int) or result.duration_ms < 0:
        raise ValueError("retention duration must be non-negative integer milliseconds")
    for name in ("expired_media", "orphan_scan", "system_events"):
        if result.phases[name].outcome not in _PHASE_OUTCOMES:
            raise ValueError(f"invalid {name} outcome")
    for name in ("expired_media", "orphan_scan"):
        if result.phases[name].action not in _ACTIONS:
            raise ValueError(f"invalid {name} action")
    for name in _COUNTERS:
        value = getattr(result, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid retention counter: {name}")


class RetentionStateRepository:
    """Read and atomically replace the singleton retention state snapshot."""

    def __init__(self, database_path):
        self.database_path = database_path

    def read(self):
        conn = connect_db(self.database_path)
        try:
            row = conn.execute(
                "SELECT * FROM retention_status WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            state = dict(row)
            state["last_run"] = (
                state["last_attempt_completed_at"]
                or state["legacy_orphan_scan_at"]
            )
            state["missing_count"] = state["missing_reference_count"]
            state["error_summaries"] = deserialize_diagnostics(
                state["error_summaries_json"]
            )
            return state
        finally:
            conn.close()

    def persist(self, result):
        """Atomically replace latest-attempt fields and conditionally last success."""

        _validate_result(result)
        values = (
            RECORD_VERSION,
            result.started_at.isoformat(),
            result.completed_at.isoformat(),
            result.duration_ms,
            result.trigger,
            result.outcome,
            result.phases["expired_media"].outcome,
            result.phases["orphan_scan"].outcome,
            result.phases["system_events"].outcome,
            result.phases["expired_media"].action,
            result.phases["orphan_scan"].action,
            result.rows_scanned,
            result.expired_snapshot_count,
            result.expired_clip_count,
            result.deleted_snapshot_count,
            result.deleted_clip_count,
            result.orphan_count,
            result.orphan_deletion_count,
            result.missing_reference_count,
            result.system_events_pruned,
            result.error_count,
            serialize_diagnostics(result.error_summaries),
        )
        conn = connect_db(self.database_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO retention_status (
                    id, record_version,
                    last_attempt_started_at, last_attempt_completed_at,
                    last_attempt_duration_ms, last_attempt_trigger,
                    last_attempt_outcome, last_success_completed_at,
                    expired_media_outcome, orphan_scan_outcome,
                    system_events_outcome, expired_media_action,
                    orphan_media_action, rows_scanned,
                    expired_snapshot_count, expired_clip_count,
                    deleted_snapshot_count, deleted_clip_count,
                    orphan_count, orphan_deletion_count,
                    missing_reference_count, system_events_pruned,
                    error_count, error_summaries_json, legacy_orphan_scan_at
                ) VALUES (
                    1, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? = 'success' THEN ? ELSE NULL END,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL
                )
                ON CONFLICT(id) DO UPDATE SET
                    record_version = excluded.record_version,
                    last_attempt_started_at = excluded.last_attempt_started_at,
                    last_attempt_completed_at = excluded.last_attempt_completed_at,
                    last_attempt_duration_ms = excluded.last_attempt_duration_ms,
                    last_attempt_trigger = excluded.last_attempt_trigger,
                    last_attempt_outcome = excluded.last_attempt_outcome,
                    last_success_completed_at = CASE
                        WHEN excluded.last_attempt_outcome = 'success'
                        THEN excluded.last_attempt_completed_at
                        ELSE retention_status.last_success_completed_at
                    END,
                    expired_media_outcome = excluded.expired_media_outcome,
                    orphan_scan_outcome = excluded.orphan_scan_outcome,
                    system_events_outcome = excluded.system_events_outcome,
                    expired_media_action = excluded.expired_media_action,
                    orphan_media_action = excluded.orphan_media_action,
                    rows_scanned = excluded.rows_scanned,
                    expired_snapshot_count = excluded.expired_snapshot_count,
                    expired_clip_count = excluded.expired_clip_count,
                    deleted_snapshot_count = excluded.deleted_snapshot_count,
                    deleted_clip_count = excluded.deleted_clip_count,
                    orphan_count = excluded.orphan_count,
                    orphan_deletion_count = excluded.orphan_deletion_count,
                    missing_reference_count = excluded.missing_reference_count,
                    system_events_pruned = excluded.system_events_pruned,
                    error_count = excluded.error_count,
                    error_summaries_json = excluded.error_summaries_json,
                    legacy_orphan_scan_at = NULL
                """,
                values[:6]
                + (result.outcome, result.completed_at.isoformat())
                + values[6:],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_retention_state(database_path=None):
    return RetentionStateRepository(database_path).read()
