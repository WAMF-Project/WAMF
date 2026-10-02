"""Dedicated-process scheduling for native automatic retention.

Importing this module has no lifecycle side effects. The native supervisor is
the sole owner of :func:`run_scheduler_child`.
"""

from copy import deepcopy
from datetime import datetime, timedelta
import logging
import os
import select
import signal
import threading
import time

from apscheduler.executors.base import run_job
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.triggers.calendarinterval import CalendarIntervalTrigger

from app.process_control import configure_worker_signals
from app.retention_schedule import parse_retention_schedule
from app.retention_service import run_retention


logger = logging.getLogger(__name__)
SCHEDULED_JOB_ID = "wamf-retention"
MISFIRE_GRACE_SECONDS = 5 * 60
SHUTDOWN_GRACE_SECONDS = 30


class ScheduledWorkTracker:
    """Track submitted APScheduler futures through their actual completion."""

    def __init__(self):
        self._condition = threading.Condition()
        self._outstanding = 0

    @property
    def outstanding(self):
        with self._condition:
            return self._outstanding

    def submitted(self):
        with self._condition:
            self._outstanding += 1
            self._condition.notify_all()

    def finished(self):
        with self._condition:
            if self._outstanding <= 0:
                raise RuntimeError("scheduled work accounting underflow")
            self._outstanding -= 1
            self._condition.notify_all()

    def wait_for_outstanding(self, minimum, timeout):
        with self._condition:
            return self._condition.wait_for(
                lambda: self._outstanding >= minimum,
                timeout=timeout,
            )

    def wait_until_idle(self, timeout):
        with self._condition:
            return self._condition.wait_for(
                lambda: self._outstanding == 0,
                timeout=max(0, timeout),
            )


class TrackingThreadPoolExecutor(ThreadPoolExecutor):
    """One-worker APScheduler executor with explicit future accounting."""

    def __init__(self, work_tracker, max_workers=1):
        self.work_tracker = work_tracker
        super().__init__(max_workers=max_workers)

    def _do_submit_job(self, job, run_times):
        def apscheduler_callback(future):
            exception, traceback = (
                future.exception_info()
                if hasattr(future, "exception_info")
                else (
                    future.exception(),
                    getattr(future.exception(), "__traceback__", None),
                )
            )
            if exception:
                self._run_job_error(job.id, exception, traceback)
            else:
                self._run_job_success(job.id, future.result())

        self.work_tracker.submitted()
        try:
            future = self._pool.submit(
                run_job,
                job,
                job._jobstore_alias,
                run_times,
                self._logger.name,
            )
        except BaseException:
            self.work_tracker.finished()
            raise
        future.add_done_callback(apscheduler_callback)
        future.add_done_callback(lambda completed: self.work_tracker.finished())


def build_scheduler_config_snapshot(config):
    """Copy only configuration used by schedule parsing and A1 retention."""

    snapshot = {}
    for key in ("config_version", "storage", "media", "retention"):
        if key in config:
            snapshot[key] = deepcopy(config[key])
    return snapshot


def _first_future_local_date(schedule, now):
    local_now = now.astimezone(schedule.timezone)
    scheduled_wall_time = schedule.daily_time
    local_wall_time = local_now.timetz().replace(tzinfo=None)
    if scheduled_wall_time > local_wall_time:
        return local_now.date()
    return local_now.date() + timedelta(days=1)


def build_daily_trigger(schedule, *, now=None):
    """Build a daily wall-clock trigger whose first occurrence is future-only."""

    current = now or datetime.now(schedule.timezone)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    start_date = _first_future_local_date(schedule, current)

    def new_trigger(date):
        return CalendarIntervalTrigger(
            days=1,
            hour=schedule.daily_time.hour,
            minute=schedule.daily_time.minute,
            second=0,
            start_date=date,
            timezone=schedule.timezone,
        )

    trigger = new_trigger(start_date)
    first_fire = trigger.get_next_fire_time(None, current)
    while first_fire is not None and first_fire.timestamp() <= current.timestamp():
        start_date = max(start_date, first_fire.date()) + timedelta(days=1)
        trigger = new_trigger(start_date)
        first_fire = trigger.get_next_fire_time(None, current)
    return trigger


def next_scheduled_occurrence(schedule, *, now=None):
    """Calculate the next future occurrence without starting a scheduler."""

    current = now or datetime.now(schedule.timezone)
    trigger = build_daily_trigger(schedule, now=current)
    return trigger.get_next_fire_time(None, current)


