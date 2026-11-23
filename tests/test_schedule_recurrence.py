from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from automadin.models.schedule import (
    MissedRunPolicy,
    OverlapPolicy,
    SchedulePattern,
    ScheduleSpec,
)
from automadin.services.schedule_recurrence import next_occurrence


def test_schedule_spec_round_trip() -> None:
    spec = ScheduleSpec(
        pattern=SchedulePattern.WEEKLY,
        local_time=time(8, 30),
        weekdays=(4, 0, 4),
        missed_policy=MissedRunPolicy.RUN_ONCE,
        overlap_policy=OverlapPolicy.QUEUE_ONE,
    )

    restored = ScheduleSpec.from_dict(spec.to_dict())

    assert restored == ScheduleSpec(
        pattern=SchedulePattern.WEEKLY,
        local_time=time(8, 30),
        weekdays=(0, 4),
        missed_policy=MissedRunPolicy.RUN_ONCE,
        overlap_policy=OverlapPolicy.QUEUE_ONE,
    )


@pytest.mark.parametrize(
    "spec",
    [
        ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 15)),
        ScheduleSpec(
            pattern=SchedulePattern.WEEKLY,
            local_time=time(9, 15),
            weekdays=(0, 2, 6),
        ),
        ScheduleSpec(
            pattern=SchedulePattern.INTERVAL,
            start_at_local=datetime(2026, 7, 24, 9, 0),
            interval_minutes=90,
        ),
        ScheduleSpec(
            pattern=SchedulePattern.ONCE,
            start_at_local=datetime(2026, 7, 24, 9, 0),
        ),
    ],
)
def test_schedule_spec_accepts_each_supported_pattern(spec: ScheduleSpec) -> None:
    assert ScheduleSpec.from_dict(spec.to_dict()) == spec


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"pattern": SchedulePattern.DAILY}, "local_time"),
        (
            {"pattern": SchedulePattern.WEEKLY, "local_time": time(9, 0)},
            "weekdays",
        ),
        (
            {
                "pattern": SchedulePattern.INTERVAL,
                "start_at_local": datetime(2026, 7, 24, 9, 0),
                "interval_minutes": 0,
            },
            "interval_minutes",
        ),
        (
            {
                "pattern": SchedulePattern.ONCE,
                "start_at_local": datetime(2026, 7, 24, 9, 0, 1),
            },
            "whole minute",
        ),
    ],
)
def test_schedule_spec_rejects_invalid_patterns(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ScheduleSpec(**kwargs)


def test_daily_next_occurrence_uses_local_wall_clock() -> None:
    spec = ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0))

    result = next_occurrence(
        spec,
        after_utc=datetime(2026, 7, 24, 9, 30, tzinfo=UTC),
        local_timezone=UTC,
    )

    assert result == datetime(2026, 7, 25, 9, 0, tzinfo=UTC)


def test_weekly_next_occurrence_selects_next_enabled_day() -> None:
    spec = ScheduleSpec(
        pattern=SchedulePattern.WEEKLY,
        local_time=time(10, 0),
        weekdays=(0, 4),
    )

    result = next_occurrence(
        spec,
        after_utc=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        local_timezone=UTC,
    )

    assert result == datetime(2026, 7, 24, 10, 0, tzinfo=UTC)


def test_interval_is_anchored_and_does_not_drift() -> None:
    spec = ScheduleSpec(
        pattern=SchedulePattern.INTERVAL,
        start_at_local=datetime(2026, 7, 24, 9, 0),
        interval_minutes=30,
    )

    result = next_occurrence(
        spec,
        after_utc=datetime(2026, 7, 24, 10, 7, tzinfo=UTC),
        local_timezone=UTC,
    )

    assert result == datetime(2026, 7, 24, 10, 30, tzinfo=UTC)


def test_once_has_no_occurrence_after_its_due_time() -> None:
    spec = ScheduleSpec(
        pattern=SchedulePattern.ONCE,
        start_at_local=datetime(2026, 7, 24, 9, 0),
    )

    assert (
        next_occurrence(
            spec,
            after_utc=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
            local_timezone=UTC,
        )
        is None
    )


def test_after_utc_must_be_timezone_aware() -> None:
    spec = ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0))

    with pytest.raises(ValueError, match="timezone-aware"):
        next_occurrence(
            spec,
            after_utc=datetime(2026, 7, 24, 8, 0),
            local_timezone=UTC,
        )


def _london_timezone() -> ZoneInfo:
    try:
        return ZoneInfo("Europe/London")
    except ZoneInfoNotFoundError:
        pytest.skip("IANA time-zone data is unavailable in this environment")


def test_nonexistent_dst_time_advances_to_first_valid_minute() -> None:
    london = _london_timezone()
    spec = ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(1, 30))

    result = next_occurrence(
        spec,
        after_utc=datetime(2026, 3, 28, 23, 0, tzinfo=UTC),
        local_timezone=london,
    )

    assert result == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)


def test_ambiguous_dst_time_uses_first_occurrence_only() -> None:
    london = _london_timezone()
    spec = ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(1, 30))

    first = next_occurrence(
        spec,
        after_utc=datetime(2026, 10, 24, 23, 0, tzinfo=UTC),
        local_timezone=london,
    )
    after_first = next_occurrence(
        spec,
        after_utc=first + timedelta(seconds=1),
        local_timezone=london,
    )

    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert after_first == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)
