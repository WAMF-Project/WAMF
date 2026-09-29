"""Safe in-process retention phases and structured run results."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

from app.config_loader import load_runtime_config
from app.config_migration import migrate_config
from app.config_validation import validate_config
from app.db import connect_db
from app.media_coordination import (
    media_activity_guard,
    retention_execution_guard,
)
from app.system_events import log_system_event
from wamf_paths import (
    contained_media_path,
    get_clips_path,
    get_database_path,
    get_snapshots_path,
)


logger = logging.getLogger(__name__)
DEFAULT_SNAPSHOTS_DAYS = 90
DEFAULT_CLIPS_DAYS = 90
DEFAULT_SYSTEM_EVENTS_DAYS = 90
DEFAULT_SYSTEM_EVENTS_MIN_ROWS = 1000
PHASE_NAMES = ("expired_media", "orphan_scan", "system_events")


class RetentionPolicyError(ValueError):
    """The effective retention configuration cannot be applied safely."""


@dataclass(frozen=True)
class SpeciesRetentionOverride:
    scientific_name: str
    normalized_name: str
    snapshots_days: int | None
    clips_days: int | None
    values: dict


@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool
    snapshots_days: int
    clips_days: int
    delete_media: bool
    orphan_scan_enabled: bool
    delete_orphaned_media: bool
    system_events_days: int
    system_events_min_rows: int
    species_overrides: tuple[SpeciesRetentionOverride, ...]
    overrides_by_name: dict = field(repr=False, compare=False)


@dataclass
class RetentionPhaseResult:
    name: str
    outcome: str = "success"
    rows_scanned: int = 0
    items_scanned: int = 0
    error_summaries: list[str] = field(default_factory=list)

    @property
    def error_count(self):
        return len(self.error_summaries)


@dataclass
class RetentionRunResult:
    trigger: str
    outcome: str = "success"
    phases: dict[str, RetentionPhaseResult] = field(default_factory=dict)
    rows_scanned: int = 0
    expired_snapshot_count: int = 0
    expired_clip_count: int = 0
    deleted_snapshot_count: int = 0
    deleted_clip_count: int = 0
    orphan_count: int = 0
    orphan_deletion_count: int = 0
    missing_reference_count: int = 0
    system_events_pruned: int = 0
    error_summaries: list[str] = field(default_factory=list)

    @property
    def error_count(self):
        return len(self.error_summaries)


def _normalized_species_name(value):
    return value.strip().casefold()


def _boolean_value(mapping, key, default, path):
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise RetentionPolicyError(f"{path} must be a boolean")
    return value


def _days_value(value, path):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetentionPolicyError(f"{path} must be a non-negative integer")
    return value


def build_retention_policy(config):
    """Validate and compile retention settings from one canonical snapshot."""

    retention = config.get("retention", {})
    if not isinstance(retention, Mapping):
        raise RetentionPolicyError("retention must be a mapping")

    snapshots_days = _days_value(
        retention.get("snapshots_days", DEFAULT_SNAPSHOTS_DAYS),
        "retention.snapshots_days",
    )
    clips_days = _days_value(
        retention.get("clips_days", DEFAULT_CLIPS_DAYS),
        "retention.clips_days",
    )
    system_events_days = _days_value(
        retention.get("system_events_days", DEFAULT_SYSTEM_EVENTS_DAYS),
        "retention.system_events_days",
    )
    system_events_min_rows = _days_value(
        retention.get("system_events_min_rows", DEFAULT_SYSTEM_EVENTS_MIN_ROWS),
        "retention.system_events_min_rows",
    )

    raw_overrides = retention.get("species_overrides", {})
    if not isinstance(raw_overrides, Mapping):
        raise RetentionPolicyError(
            "retention.species_overrides must be a mapping"
        )

    compiled_overrides = []
    overrides_by_name = {}
    for scientific_name, raw_values in raw_overrides.items():
        if not isinstance(scientific_name, str) or not scientific_name.strip():
            raise RetentionPolicyError(
                "retention.species_overrides keys must be scientific-name strings"
            )
        normalized_name = _normalized_species_name(scientific_name)
        if normalized_name in overrides_by_name:
            previous = overrides_by_name[normalized_name].scientific_name
            raise RetentionPolicyError(
                "retention.species_overrides contains duplicate scientific name "
                f"keys after case/whitespace normalization: {previous!r} and "
                f"{scientific_name!r}"
            )
        if not isinstance(raw_values, Mapping):
            raise RetentionPolicyError(
                f"retention.species_overrides[{scientific_name!r}] must be a mapping"
            )

        snapshot_override = None
        clip_override = None
        if "snapshots_days" in raw_values:
            snapshot_override = _days_value(
                raw_values["snapshots_days"],
                f"retention.species_overrides[{scientific_name!r}].snapshots_days",
            )
        if "clips_days" in raw_values:
            clip_override = _days_value(
                raw_values["clips_days"],
                f"retention.species_overrides[{scientific_name!r}].clips_days",
            )

        override = SpeciesRetentionOverride(
            scientific_name=scientific_name,
            normalized_name=normalized_name,
            snapshots_days=snapshot_override,
            clips_days=clip_override,
            values=deepcopy(dict(raw_values)),
        )
        compiled_overrides.append(override)
        overrides_by_name[normalized_name] = override

    return RetentionPolicy(
        enabled=_boolean_value(retention, "enabled", True, "retention.enabled"),
        snapshots_days=snapshots_days,
        clips_days=clips_days,
        delete_media=_boolean_value(
            retention, "delete_media", False, "retention.delete_media"
        ),
        orphan_scan_enabled=_boolean_value(
            retention,
            "orphan_scan_enabled",
            True,
            "retention.orphan_scan_enabled",
        ),
        delete_orphaned_media=_boolean_value(
            retention,
            "delete_orphaned_media",
            False,
            "retention.delete_orphaned_media",
        ),
        system_events_days=system_events_days,
        system_events_min_rows=system_events_min_rows,
        species_overrides=tuple(compiled_overrides),
        overrides_by_name=overrides_by_name,
    )


def get_retention_days(policy, species_name, media_type):
    """Resolve an independent snapshot/clip policy by scientific name."""

    if media_type not in {"snapshots", "clips"}:
        raise ValueError("media_type must be snapshots or clips")
    global_days = (
        policy.snapshots_days if media_type == "snapshots" else policy.clips_days
    )
    if not isinstance(species_name, str):
        return global_days
    override = policy.overrides_by_name.get(
        _normalized_species_name(species_name)
    )
    if override is None:
        return global_days
    override_days = (
        override.snapshots_days
        if media_type == "snapshots"
        else override.clips_days
    )
    return global_days if override_days is None else override_days


def _new_result(trigger):
    return RetentionRunResult(
        trigger=str(trigger),
        phases={
            name: RetentionPhaseResult(name=name)
            for name in PHASE_NAMES
        },
    )


def _safe_error_summary(message, exc=None):
    if exc is None:
        return str(message)[:500]
    return f"{message}: {type(exc).__name__}: {exc}"[:500]


def _add_error(result, phase, message, exc=None):
    summary = _safe_error_summary(message, exc)
    phase_result = result.phases[phase]
    phase_result.error_summaries.append(summary)
    if phase_result.outcome == "success":
        phase_result.outcome = "partial"
    result.error_summaries.append(summary)
    logger.warning("Retention %s", summary)


def _emit_event(database_path, severity, message):
    try:
        log_system_event(
            severity,
            "RETENTION",
            message,
            db_path=database_path,
        )
    except Exception as exc:  # Logging must not take down a maintenance run.
        logger.warning("Unable to record retention system event: %s", exc)


def _effective_config(config):
    snapshot = (
        load_runtime_config()
        if config is None
        else migrate_config(deepcopy(config)).config
    )
    validation = validate_config(snapshot)
    if validation.errors:
        fields = ", ".join(issue.field for issue in validation.errors)
        raise RetentionPolicyError(
            f"effective configuration is structurally invalid: {fields}"
        )
    return snapshot


def _clear_media_reference(conn, row_id, column, stored_value):
    cursor = conn.execute(
        f"UPDATE detections SET {column} = NULL WHERE id = ? AND {column} = ?",
        (row_id, stored_value),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise RuntimeError("media reference changed during retention")
    conn.commit()


def _restore_media_reference(conn, row_id, column, stored_value):
    cursor = conn.execute(
        f"UPDATE detections SET {column} = ? WHERE id = ? AND {column} IS NULL",
        (stored_value, row_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise RuntimeError("media reference could not be restored")
    conn.commit()


def _unlink_media_file(path):
    path.unlink()


def _process_expired_item(
    result,
    conn,
    config,
    row_id,
    stored_value,
    media_type,
):
    singular = "snapshot" if media_type == "snapshots" else "clip"
    column = f"wamf_{singular}_path"
    path = contained_media_path(stored_value, media_type, config)
    if path is None:
        _add_error(
            result,
            "expired_media",
            f"detection {row_id} {singular} path is outside its configured archive root",
        )
        return

    if not path.exists():
        try:
            _clear_media_reference(conn, row_id, column, stored_value)
        except Exception as exc:
            conn.rollback()
            _add_error(
                result,
                "expired_media",
                f"could not clear missing {singular} reference for detection {row_id}",
                exc,
            )
        return

    if not path.is_file():
        _add_error(
            result,
            "expired_media",
            f"detection {row_id} {singular} path is not a regular file",
        )
        return

    # SQLite and the filesystem cannot share a transaction. Clear and commit the
    # reference before unlinking so a DB failure never permits file deletion. A
    # crash or unlink failure can at worst leave a recoverable orphan file.
    try:
        _clear_media_reference(conn, row_id, column, stored_value)
    except Exception as exc:
        conn.rollback()
        _add_error(
            result,
            "expired_media",
            f"could not clear {singular} reference for detection {row_id}",
            exc,
        )
        return

    try:
        _unlink_media_file(path)
    except FileNotFoundError:
        return
    except Exception as exc:
        try:
            _restore_media_reference(
                conn,
                row_id,
                column,
                stored_value,
            )
        except Exception as restore_exc:
            conn.rollback()
            _add_error(
                result,
                "expired_media",
                f"could not restore {singular} reference for detection {row_id}",
                restore_exc,
            )
        _add_error(
            result,
            "expired_media",
            f"could not delete {singular} for detection {row_id}",
            exc,
        )
        return

    if media_type == "snapshots":
        result.deleted_snapshot_count += 1
    else:
        result.deleted_clip_count += 1
    _emit_event(
        get_database_path(config),
        "INFO",
        f"Deleted {singular}: {path}",
    )


def _run_expired_media(result, config, policy, now, database_path):
    phase = result.phases["expired_media"]
    if not policy.enabled:
        phase.outcome = "skipped"
        _emit_event(database_path, "INFO", "Retention disabled")
        return

    conn = None
    with media_activity_guard(database_path):
        try:
            conn = connect_db(database_path)
            rows = conn.execute(
                """
                SELECT id, detection_time, display_name,
                       wamf_snapshot_path, wamf_clip_path
                FROM detections
                """
            ).fetchall()
            phase.rows_scanned = len(rows)
            result.rows_scanned = max(result.rows_scanned, len(rows))

            for row in rows:
                phase.items_scanned += 1
                try:
                    detection_time = datetime.fromisoformat(row["detection_time"])
                    age_days = (now - detection_time).days
                except (TypeError, ValueError) as exc:
                    _add_error(
                        result,
                        "expired_media",
                        f"detection {row['id']} has an invalid detection time",
                        exc,
                    )
                    continue

                for media_type, field_name in (
                    ("snapshots", "wamf_snapshot_path"),
                    ("clips", "wamf_clip_path"),
                ):
                    stored_value = row[field_name]
                    if not stored_value:
                        continue
                    retention_days = get_retention_days(
                        policy,
                        row["display_name"],
                        media_type,
                    )
                    if age_days <= retention_days:
                        continue

                    if media_type == "snapshots":
                        result.expired_snapshot_count += 1
                    else:
                        result.expired_clip_count += 1

                    path = contained_media_path(stored_value, media_type, config)
                    if path is None:
                        singular = (
                            "snapshot" if media_type == "snapshots" else "clip"
                        )
                        _add_error(
                            result,
                            "expired_media",
                            f"detection {row['id']} {singular} path is outside "
                            "its configured archive root",
                        )
                        continue

                    if not policy.delete_media:
                        singular = (
                            "snapshot" if media_type == "snapshots" else "clip"
                        )
                        logger.info("[DRY RUN] Would delete %s: %s", singular, path)
                        _emit_event(
                            database_path,
                            "INFO",
                            f"Would delete {singular}: {path}",
                        )
                        continue

                    _process_expired_item(
                        result,
                        conn,
                        config,
                        row["id"],
                        stored_value,
                        media_type,
                    )
        finally:
            if conn is not None:
                conn.close()


def _update_retention_status(conn, now, rows, orphan_count, missing_count):
    conn.execute("DELETE FROM retention_status")
    conn.execute(
        """
        INSERT INTO retention_status (
            last_run, rows_scanned, orphan_count, missing_count
        )
        VALUES (?, ?, ?, ?)
        """,
        (now.isoformat(), rows, orphan_count, missing_count),
    )
    conn.commit()


def _run_orphan_scan(result, config, policy, now, database_path):
    phase = result.phases["orphan_scan"]
    if not policy.orphan_scan_enabled:
        phase.outcome = "skipped"
        _emit_event(database_path, "INFO", "Orphan scan disabled")
        return

    conn = None
    with media_activity_guard(database_path):
        try:
            conn = connect_db(database_path)
            rows = conn.execute(
                "SELECT id, wamf_snapshot_path, wamf_clip_path FROM detections"
            ).fetchall()
            phase.rows_scanned = len(rows)
            result.rows_scanned = max(result.rows_scanned, len(rows))
            referenced_files = set()

            for row in rows:
                for media_type, field_name in (
                    ("snapshots", "wamf_snapshot_path"),
                    ("clips", "wamf_clip_path"),
                ):
                    stored_value = row[field_name]
                    if not stored_value:
                        continue
                    path = contained_media_path(stored_value, media_type, config)
                    if path is None:
                        _add_error(
                            result,
                            "orphan_scan",
                            f"detection {row['id']} {media_type} reference is "
                            "outside its configured archive root",
                        )
                        continue
                    referenced_files.add(path.resolve())

            for media_type, media_dir in (
                ("snapshots", get_snapshots_path(config)),
                ("clips", get_clips_path(config)),
            ):
                for file_path in media_dir.glob("*"):
                    phase.items_scanned += 1
                    try:
                        resolved_file = file_path.resolve()
                    except (OSError, RuntimeError) as exc:
                        _add_error(
                            result,
                            "orphan_scan",
                            f"could not resolve archive entry {file_path.name!r}",
                            exc,
                        )
                        continue
                    if resolved_file in referenced_files:
                        continue

                    result.orphan_count += 1
                    logger.warning("[ORPHAN] %s", file_path)
                    _emit_event(
                        database_path,
                        "WARN",
                        f"Orphan detected: {file_path}",
                    )
                    if not policy.delete_orphaned_media or not file_path.is_file():
                        continue
                    if contained_media_path(file_path, media_type, config) is None:
                        _add_error(
                            result,
                            "orphan_scan",
                            f"orphan {file_path.name!r} resolves outside its "
                            "configured archive root",
                        )
                        continue
                    try:
                        _unlink_media_file(file_path)
                    except Exception as exc:
                        _add_error(
                            result,
                            "orphan_scan",
                            f"could not delete orphan {file_path.name!r}",
                            exc,
                        )
                        continue
                    result.orphan_deletion_count += 1
                    _emit_event(
                        database_path,
                        "INFO",
                        f"Deleted orphan media: {file_path}",
                    )

            for file_path in referenced_files:
                if not file_path.exists():
                    logger.warning("[MISSING] %s", file_path)
                    result.missing_reference_count += 1

            if phase.error_count == 0:
                _update_retention_status(
                    conn,
                    now,
                    len(rows),
                    result.orphan_count,
                    result.missing_reference_count,
                )
        finally:
            if conn is not None:
                conn.close()

    scan_outcome = "complete" if phase.error_count == 0 else "partial"
    _emit_event(
        database_path,
        "INFO" if phase.error_count == 0 else "ERROR",
        f"Retention scan {scan_outcome}. "
        f"Scanned {phase.rows_scanned} rows. "
        f"Orphans found: {result.orphan_count}. "
        f"Missing files: {result.missing_reference_count}",
    )


def _run_system_event_pruning(result, config, policy, now, database_path):
    phase = result.phases["system_events"]
    if not policy.enabled:
        phase.outcome = "skipped"
        _emit_event(database_path, "INFO", "System event retention disabled")
        return

    cutoff = (now - timedelta(days=policy.system_events_days)).isoformat()
    conn = connect_db(database_path)
    try:
        cursor = conn.execute(
            """
            DELETE FROM system_events
            WHERE timestamp < ?
            AND id NOT IN (
                SELECT id
                FROM system_events
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (cutoff, policy.system_events_min_rows),
        )
        result.system_events_pruned = cursor.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if result.system_events_pruned:
        _emit_event(
            database_path,
            "INFO",
            f"Pruned {result.system_events_pruned} system events older than "
            f"{policy.system_events_days} days; kept newest "
            f"{policy.system_events_min_rows} rows",
        )


