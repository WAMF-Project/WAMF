from contextlib import contextmanager
from datetime import datetime, timedelta
import multiprocessing
from pathlib import Path
import sqlite3
import threading

import pytest

from app.db import ensure_schema


def _hold_retention_guard(database_path, ready, release):
    from app.media_coordination import retention_execution_guard

    with retention_execution_guard(database_path) as acquired:
        if not acquired:
            return
        ready.set()
        release.wait(5)


def retention_config(tmp_path, **retention_updates):
    snapshots = tmp_path / "snapshots"
    clips = tmp_path / "clips"
    snapshots.mkdir(exist_ok=True)
    clips.mkdir(exist_ok=True)
    config = {
        "config_version": 2,
        "storage": {"database_path": str(tmp_path / "wamf.db")},
        "media": {
            "snapshots_path": str(snapshots),
            "clips_path": str(clips),
        },
        "retention": {
            "enabled": True,
            "snapshots_days": 90,
            "clips_days": 90,
            "delete_media": True,
            "orphan_scan_enabled": True,
            "delete_orphaned_media": False,
            "system_events_days": 90,
            "system_events_min_rows": 0,
            "species_overrides": {},
        },
    }
    config["retention"].update(retention_updates)
    ensure_schema(config["storage"]["database_path"])
    return config


