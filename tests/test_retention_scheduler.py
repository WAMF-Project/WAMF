from datetime import datetime, timedelta
import importlib
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import yaml
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.blocking import BlockingScheduler

from app.config_validation import validate_config
from app.db import ensure_schema
from app.retention_schedule import (
    DEFAULT_RETENTION_SCHEDULE_TIME,
    DEFAULT_RETENTION_SCHEDULE_TIMEZONE,
    RetentionScheduleError,
    parse_retention_schedule,
)
from app import retention_scheduler


def schedule_config(*, enabled=True, time="03:00", timezone="UTC"):
    return {
        "retention": {
            "schedule": {
                "enabled": enabled,
                "time": time,
                "timezone": timezone,
            }
        }
    }


def test_absent_schedule_uses_disabled_utc_defaults():
    schedule = parse_retention_schedule({})

    assert schedule.enabled is False
    assert schedule.daily_time.strftime("%H:%M") == DEFAULT_RETENTION_SCHEDULE_TIME
    assert schedule.timezone_name == DEFAULT_RETENTION_SCHEDULE_TIMEZONE
    assert schedule.timezone == ZoneInfo("UTC")


@pytest.mark.parametrize("enabled", [True, False])
def test_schedule_accepts_real_booleans(enabled):
    assert parse_retention_schedule(
        schedule_config(enabled=enabled)
    ).enabled is enabled


@pytest.mark.parametrize("enabled", [0, 1, "true", None, []])
def test_schedule_rejects_non_boolean_enabled(enabled):
    with pytest.raises(
        RetentionScheduleError,
        match="retention.schedule.enabled",
    ):
        parse_retention_schedule(schedule_config(enabled=enabled))


@pytest.mark.parametrize("value", ["00:00", "03:07", "23:59"])
def test_schedule_accepts_strict_valid_times(value):
    assert parse_retention_schedule(
        schedule_config(time=value)
    ).daily_time.strftime("%H:%M") == value


@pytest.mark.parametrize(
    "value",
    ["3:00", "03:0", "03:00:00", " 03:00", "24:00", "23:60", 300, None],
)
def test_schedule_rejects_invalid_time_forms(value):
    with pytest.raises(
        RetentionScheduleError,
        match="retention.schedule.time",
    ):
        parse_retention_schedule(schedule_config(time=value))


@pytest.mark.parametrize("timezone", ["UTC", "Europe/London", "America/New_York"])
def test_schedule_resolves_valid_iana_timezones(timezone):
    schedule = parse_retention_schedule(schedule_config(timezone=timezone))
    assert schedule.timezone == ZoneInfo(timezone)


@pytest.mark.parametrize("timezone", ["Not/A_Zone", "", None, 1])
def test_schedule_rejects_invalid_timezone(timezone):
    with pytest.raises(
        RetentionScheduleError,
        match="retention.schedule.timezone",
    ):
        parse_retention_schedule(schedule_config(timezone=timezone))


def test_validation_rejects_non_mapping_schedule():
    result = validate_config({"retention": {"schedule": "daily"}})
    assert [issue.field for issue in result.errors] == ["retention.schedule"]


def test_retention_policy_enabled_is_independent_of_schedule_enabled():
    from app.retention_service import build_retention_policy

    config = schedule_config(enabled=True)
    config["retention"]["enabled"] = False
    assert build_retention_policy(config).enabled is False
    assert parse_retention_schedule(config).enabled is True

    config["retention"]["enabled"] = True
    config["retention"]["schedule"]["enabled"] = False
    assert build_retention_policy(config).enabled is True
    assert parse_retention_schedule(config).enabled is False


def test_next_occurrence_before_todays_time_uses_today():
    schedule = parse_retention_schedule(schedule_config(time="15:30"))
    now = datetime(2026, 2, 10, 12, 0, tzinfo=ZoneInfo("UTC"))

    assert retention_scheduler.next_scheduled_occurrence(
        schedule, now=now
    ) == datetime(2026, 2, 10, 15, 30, tzinfo=ZoneInfo("UTC"))


def test_next_occurrence_after_todays_time_waits_until_tomorrow():
    schedule = parse_retention_schedule(schedule_config(time="03:00"))
    now = datetime(2026, 2, 10, 5, 0, tzinfo=ZoneInfo("UTC"))

    assert retention_scheduler.next_scheduled_occurrence(
        schedule, now=now
    ) == datetime(2026, 2, 11, 3, 0, tzinfo=ZoneInfo("UTC"))


