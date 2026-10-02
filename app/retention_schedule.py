"""Shared parsing for the single native retention schedule."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_RETENTION_SCHEDULE_ENABLED = False
DEFAULT_RETENTION_SCHEDULE_TIME = "03:00"
DEFAULT_RETENTION_SCHEDULE_TIMEZONE = "UTC"
_TIME_PATTERN = re.compile(r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})")


class RetentionScheduleError(ValueError):
    """A retention schedule field is structurally invalid."""

    def __init__(self, field, message):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


@dataclass(frozen=True)
class RetentionSchedule:
    enabled: bool
    daily_time: time
    timezone_name: str
    timezone: ZoneInfo


def parse_retention_schedule(config):
    """Validate and compile the optional daily schedule from one config snapshot."""

    retention = config.get("retention", {})
    if not isinstance(retention, Mapping):
        raise RetentionScheduleError("retention", "must be a YAML mapping")

    schedule = retention.get("schedule", {})
    if not isinstance(schedule, Mapping):
        raise RetentionScheduleError(
            "retention.schedule", "must be a YAML mapping"
        )

    enabled = schedule.get(
        "enabled", DEFAULT_RETENTION_SCHEDULE_ENABLED
    )
    if not isinstance(enabled, bool):
        raise RetentionScheduleError(
            "retention.schedule.enabled", "must be a boolean"
        )

    time_value = schedule.get("time", DEFAULT_RETENTION_SCHEDULE_TIME)
    if not isinstance(time_value, str):
        raise RetentionScheduleError(
            "retention.schedule.time", "must use strict HH:MM format"
        )
    match = _TIME_PATTERN.fullmatch(time_value)
    if match is None:
        raise RetentionScheduleError(
            "retention.schedule.time", "must use strict HH:MM format"
        )
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        raise RetentionScheduleError(
            "retention.schedule.time",
            "hour must be 00-23 and minute must be 00-59",
        )

    timezone_name = schedule.get(
        "timezone", DEFAULT_RETENTION_SCHEDULE_TIMEZONE
    )
    if not isinstance(timezone_name, str) or not timezone_name:
        raise RetentionScheduleError(
            "retention.schedule.timezone",
            "must be a valid IANA timezone identifier",
        )
    try:
        timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        raise RetentionScheduleError(
            "retention.schedule.timezone",
            "must be a valid IANA timezone identifier",
        ) from None

    return RetentionSchedule(
        enabled=enabled,
        daily_time=time(hour, minute),
        timezone_name=timezone_name,
        timezone=timezone,
    )