def run_scheduled_retention(config_snapshot, *, retention_runner=run_retention):
    """Invoke A1 once and contain all failures at the scheduled boundary."""

    try:
        result = retention_runner(trigger="scheduled", config=config_snapshot)
    except Exception:
        logger.exception("Scheduled retention raised an unexpected exception")
        return None

    if result.outcome == "success":
        logger.info(
            "Scheduled retention completed successfully (errors=%s)",
            result.error_count,
        )
    elif result.outcome == "partial":
        logger.warning(
            "Scheduled retention completed partially (errors=%s)",
            result.error_count,
        )
    elif result.outcome == "failed":
        logger.error(
            "Scheduled retention failed (errors=%s)",
            result.error_count,
        )
    elif result.outcome == "already_running":
        logger.info(
            "Scheduled retention skipped because another retention run is active"
        )
    else:
        logger.error("Scheduled retention returned unknown outcome %r", result.outcome)
    return result


def create_scheduler(
    schedule,
    config_snapshot,
    *,
    now=None,
    retention_runner=run_retention,
    work_tracker=None,
):
    """Construct the configured in-memory scheduler and its single daily job."""

    tracker = work_tracker or ScheduledWorkTracker()

    def scheduled_callback():
        return run_scheduled_retention(
            config_snapshot,
            retention_runner=retention_runner,
        )

    scheduler = BlockingScheduler(
        timezone=schedule.timezone,
        jobstores={"default": MemoryJobStore()},
        executors={"default": TrackingThreadPoolExecutor(tracker, max_workers=1)},
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": MISFIRE_GRACE_SECONDS,
        },
    )
    scheduler.add_job(
        scheduled_callback,
        trigger=build_daily_trigger(schedule, now=now),
        id=SCHEDULED_JOB_ID,
        name="WAMF daily retention",
    )
    return scheduler


def _shutdown_scheduler(scheduler):
    try:
        scheduler.shutdown(wait=False)
    except SchedulerNotRunningError:
        pass


def run_scheduler_child(
    config_snapshot,
    *,
    scheduler_factory=create_scheduler,
    shutdown_grace_seconds=SHUTDOWN_GRACE_SECONDS,
    force_exit=os._exit,
):
    """Run the blocking scheduler in its supervisor-owned child process."""

    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1)
    }
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    previous_wakeup_fd = -1
    scheduler_thread = None
    scheduler_errors = []
    shutdown_requested = False
    try:
        configure_worker_signals()
        schedule = parse_retention_schedule(config_snapshot)
        if not schedule.enabled:
            logger.info("Automatic retention scheduling is disabled")
            return

        work_tracker = ScheduledWorkTracker()
        scheduler = scheduler_factory(
            schedule,
            config_snapshot,
            work_tracker=work_tracker,
        )

        def request_shutdown(signum, frame):
            nonlocal shutdown_requested
            shutdown_requested = True

        signal.signal(signal.SIGTERM, request_shutdown)
        previous_wakeup_fd = signal.set_wakeup_fd(
            write_fd,
            warn_on_full_buffer=False,
        )
        try:
            next_run = scheduler.get_job(SCHEDULED_JOB_ID).next_run_time
        except AttributeError:
            # Pending jobs receive next_run_time when BlockingScheduler starts.
            next_run = next_scheduled_occurrence(schedule)
        logger.info(
            "Starting daily retention scheduler at %s %s (next run: %s)",
            schedule.daily_time.strftime("%H:%M"),
            schedule.timezone_name,
            next_run,
        )

        def scheduler_main():
            try:
                scheduler.start()
            except BaseException as exc:
                scheduler_errors.append(exc)
            finally:
                try:
                    os.write(write_fd, b"\0")
                except OSError:
                    pass

        scheduler_thread = threading.Thread(
            target=scheduler_main,
            name="wamf-retention-scheduler",
        )
        scheduler_thread.start()

        while scheduler_thread.is_alive() and not shutdown_requested:
            select.select([read_fd], [], [])
            try:
                os.read(read_fd, 4096)
            except BlockingIOError:
                pass

        if not shutdown_requested:
            scheduler_thread.join()
            if scheduler_errors:
                raise scheduler_errors[0]
            return

        shutdown_deadline = time.monotonic() + shutdown_grace_seconds
        logger.info("Retention scheduler received SIGTERM; stopping")
        _shutdown_scheduler(scheduler)

        remaining = shutdown_deadline - time.monotonic()
        if not work_tracker.wait_until_idle(remaining):
            logger.error(
                "Scheduled retention exceeded the %s-second shutdown grace; "
                "terminating scheduler child",
                shutdown_grace_seconds,
            )
            force_exit(0)
            return

        remaining = max(0, shutdown_deadline - time.monotonic())
        scheduler_thread.join(timeout=remaining)
        if scheduler_thread.is_alive():
            logger.error(
                "Retention scheduler thread exceeded the %s-second shutdown "
                "grace; terminating scheduler child",
                shutdown_grace_seconds,
            )
            force_exit(0)
            return
        logger.info("Retention scheduler stopped")
    finally:
        if previous_wakeup_fd != -1:
            signal.set_wakeup_fd(previous_wakeup_fd)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        os.close(read_fd)
        os.close(write_fd)