def _execute_phase(result, phase_name, phase_function, *args):
    phase = result.phases[phase_name]
    previous_error_count = phase.error_count
    try:
        phase_function(result, *args)
    except Exception as exc:
        phase.outcome = "failed"
        _add_error(
            result,
            phase_name,
            f"{phase_name.replace('_', ' ')} phase failed",
            exc,
        )
    database_path = args[-1]
    for summary in phase.error_summaries[previous_error_count:]:
        _emit_event(database_path, "ERROR", summary)


def _finalize_outcome(result):
    active_phases = [
        phase for phase in result.phases.values()
        if phase.outcome != "skipped"
    ]
    if active_phases and all(
        phase.outcome == "failed" for phase in active_phases
    ):
        result.outcome = "failed"
    elif any(
        phase.outcome in {"partial", "failed"} for phase in active_phases
    ):
        result.outcome = "partial"
    else:
        result.outcome = "success"


def _run_acquired(result, config_snapshot, policy, run_now, database_path):
    _emit_event(
        database_path,
        "INFO",
        f"Retention run started ({result.trigger})",
    )
    _execute_phase(
        result,
        "expired_media",
        _run_expired_media,
        config_snapshot,
        policy,
        run_now,
        database_path,
    )
    _execute_phase(
        result,
        "orphan_scan",
        _run_orphan_scan,
        config_snapshot,
        policy,
        run_now,
        database_path,
    )
    _execute_phase(
        result,
        "system_events",
        _run_system_event_pruning,
        config_snapshot,
        policy,
        run_now,
        database_path,
    )
    _finalize_outcome(result)
    severity = "INFO" if result.outcome == "success" else "ERROR"
    _emit_event(
        database_path,
        severity,
        f"Retention run {result.outcome}. "
        f"Expired snapshots: {result.expired_snapshot_count}; "
        f"expired clips: {result.expired_clip_count}; "
        f"deleted snapshots: {result.deleted_snapshot_count}; "
        f"deleted clips: {result.deleted_clip_count}; "
        f"orphans: {result.orphan_count}; "
        f"orphan deletions: {result.orphan_deletion_count}; "
        f"missing: {result.missing_reference_count}; "
        f"events pruned: {result.system_events_pruned}; "
        f"errors: {result.error_count}",
    )
    return result


