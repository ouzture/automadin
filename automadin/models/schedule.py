from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Any


class SchedulePattern(Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    INTERVAL = "interval"


class MissedRunPolicy(Enum):
    SKIP = "skip"
    RUN_ONCE = "run_once"


class OverlapPolicy(Enum):
    SKIP = "skip"
    QUEUE_ONE = "queue_one"


class RunHistoryOutcome(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    REQUIRES_ATTENTION = "requires_attention"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


def _parse_local_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("start_at_local must be an ISO local date and time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("start_at_local must be an ISO local date and time") from exc
    if parsed.tzinfo is not None:
        raise ValueError("start_at_local must not contain a time zone")
    return parsed


def _parse_local_time(value: object) -> time | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("local_time must use HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("local_time must use HH:MM") from exc
    if parsed.tzinfo is not None:
        raise ValueError("local_time must not contain a time zone")
    return parsed


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """Versioned, local-wall-clock recurrence specification."""

    pattern: SchedulePattern
    local_time: time | None = None
    weekdays: tuple[int, ...] = ()
    start_at_local: datetime | None = None
    interval_minutes: int | None = None
    missed_policy: MissedRunPolicy = MissedRunPolicy.SKIP
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP

    def __post_init__(self) -> None:
        weekdays = tuple(sorted(set(self.weekdays)))
        object.__setattr__(self, "weekdays", weekdays)
        if any(day < 0 or day > 6 for day in weekdays):
            raise ValueError("weekdays must contain values from 0 through 6")
        if self.local_time is not None and (
            self.local_time.tzinfo is not None
            or self.local_time.second
            or self.local_time.microsecond
        ):
            raise ValueError("local_time must be a naive whole minute")
        if self.start_at_local is not None and (
            self.start_at_local.tzinfo is not None
            or self.start_at_local.second
            or self.start_at_local.microsecond
        ):
            raise ValueError("start_at_local must be a naive whole minute")

        if self.pattern in (SchedulePattern.DAILY, SchedulePattern.WEEKLY):
            if self.local_time is None:
                raise ValueError(f"{self.pattern.value} schedules require local_time")
        if self.pattern is SchedulePattern.WEEKLY and not weekdays:
            raise ValueError("weekly schedules require at least one entry in weekdays")
        if self.pattern in (SchedulePattern.ONCE, SchedulePattern.INTERVAL):
            if self.start_at_local is None:
                raise ValueError(
                    f"{self.pattern.value} schedules require start_at_local"
                )
        if self.pattern is SchedulePattern.INTERVAL and (
            not isinstance(self.interval_minutes, int)
            or isinstance(self.interval_minutes, bool)
            or self.interval_minutes < 1
        ):
            raise ValueError(
                "interval schedules require interval_minutes greater than zero"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "pattern": self.pattern.value,
            "local_time": (
                self.local_time.isoformat(timespec="minutes")
                if self.local_time is not None
                else None
            ),
            "weekdays": list(self.weekdays),
            "start_at_local": (
                self.start_at_local.isoformat(timespec="minutes")
                if self.start_at_local is not None
                else None
            ),
            "interval_minutes": self.interval_minutes,
            "missed_policy": self.missed_policy.value,
            "overlap_policy": self.overlap_policy.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleSpec:
        if data.get("version", 1) != 1:
            raise ValueError("Unsupported schedule specification version")
        weekdays = data.get("weekdays", ())
        if not isinstance(weekdays, (list, tuple)):
            raise ValueError("weekdays must be a list")
        try:
            return cls(
                pattern=SchedulePattern(data["pattern"]),
                local_time=_parse_local_time(data.get("local_time")),
                weekdays=tuple(weekdays),
                start_at_local=_parse_local_datetime(data.get("start_at_local")),
                interval_minutes=data.get("interval_minutes"),
                missed_policy=MissedRunPolicy(
                    data.get("missed_policy", MissedRunPolicy.SKIP.value)
                ),
                overlap_policy=OverlapPolicy(
                    data.get("overlap_policy", OverlapPolicy.SKIP.value)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid schedule specification: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ScheduledFlow:
    schedule_id: str
    name: str
    run_config_id: int
    spec: ScheduleSpec
    enabled: bool
    next_due_at_utc: datetime | None
    last_due_at_utc: datetime | None
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class RunHistoryRecord:
    run_id: str
    source: str
    schedule_id: str | None
    schedule_name: str
    run_config_id: int | None
    run_config_name: str
    flow_name: str
    scheduled_for_utc: datetime
    started_at_utc: datetime | None
    finished_at_utc: datetime | None
    outcome: RunHistoryOutcome
    message: str