def test_startup_just_after_schedule_does_not_use_misfire_grace_as_catchup():
    schedule = parse_retention_schedule(schedule_config(time="03:00"))
    now = datetime(2026, 2, 10, 3, 2, tzinfo=ZoneInfo("UTC"))

    assert retention_scheduler.next_scheduled_occurrence(
        schedule, now=now
    ) == datetime(2026, 2, 11, 3, 0, tzinfo=ZoneInfo("UTC"))


def test_calendar_trigger_skips_nonexistent_spring_forward_time():
    schedule = parse_retention_schedule(
        schedule_config(time="01:30", timezone="Europe/London")
    )
    now = datetime(2026, 3, 28, 2, 0, tzinfo=ZoneInfo("Europe/London"))

    assert retention_scheduler.next_scheduled_occurrence(
        schedule, now=now
    ) == datetime(2026, 3, 30, 1, 30, tzinfo=ZoneInfo("Europe/London"))


def test_calendar_trigger_uses_earlier_fold_once_on_fall_back_day():
    schedule = parse_retention_schedule(
        schedule_config(time="01:30", timezone="Europe/London")
    )
    now = datetime(2026, 10, 24, 2, 0, tzinfo=ZoneInfo("Europe/London"))
    trigger = retention_scheduler.build_daily_trigger(schedule, now=now)

    first = trigger.get_next_fire_time(None, now)
    second = trigger.get_next_fire_time(first, first)

    assert first == datetime(
        2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0
    )
    assert first.utcoffset().total_seconds() == 3600
    assert second.date().isoformat() == "2026-10-26"


def test_startup_during_london_second_fold_selects_strictly_future_day():
    schedule = parse_retention_schedule(
        schedule_config(time="01:30", timezone="Europe/London")
    )
    now = datetime(
        2026,
        10,
        25,
        1,
        15,
        tzinfo=ZoneInfo("Europe/London"),
        fold=1,
    )

    next_run = retention_scheduler.next_scheduled_occurrence(schedule, now=now)

    assert next_run == datetime(
        2026, 10, 26, 1, 30, tzinfo=ZoneInfo("Europe/London")
    )
    assert next_run.timestamp() > now.timestamp()


def test_startup_during_lord_howe_second_fold_cannot_catch_up_past_occurrence():
    schedule = parse_retention_schedule(
        schedule_config(time="01:59", timezone="Australia/Lord_Howe")
    )
    now = datetime(
        2026,
        4,
        5,
        1,
        31,
        tzinfo=ZoneInfo("Australia/Lord_Howe"),
        fold=1,
    )

    next_run = retention_scheduler.next_scheduled_occurrence(schedule, now=now)

    assert next_run == datetime(
        2026, 4, 6, 1, 59, tzinfo=ZoneInfo("Australia/Lord_Howe")
    )
    assert next_run.timestamp() > now.timestamp()
    assert next_run.date() > now.date()


def test_next_occurrence_uses_configured_timezone_not_host_timezone():
    schedule = parse_retention_schedule(
        schedule_config(time="03:00", timezone="America/New_York")
    )
    now = datetime(2026, 1, 1, 4, 0, tzinfo=ZoneInfo("UTC"))

    next_run = retention_scheduler.next_scheduled_occurrence(schedule, now=now)

    assert next_run == datetime(
        2026, 1, 1, 3, 0, tzinfo=ZoneInfo("America/New_York")
    )
    assert next_run.astimezone(ZoneInfo("UTC")).hour == 8


def test_scheduler_construction_is_in_memory_single_worker_single_daily_job():
    schedule = parse_retention_schedule(schedule_config())
    now = datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))

    scheduler = retention_scheduler.create_scheduler(
        schedule,
        schedule_config(),
        now=now,
        retention_runner=MagicMock(),
    )

    assert list(scheduler._jobstores) == ["default"]
    assert scheduler._jobstores["default"].__class__.__name__ == "MemoryJobStore"
    assert scheduler._executors["default"]._pool._max_workers == 1
    assert scheduler._job_defaults == {
        "misfire_grace_time": retention_scheduler.MISFIRE_GRACE_SECONDS,
        "coalesce": True,
        "max_instances": 1,
    }
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == retention_scheduler.SCHEDULED_JOB_ID
    assert jobs[0].trigger.days == 1