def run_retention(trigger="direct", *, config=None, now=None):
    """Run all independent retention phases without allowing overlap."""

    result = _new_result(trigger)
    try:
        config_snapshot = _effective_config(config)
        policy = build_retention_policy(config_snapshot)
        database_path = get_database_path(config_snapshot)
    except Exception as exc:
        result.outcome = "failed"
        result.error_summaries.append(
            _safe_error_summary("retention configuration could not be prepared", exc)
        )
        for phase in result.phases.values():
            phase.outcome = "skipped"
        logger.warning("Retention configuration could not be prepared: %s", exc)
        return result

    started = False
    try:
        with retention_execution_guard(database_path) as acquired:
            if not acquired:
                result.outcome = "already_running"
                for phase in result.phases.values():
                    phase.outcome = "skipped"
                logger.info("Retention run rejected because another run is active")
                return result
            started = True
            return _run_acquired(
                result,
                config_snapshot,
                policy,
                now or datetime.now(),
                database_path,
            )
    except Exception as exc:
        result.outcome = "failed"
        summary = _safe_error_summary("retention run failed outside a phase", exc)
        result.error_summaries.append(summary)
        if not started:
            for phase in result.phases.values():
                phase.outcome = "skipped"
        logger.warning("Retention run failed outside a phase: %s", exc)
        return result