def insert_detection(
    config,
    *,
    detection_time,
    species="Cyanistes caeruleus",
    snapshot=None,
    clip=None,
):
    conn = sqlite3.connect(config["storage"]["database_path"])
    cursor = conn.execute(
        """
        INSERT INTO detections (
            detection_time,
            display_name,
            frigate_event,
            wamf_snapshot_path,
            wamf_clip_path
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            detection_time.isoformat(),
            species,
            f"event-{detection_time.timestamp()}-{snapshot}-{clip}",
            str(snapshot) if snapshot is not None else None,
            str(clip) if clip is not None else None,
        ),
    )
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id


def media_values(config, row_id):
    conn = sqlite3.connect(config["storage"]["database_path"])
    row = conn.execute(
        "SELECT wamf_snapshot_path, wamf_clip_path FROM detections WHERE id = ?",
        (row_id,),
    ).fetchone()
    conn.close()
    return row


@pytest.mark.parametrize(
    ("stored_name", "lookup_name"),
    [
        ("Cyanistes caeruleus", "Cyanistes caeruleus"),
        ("Cyanistes caeruleus", "CYANISTES CAERULEUS"),
        ("  Cyanistes caeruleus  ", " cyanistes CAERULEUS "),
    ],
)
def test_species_policy_matches_normalized_scientific_name(
    tmp_path, stored_name, lookup_name
):
    from app.retention_service import build_retention_policy, get_retention_days

    config = retention_config(tmp_path)
    config["retention"]["species_overrides"] = {
        stored_name: {"snapshots_days": 365, "clips_days": 180},
    }

    policy = build_retention_policy(config)

    assert get_retention_days(policy, lookup_name, "snapshots") == 365
    assert get_retention_days(policy, lookup_name, "clips") == 180
    assert policy.species_overrides[0].scientific_name == stored_name


def test_species_policy_global_and_independent_leaf_fallbacks(tmp_path):
    from app.retention_service import build_retention_policy, get_retention_days

    config = retention_config(tmp_path, snapshots_days=90, clips_days=30)
    config["retention"]["species_overrides"] = {
        "Snapshot only": {"snapshots_days": 0},
        "Clip only": {"clips_days": 7},
    }
    policy = build_retention_policy(config)

    assert get_retention_days(policy, "Snapshot only", "snapshots") == 0
    assert get_retention_days(policy, "Snapshot only", "clips") == 30
    assert get_retention_days(policy, "Clip only", "snapshots") == 90
    assert get_retention_days(policy, "Clip only", "clips") == 7
    assert get_retention_days(policy, "Unknown species", "snapshots") == 90
    assert get_retention_days(policy, None, "clips") == 30


@pytest.mark.parametrize(
    "overrides",
    [
        {"Cyanistes caeruleus": {"snapshots_days": None}},
        {"Cyanistes caeruleus": {"clips_days": "thirty"}},
        {"Cyanistes caeruleus": None},
        {"Cyanistes caeruleus": []},
    ],
)
def test_species_policy_rejects_null_invalid_and_malformed_values(
    tmp_path, overrides
):
    from app.retention_service import RetentionPolicyError, build_retention_policy

    config = retention_config(tmp_path)
    config["retention"]["species_overrides"] = overrides

    with pytest.raises(RetentionPolicyError, match="species_overrides"):
        build_retention_policy(config)


def test_species_policy_rejects_normalized_duplicate_names(tmp_path):
    from app.retention_service import RetentionPolicyError, build_retention_policy

    config = retention_config(tmp_path)
    config["retention"]["species_overrides"] = {
        "Cyanistes caeruleus": {"snapshots_days": 365},
        " cyanistes CAERULEUS ": {"snapshots_days": 90},
    }

    with pytest.raises(RetentionPolicyError, match="duplicate scientific name"):
        build_retention_policy(config)


def test_invalid_policy_returns_structured_failure_without_running_phases(tmp_path):
    from app.retention_service import run_retention

    config = retention_config(tmp_path)
    config["retention"]["species_overrides"] = {
        "Cyanistes caeruleus": {"snapshots_days": None},
    }

    result = run_retention("test", config=config)

    assert result.outcome == "failed"
    assert result.error_count == 1
    assert "snapshots_days" in result.error_summaries[0]
    assert {phase.outcome for phase in result.phases.values()} == {"skipped"}


def test_expired_media_deletes_contained_files_and_clears_references(tmp_path):
    from app.retention_service import run_retention

    config = retention_config(tmp_path, orphan_scan_enabled=False)
    snapshot = Path(config["media"]["snapshots_path"]) / "old.jpg"
    clip = Path(config["media"]["clips_path"]) / "old.mp4"
    snapshot.write_bytes(b"snapshot")
    clip.write_bytes(b"clip")
    row_id = insert_detection(
        config,
        detection_time=datetime.now() - timedelta(days=120),
        snapshot=snapshot,
        clip=clip,
    )

    result = run_retention("test", config=config)

    assert result.outcome == "success"
    assert result.expired_snapshot_count == 1
    assert result.expired_clip_count == 1
    assert result.deleted_snapshot_count == 1
    assert result.deleted_clip_count == 1
    assert not snapshot.exists()
    assert not clip.exists()
    assert media_values(config, row_id) == (None, None)


def test_expired_missing_file_clears_reference_without_counting_deletion(tmp_path):
    from app.retention_service import run_retention

    config = retention_config(tmp_path, orphan_scan_enabled=False)
    missing = Path(config["media"]["snapshots_path"]) / "missing.jpg"
    row_id = insert_detection(
        config,
        detection_time=datetime.now() - timedelta(days=120),
        snapshot=missing,
    )

    result = run_retention("test", config=config)

    assert result.outcome == "success"
    assert result.expired_snapshot_count == 1
    assert result.deleted_snapshot_count == 0
    assert media_values(config, row_id)[0] is None


def test_report_only_expired_media_preserves_file_and_reference(tmp_path):
    from app.retention_service import run_retention

    config = retention_config(
        tmp_path,
        delete_media=False,
        orphan_scan_enabled=False,
    )
    snapshot = Path(config["media"]["snapshots_path"]) / "old.jpg"
    snapshot.write_bytes(b"snapshot")
    row_id = insert_detection(
        config,
        detection_time=datetime.now() - timedelta(days=120),
        snapshot=snapshot,
    )

    result = run_retention("test", config=config)

    assert result.outcome == "success"
    assert result.expired_snapshot_count == 1
    assert result.deleted_snapshot_count == 0
    assert snapshot.exists()
    assert media_values(config, row_id)[0] == str(snapshot)


def test_expired_media_refuses_outside_absolute_path_without_rewriting_db(tmp_path):
    from app.retention_service import run_retention

    config = retention_config(tmp_path, orphan_scan_enabled=False)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"must survive")
    row_id = insert_detection(
        config,
        detection_time=datetime.now() - timedelta(days=120),
        snapshot=outside,
    )

    result = run_retention("test", config=config)

    assert result.outcome == "partial"
    assert result.error_count == 1
    assert outside.read_bytes() == b"must survive"
    assert media_values(config, row_id)[0] == str(outside)


def test_database_clear_failure_does_not_delete_file_and_other_items_continue(
    tmp_path, monkeypatch
):
    from app import retention_service

    config = retention_config(tmp_path, orphan_scan_enabled=False)
    snapshots = Path(config["media"]["snapshots_path"])
    first = snapshots / "first.jpg"
    second = snapshots / "second.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    old = datetime.now() - timedelta(days=120)
    first_id = insert_detection(config, detection_time=old, snapshot=first)
    second_id = insert_detection(config, detection_time=old, snapshot=second)
    real_connect = retention_service.connect_db
    connection_count = 0

    class FailFirstCommit:
        def __init__(self, connection):
            self.connection = connection
            self.failed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            if not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("controlled commit failure")
            self.connection.commit()

    def connect_with_one_failure(path):
        nonlocal connection_count
        connection = real_connect(path)
        connection_count += 1
        return FailFirstCommit(connection) if connection_count == 1 else connection

    monkeypatch.setattr(retention_service, "connect_db", connect_with_one_failure)

    result = retention_service.run_retention("test", config=config)

    assert result.outcome == "partial"
    assert first.exists()
    assert media_values(config, first_id)[0] == str(first)
    assert not second.exists()
    assert media_values(config, second_id)[0] is None


def test_unlink_failure_restores_reference_and_other_items_continue(
    tmp_path, monkeypatch
):
    from app import retention_service

    config = retention_config(tmp_path, orphan_scan_enabled=False)
    snapshots = Path(config["media"]["snapshots_path"])
    first = snapshots / "first.jpg"
    second = snapshots / "second.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    old = datetime.now() - timedelta(days=120)
    first_id = insert_detection(config, detection_time=old, snapshot=first)
    second_id = insert_detection(config, detection_time=old, snapshot=second)
    real_unlink = retention_service._unlink_media_file

    def fail_first(path):
        if path == first:
            raise PermissionError("controlled unlink failure")
        return real_unlink(path)

    monkeypatch.setattr(retention_service, "_unlink_media_file", fail_first)

    result = retention_service.run_retention("test", config=config)

    assert result.outcome == "partial"
    assert first.exists()
    assert media_values(config, first_id)[0] == str(first)
    assert not second.exists()
    assert media_values(config, second_id)[0] is None


def test_orphan_scan_reports_missing_and_isolates_deletion_failure(
    tmp_path, monkeypatch
):
    from app import retention_service

    config = retention_config(
        tmp_path,
        enabled=False,
        delete_orphaned_media=True,
    )
    snapshots = Path(config["media"]["snapshots_path"])
    failed = snapshots / "failed.jpg"
    deleted = snapshots / "deleted.jpg"
    missing = snapshots / "missing.jpg"
    failed.write_bytes(b"failed")
    deleted.write_bytes(b"deleted")
    insert_detection(config, detection_time=datetime.now(), snapshot=missing)
    real_unlink = retention_service._unlink_media_file

    def fail_one(path):
        if path == failed:
            raise PermissionError("controlled orphan failure")
        return real_unlink(path)

    monkeypatch.setattr(retention_service, "_unlink_media_file", fail_one)

    result = retention_service.run_retention("test", config=config)

    assert result.outcome == "partial"
    assert result.phases["expired_media"].outcome == "skipped"
    assert result.phases["system_events"].outcome == "skipped"
    assert result.orphan_count == 2
    assert result.orphan_deletion_count == 1
    assert result.missing_reference_count == 1
    assert failed.exists()
    assert not deleted.exists()

    conn = sqlite3.connect(config["storage"]["database_path"])
    assert conn.execute("SELECT COUNT(*) FROM retention_status").fetchone()[0] == 0
    conn.close()


def test_orphan_deletion_rejects_symlink_resolving_outside_archive(tmp_path):
    from app.retention_service import run_retention

    config = retention_config(
        tmp_path,
        enabled=False,
        delete_orphaned_media=True,
    )
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"must survive")
    symlink = Path(config["media"]["snapshots_path"]) / "outside-link.jpg"
    symlink.symlink_to(outside)

    result = run_retention("test", config=config)

    assert result.outcome == "partial"
    assert result.orphan_count == 1
    assert result.orphan_deletion_count == 0
    assert symlink.is_symlink()
    assert outside.read_bytes() == b"must survive"


def test_delete_orphans_has_no_effect_when_scan_is_disabled(tmp_path):
    from app.retention_service import run_retention

    config = retention_config(
        tmp_path,
        enabled=False,
        orphan_scan_enabled=False,
        delete_orphaned_media=True,
    )
    orphan = Path(config["media"]["snapshots_path"]) / "orphan.jpg"
    orphan.write_bytes(b"orphan")

    result = run_retention("test", config=config)

    assert result.outcome == "success"
    assert result.phases["orphan_scan"].outcome == "skipped"
    assert result.orphan_count == 0
    assert orphan.exists()


def test_unexpected_phase_failure_is_recorded_and_later_phases_run(
    tmp_path, monkeypatch
):
    from app import retention_service

    config = retention_config(tmp_path)
    calls = []

    def fail_expired(*args, **kwargs):
        calls.append("expired")
        raise RuntimeError("controlled phase failure")

    def run_orphans(*args, **kwargs):
        calls.append("orphans")

    def run_events(*args, **kwargs):
        calls.append("events")

    monkeypatch.setattr(retention_service, "_run_expired_media", fail_expired)
    monkeypatch.setattr(retention_service, "_run_orphan_scan", run_orphans)
    monkeypatch.setattr(retention_service, "_run_system_event_pruning", run_events)

    result = retention_service.run_retention("test", config=config)

    assert calls == ["expired", "orphans", "events"]
    assert result.outcome == "partial"
    assert result.phases["expired_media"].outcome == "failed"


def test_run_loads_effective_config_once(tmp_path, monkeypatch):
    from app import retention_service

    config = retention_config(
        tmp_path,
        enabled=False,
        orphan_scan_enabled=False,
    )
    loads = []

    def load_once():
        loads.append(True)
        return config

    monkeypatch.setattr(retention_service, "load_runtime_config", load_once)

    result = retention_service.run_retention("test")

    assert result.outcome == "success"
    assert loads == [True]


def test_second_in_process_run_returns_already_running(tmp_path, monkeypatch):
    from app import retention_service

    config = retention_config(tmp_path, orphan_scan_enabled=False)
    entered = threading.Event()
    release = threading.Event()

    def blocking_phase(*args, **kwargs):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(retention_service, "_run_expired_media", blocking_phase)
    first_result = []
    thread = threading.Thread(
        target=lambda: first_result.append(
            retention_service.run_retention("first", config=config)
        )
    )
    thread.start()
    assert entered.wait(1)

    second = retention_service.run_retention("second", config=config)
    release.set()
    thread.join(2)

    assert second.outcome == "already_running"
    assert second.trigger == "second"
    assert first_result[0].outcome == "success"


def test_execution_guard_failure_returns_structured_failure(tmp_path, monkeypatch):
    from app import retention_service

    config = retention_config(tmp_path)

    @contextmanager
    def broken_guard(database_path):
        raise OSError("controlled guard failure")
        yield

    monkeypatch.setattr(
        retention_service,
        "retention_execution_guard",
        broken_guard,
    )

    result = retention_service.run_retention("test", config=config)

    assert result.outcome == "failed"
    assert result.error_count == 1
    assert {phase.outcome for phase in result.phases.values()} == {"skipped"}


def test_standalone_process_cannot_bypass_retention_execution_guard(tmp_path):
    from app.media_coordination import retention_execution_guard

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_retention_guard,
        args=(tmp_path / "wamf.db", ready, release),
    )
    process.start()
    try:
        assert ready.wait(2)
        with retention_execution_guard(tmp_path / "wamf.db") as acquired:
            assert acquired is False
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0


def test_media_coordination_guard_serializes_archive_and_scan(tmp_path):
    from app.media_coordination import media_activity_guard

    db_path = tmp_path / "wamf.db"
    first_entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first():
        with media_activity_guard(db_path):
            first_entered.set()
            assert release.wait(2)

    def second():
        assert first_entered.wait(1)
        with media_activity_guard(db_path):
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(1)
    assert not second_entered.wait(0.05)
    release.set()
    first_thread.join(2)
    second_thread.join(2)

    assert second_entered.is_set()