@pytest.mark.parametrize(
    ("outcome", "level"),
    [
        ("success", logging.INFO),
        ("partial", logging.WARNING),
        ("failed", logging.ERROR),
        ("already_running", logging.INFO),
    ],
)
def test_scheduled_callback_calls_a1_once_and_logs_structured_outcome(
    caplog, outcome, level
):
    runner = MagicMock(
        return_value=SimpleNamespace(outcome=outcome, error_count=1)
    )
    config = schedule_config()

    with caplog.at_level(level, logger="app.retention_scheduler"):
        result = retention_scheduler.run_scheduled_retention(
            config, retention_runner=runner
        )

    runner.assert_called_once_with(trigger="scheduled", config=config)
    assert result.outcome == outcome
    assert any(record.levelno == level for record in caplog.records)


def test_scheduled_callback_contains_unexpected_exception_without_retry(caplog):
    runner = MagicMock(side_effect=RuntimeError("controlled"))

    with caplog.at_level(logging.ERROR, logger="app.retention_scheduler"):
        result = retention_scheduler.run_scheduled_retention(
            schedule_config(), retention_runner=runner
        )

    assert result is None
    runner.assert_called_once()
    assert "unexpected exception" in caplog.text


def test_snapshot_is_deep_copied_minimal_and_secret_free():
    config = {
        "config_version": 2,
        "storage": {"database_path": "/data/wamf.db"},
        "media": {"snapshots_path": "/media/snapshots"},
        "retention": {"schedule": {"enabled": True}},
        "mqtt": {"password": "sentinel"},
        "admin": {"session_secret": "sentinel"},
    }

    snapshot = retention_scheduler.build_scheduler_config_snapshot(config)
    config["retention"]["schedule"]["enabled"] = False

    assert set(snapshot) == {"config_version", "storage", "media", "retention"}
    assert snapshot["retention"]["schedule"]["enabled"] is True
    assert "sentinel" not in repr(snapshot)


def test_scheduler_module_import_has_no_lifecycle_side_effects(monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(
        "apscheduler.schedulers.blocking.BlockingScheduler.start", start
    )

    importlib.reload(retention_scheduler)

    start.assert_not_called()


def test_web_import_does_not_start_scheduler(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "config_version": 2,
                "admin": {"auth_enabled": False},
                "storage": {
                    "database_path": str(tmp_path / "data" / "wamf.db")
                },
                "media": {
                    "snapshots_path": str(tmp_path / "snapshots"),
                    "clips_path": str(tmp_path / "clips"),
                },
            }
        ),
        encoding="utf-8",
    )
    script = """
from unittest.mock import patch
with patch('apscheduler.schedulers.blocking.BlockingScheduler.start') as start:
    import webui
    assert not start.called
"""
    env = dict(os.environ, WHOSATMYFEEDER_CONFIG=str(config_path))

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr


def test_disabled_child_returns_without_constructing_scheduler(monkeypatch):
    monkeypatch.setattr(retention_scheduler, "configure_worker_signals", MagicMock())
    factory = MagicMock()

    retention_scheduler.run_scheduler_child(
        schedule_config(enabled=False), scheduler_factory=factory
    )

    factory.assert_not_called()


