from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo

from automadin.models.schedule import SchedulePattern, ScheduleSpec


_DST_SEARCH_MINUTES = 180


def _first_valid_utc(local_value: datetime, timezone: tzinfo) -> datetime:
    """Resolve a local wall time, preferring the first ambiguous occurrence."""
    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = local_value.replace(tzinfo=timezone, fold=fold)
        utc_value = aware.astimezone(UTC)
        round_trip = utc_value.astimezone(timezone).replace(tzinfo=None)
        if round_trip == local_value:
            candidates.append(utc_value)
    if candidates:
        return min(candidates)

    for minutes in range(1, _DST_SEARCH_MINUTES + 1):
        candidate = local_value + timedelta(minutes=minutes)
        for fold in (0, 1):
            aware = candidate.replace(tzinfo=timezone, fold=fold)
            utc_value = aware.astimezone(UTC)
            if utc_value.astimezone(timezone).replace(tzinfo=None) == candidate:
                return utc_value
    raise ValueError("Could not resolve local schedule time across clock transition")


def _local_candidate(
    candidate_date: date,
    spec: ScheduleSpec,
    timezone: tzinfo,
) -> datetime:
    if spec.local_time is None:  # guarded by ScheduleSpec validation
        raise ValueError("Schedule does not define a local time")
    return _first_valid_utc(
        datetime.combine(candidate_date, spec.local_time),
        timezone,
    )


def next_occurrence(
    spec: ScheduleSpec,
    *,
    after_utc: datetime,
    local_timezone: tzinfo,
) -> datetime | None:
    """Return the first scheduled UTC instant strictly after ``after_utc``."""
    if after_utc.tzinfo is None or after_utc.utcoffset() is None:
        raise ValueError("after_utc must be timezone-aware")
    after = after_utc.astimezone(UTC)

    if spec.pattern in (SchedulePattern.ONCE, SchedulePattern.INTERVAL):
        if spec.start_at_local is None:  # guarded by ScheduleSpec validation
            raise ValueError("Schedule does not define a start time")
        anchor = _first_valid_utc(spec.start_at_local, local_timezone)
        if spec.pattern is SchedulePattern.ONCE:
            return anchor if anchor > after else None
        interval = timedelta(minutes=int(spec.interval_minutes or 0))
        if after < anchor:
            return anchor
        elapsed_intervals = (after - anchor) // interval
        return anchor + interval * (elapsed_intervals + 1)

    local_after = after.astimezone(local_timezone)
    candidate_date = local_after.date()
    for offset in range(8):
        day = candidate_date + timedelta(days=offset)
        if (
            spec.pattern is SchedulePattern.WEEKLY
            and day.weekday() not in spec.weekdays
        ):
            continue
        candidate = _local_candidate(day, spec, local_timezone)
        if candidate > after:
            return candidate
    raise ValueError("Could not calculate the next schedule occurrence")