def test_child_shutdown_is_prompt_when_idle(monkeypatch):
    monkeypatch.setattr(retention_scheduler, "configure_worker_signals", MagicMock())
    handlers = {}
    monkeypatch.setattr(
        retention_scheduler.signal,
        "signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    scheduler = MagicMock()
    scheduler.get_job.return_value = SimpleNamespace(next_run_time="future")
    def request_shutdown_from_scheduler_thread():
        handlers[retention_scheduler.signal.SIGTERM](
            retention_scheduler.signal.SIGTERM, None
        )
        scheduler.shutdown.assert_not_called()

    scheduler.start.side_effect = request_shutdown_from_scheduler_thread
    factory = MagicMock(return_value=scheduler)
    force_exit = MagicMock()

    retention_scheduler.run_scheduler_child(
        schedule_config(),
        scheduler_factory=factory,
        shutdown_grace_seconds=0,
        force_exit=force_exit,
    )

    scheduler.shutdown.assert_called_once_with(wait=False)
    force_exit.assert_not_called()


def test_child_shutdown_force_exits_after_bounded_grace_when_active(monkeypatch):
    monkeypatch.setattr(retention_scheduler, "configure_worker_signals", MagicMock())
    handlers = {}
    monkeypatch.setattr(
        retention_scheduler.signal,
        "signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    scheduler = MagicMock()
    scheduler.get_job.return_value = SimpleNamespace(next_run_time="future")

    def factory(schedule, config_snapshot, *, work_tracker):
        work_tracker.submitted()
        scheduler.start.side_effect = lambda: (
            handlers[retention_scheduler.signal.SIGTERM](
                retention_scheduler.signal.SIGTERM, None
            ),
        )
        return scheduler

    force_exit = MagicMock()

    retention_scheduler.run_scheduler_child(
        schedule_config(),
        scheduler_factory=factory,
        shutdown_grace_seconds=0,
        force_exit=force_exit,
    )

    scheduler.shutdown.assert_called_once_with(wait=False)
    force_exit.assert_called_once_with(0)


def _real_test_scheduler(work_tracker, jobs, shutdown_called):
    scheduler = BlockingScheduler(
        timezone=ZoneInfo("UTC"),
        jobstores={"default": MemoryJobStore()},
        executors={
            "default": retention_scheduler.TrackingThreadPoolExecutor(
                work_tracker,
                max_workers=1,
            )
        },
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": retention_scheduler.MISFIRE_GRACE_SECONDS,
        },
    )
    base_time = datetime.now(ZoneInfo("UTC"))
    for index, (job, delay) in enumerate(jobs):
        scheduler.add_job(
            job,
            "date",
            run_date=base_time + timedelta(seconds=delay),
            id=(
                retention_scheduler.SCHEDULED_JOB_ID
                if index == 0
                else f"test-job-{index}"
            ),
        )
    real_shutdown = scheduler.shutdown

    def observed_shutdown(*, wait=True):
        shutdown_called.set()
        return real_shutdown(wait=wait)

    scheduler.shutdown = observed_shutdown
    return scheduler


def test_real_executor_active_work_finishes_within_shutdown_grace():
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    shutdown_called = threading.Event()
    future_job_ran = threading.Event()
    scheduler_holder = {}

    def active_job():
        started.set()
        assert release.wait(2)
        completed.set()

    def factory(schedule, config_snapshot, *, work_tracker):
        scheduler = _real_test_scheduler(
            work_tracker,
            [(active_job, 0.02), (future_job_ran.set, 10)],
            shutdown_called,
        )
        scheduler_holder["scheduler"] = scheduler
        return scheduler

    def request_shutdown():
        assert started.wait(2)
        os.kill(os.getpid(), retention_scheduler.signal.SIGTERM)
        assert shutdown_called.wait(2)
        release.set()

    requester = threading.Thread(target=request_shutdown)
    requester.start()
    retention_scheduler.run_scheduler_child(
        schedule_config(),
        scheduler_factory=factory,
        shutdown_grace_seconds=1,
    )
    requester.join(2)

    assert not requester.is_alive()
    assert completed.is_set()
    assert not scheduler_holder["scheduler"].running
    assert not future_job_ran.is_set()


def test_real_executor_blocked_work_uses_bounded_force_exit_path():
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    shutdown_called = threading.Event()
    forced = []

    class ForcedExit(Exception):
        pass

    def blocked_job():
        started.set()
        release.wait(2)
        completed.set()

    def factory(schedule, config_snapshot, *, work_tracker):
        return _real_test_scheduler(
            work_tracker,
            [(blocked_job, 0.02)],
            shutdown_called,
        )

    def request_shutdown():
        assert started.wait(2)
        os.kill(os.getpid(), retention_scheduler.signal.SIGTERM)

    def force_exit(code):
        forced.append(code)
        raise ForcedExit

    requester = threading.Thread(target=request_shutdown)
    requester.start()
    try:
        with pytest.raises(ForcedExit):
            retention_scheduler.run_scheduler_child(
                schedule_config(),
                scheduler_factory=factory,
                shutdown_grace_seconds=0,
                force_exit=force_exit,
            )
    finally:
        release.set()
        requester.join(2)

    assert shutdown_called.is_set()
    assert forced == [0]
    assert completed.wait(2)


def test_real_executor_tracks_submitted_callback_before_entry():
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    queued_entered = threading.Event()
    shutdown_called = threading.Event()
    forced_while_queued = threading.Event()
    tracker_holder = {}

    class ForcedExit(Exception):
        pass

    def blocker():
        blocker_started.set()
        release_blocker.wait(2)

    def queued_job():
        queued_entered.set()

    def factory(schedule, config_snapshot, *, work_tracker):
        tracker_holder["tracker"] = work_tracker
        return _real_test_scheduler(
            work_tracker,
            [(blocker, 0.02), (queued_job, 0.03)],
            shutdown_called,
        )

    def request_shutdown():
        assert blocker_started.wait(2)
        assert tracker_holder["tracker"].wait_for_outstanding(2, timeout=2)
        assert not queued_entered.is_set()
        os.kill(os.getpid(), retention_scheduler.signal.SIGTERM)

    def force_exit(code):
        assert code == 0
        assert not queued_entered.is_set()
        forced_while_queued.set()
        raise ForcedExit

    requester = threading.Thread(target=request_shutdown)
    requester.start()
    try:
        with pytest.raises(ForcedExit):
            retention_scheduler.run_scheduler_child(
                schedule_config(),
                scheduler_factory=factory,
                shutdown_grace_seconds=0,
                force_exit=force_exit,
            )
    finally:
        release_blocker.set()
        requester.join(2)

    assert shutdown_called.is_set()
    assert forced_while_queued.is_set()
    assert queued_entered.wait(2)


def _native_config(schedule_enabled):
    return {
        "config_version": 2,
        "frigate": {
            "frigate_url": "http://frigate:5000",
            "camera": ["birdcam"],
        },
        "mqtt": {"host": "mqtt", "topic_prefix": "frigate"},
        "classification": {"model": "model.tflite", "threshold": 0.7},
        "retention": {
            "schedule": {
                "enabled": schedule_enabled,
                "time": "03:00",
                "timezone": "UTC",
            }
        },
    }


@pytest.mark.parametrize(
    ("enabled", "expected_processes"), [(False, 2), (True, 3)]
)
def test_native_supervisor_starts_exactly_one_scheduler_only_when_enabled(
    monkeypatch, enabled, expected_processes
):
    import speciesid

    monkeypatch.setattr(speciesid, "config", _native_config(enabled))
    monkeypatch.setattr(speciesid, "load_config", MagicMock())
    monkeypatch.setattr(speciesid, "setupdb", MagicMock())
    monkeypatch.setattr(speciesid, "log_system_event", MagicMock())
    flask = MagicMock(pid=101)
    flask.is_alive.side_effect = [True, False, False]
    mqtt = MagicMock(pid=102)
    mqtt.is_alive.return_value = True
    processes = [flask, mqtt]
    scheduler = None
    if enabled:
        scheduler = MagicMock(pid=103)
        scheduler.is_alive.side_effect = [True, False]
        processes.append(scheduler)
    factory = MagicMock(side_effect=processes)
    monkeypatch.setattr(speciesid.multiprocessing, "Process", factory)

    speciesid.main()

    assert factory.call_count == expected_processes
    scheduler_targets = [
        call.kwargs.get("target")
        for call in factory.call_args_list
        if call.kwargs.get("target") is speciesid.run_scheduler_child
    ]
    assert len(scheduler_targets) == int(enabled)
    if scheduler is not None:
        scheduler.terminate.assert_called_once()
        scheduler.join.assert_called_once_with(
            timeout=speciesid.SCHEDULER_PARENT_SHUTDOWN_GRACE_SECONDS
        )


def test_scheduler_starts_even_when_mqtt_frigate_readiness_disables_detector(
    monkeypatch,
):
    import speciesid

    monkeypatch.setattr(speciesid, "config", _native_config(True))
    monkeypatch.setattr(speciesid, "load_config", MagicMock())
    monkeypatch.setattr(speciesid, "setupdb", MagicMock())
    monkeypatch.setattr(speciesid, "preflight", lambda config: ["mqtt.host"])
    monkeypatch.setattr(speciesid, "log_system_event", MagicMock())
    flask = MagicMock(pid=101)
    flask.is_alive.side_effect = [True, False, False]
    scheduler = MagicMock(pid=103)
    scheduler.is_alive.side_effect = [True, False]
    factory = MagicMock(side_effect=[flask, scheduler])
    monkeypatch.setattr(speciesid.multiprocessing, "Process", factory)

    speciesid.main()

    targets = [call.kwargs["target"] for call in factory.call_args_list]
    assert targets == [speciesid.run_webui, speciesid.run_scheduler_child]


def test_scheduler_unexpected_exit_uses_capped_backoff_without_rapid_respawn():
    import speciesid

    dead = MagicMock(exitcode=7)
    dead.is_alive.return_value = False
    replacement = MagicMock()
    workers = MagicMock()
    workers.start.return_value = replacement
    state = speciesid.SchedulerWorkerState(
        config_snapshot={"retention": {"schedule": {"enabled": True}}},
        process=dead,
        started_at=90.0,
    )

    assert speciesid._monitor_scheduler_worker(
        workers, state, monotonic=lambda: 100.0
    ) is None
    workers.reap.assert_called_once_with(dead)
    assert state.restart_at == 105.0
    assert state.restart_delay == 10.0

    assert speciesid._monitor_scheduler_worker(
        workers, state, monotonic=lambda: 104.9
    ) is None
    workers.start.assert_not_called()

    assert speciesid._monitor_scheduler_worker(
        workers, state, monotonic=lambda: 105.0
    ) is replacement
    workers.start.assert_called_once()


def test_scheduler_restart_delay_is_bounded_and_resets_after_stable_run():
    import speciesid

    dead = MagicMock(exitcode=9)
    dead.is_alive.return_value = False
    capped = speciesid.SchedulerWorkerState(
        config_snapshot={},
        process=dead,
        started_at=0.0,
        restart_delay=speciesid.SCHEDULER_RESTART_MAX_SECONDS,
    )
    speciesid._monitor_scheduler_worker(
        MagicMock(), capped, monotonic=lambda: 10.0
    )
    assert capped.restart_at == 10.0 + speciesid.SCHEDULER_RESTART_MAX_SECONDS
    assert capped.restart_delay == speciesid.SCHEDULER_RESTART_MAX_SECONDS

    alive = MagicMock()
    alive.is_alive.return_value = True
    state = speciesid.SchedulerWorkerState(
        config_snapshot={},
        process=alive,
        started_at=0.0,
        restart_delay=speciesid.SCHEDULER_RESTART_MAX_SECONDS,
    )

    speciesid._monitor_scheduler_worker(
        MagicMock(),
        state,
        monotonic=lambda: speciesid.SCHEDULER_RESTART_STABLE_SECONDS,
    )

    assert state.restart_delay == speciesid.SCHEDULER_RESTART_INITIAL_SECONDS


def _integration_config(tmp_path):
    snapshots = tmp_path / "snapshots"
    clips = tmp_path / "clips"
    snapshots.mkdir()
    clips.mkdir()
    config = {
        "config_version": 2,
        "storage": {"database_path": str(tmp_path / "wamf.db")},
        "media": {
            "snapshots_path": str(snapshots),
            "clips_path": str(clips),
        },
        "retention": {
            "enabled": False,
            "snapshots_days": 90,
            "clips_days": 90,
            "delete_media": False,
            "orphan_scan_enabled": False,
            "delete_orphaned_media": False,
            "system_events_days": 90,
            "system_events_min_rows": 0,
            "species_overrides": {},
            "schedule": {
                "enabled": True,
                "time": "03:00",
                "timezone": "UTC",
            },
        },
    }
    ensure_schema(config["storage"]["database_path"])
    return config


def test_scheduled_callback_runs_real_a1_a2_with_one_explicit_snapshot(
    tmp_path, monkeypatch
):
    from app import retention_service

    config = _integration_config(tmp_path)
    snapshot = retention_scheduler.build_scheduler_config_snapshot(config)
    monkeypatch.setattr(
        retention_service,
        "load_runtime_config",
        MagicMock(side_effect=AssertionError("must not reload configuration")),
    )

    result = retention_scheduler.run_scheduled_retention(
        snapshot,
        retention_runner=retention_service.run_retention,
    )

    assert result.outcome == "success"
    conn = sqlite3.connect(config["storage"]["database_path"])
    state = conn.execute(
        "SELECT last_attempt_trigger, last_attempt_outcome "
        "FROM retention_status WHERE id = 1"
    ).fetchone()
    conn.close()
    assert state == ("scheduled", "success")
