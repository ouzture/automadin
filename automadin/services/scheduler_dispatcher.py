from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Callable

from automadin.execution.run_coordinator import (
    RunCapacityError,
    RunCoordinator,
    RunPreparationError,
)
from automadin.models.run_config import RunStatus
from automadin.models.schedule import (
    MissedRunPolicy,
    OverlapPolicy,
    RunHistoryOutcome,
    RunHistoryRecord,
    SchedulePattern,
    ScheduledFlow,
)
from automadin.services.browser_profile_leases import BrowserProfileInUseError
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import DeviceOperationConflictError
from automadin.services.qt_worker_lifecycle import qthread_start_may_have_succeeded
from automadin.services.schedule_recurrence import next_occurrence
from automadin.services.schedule_repository import ScheduleRepository

logger = logging.getLogger(__name__)

_MISSED_GRACE = timedelta(seconds=60)
_PENDING_WINDOW = timedelta(minutes=15)
_RETRY_INTERVAL = timedelta(seconds=5)


@dataclass(slots=True)
class _PendingRun:
    history: RunHistoryRecord
    deadline_utc: datetime


@dataclass(slots=True)
class _ActiveRun:
    history: RunHistoryRecord
    worker: object
    message: str = ""
    history_finished: bool = False


class ScheduleDispatcher:
    """Claims due occurrences and submits them through the shared coordinator."""

    def __init__(
        self,
        database: DatabaseService,
        repository: ScheduleRepository,
        coordinator: RunCoordinator,
        *,
        local_timezone: tzinfo,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._repository = repository
        self._coordinator = coordinator
        self._local_timezone = local_timezone
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending_by_schedule: dict[str, _PendingRun] = {}
        self._active_by_schedule: dict[str, _ActiveRun] = {}
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    def start(self) -> int:
        recovered = self._repository.recover_incomplete_runs(
            finished_at_utc=self._now()
        )
        self._repository.prune_history(now_utc=self._now())
        self._repair_missing_next_due()
        self.tick()
        return recovered

    def tick(self, now_utc: datetime | None = None) -> None:
        now = self._now() if now_utc is None else self._normalize_now(now_utc)
        self._retry_cleanup()
        if self._paused:
            return
        self._retry_pending(now)
        for schedule in self._repository.list_due(now):
            self._process_due(schedule, now)

    def next_wake_at(self, now_utc: datetime | None = None) -> datetime | None:
        now = self._now() if now_utc is None else self._normalize_now(now_utc)
        if self._pending_by_schedule or any(
            active.history_finished for active in self._active_by_schedule.values()
        ):
            return now + _RETRY_INTERVAL
        if self._paused:
            return None
        return self._repository.next_enabled_due()

    def has_enabled_schedules(self) -> bool:
        return self._repository.enabled_count() > 0

    def has_active_runs(self) -> bool:
        return bool(self._active_by_schedule)

    def update_local_timezone(
        self,
        local_timezone: tzinfo,
        *,
        now_utc: datetime | None = None,
    ) -> int:
        """Recalculate enabled schedules after the system timezone changes."""
        now = self._normalize_now(now_utc)
        self._local_timezone = local_timezone
        updated = 0
        for schedule in self._repository.list():
            if not schedule.enabled:
                continue
            next_due = next_occurrence(
                schedule.spec,
                after_utc=now,
                local_timezone=local_timezone,
            )
            if self._repository.set_next_due(
                schedule.schedule_id,
                next_due,
                now_utc=now,
            ):
                updated += 1
        return updated

    def _process_due(self, schedule: ScheduledFlow, now: datetime) -> None:
        due = schedule.next_due_at_utc
        if due is None:
            return
        missed = due < now - _MISSED_GRACE
        next_after = now if missed else due
        next_due = next_occurrence(
            schedule.spec,
            after_utc=next_after,
            local_timezone=self._local_timezone,
        )
        history = self._repository.claim_occurrence(
            schedule.schedule_id,
            expected_due_at_utc=due,
            next_due_at_utc=next_due,
            now_utc=now,
        )
        if history is None:
            return
        if missed and schedule.spec.missed_policy is MissedRunPolicy.SKIP:
            self._finish_history(
                history.run_id,
                outcome=RunHistoryOutcome.SKIPPED,
                message="Missed while Automadin was not available.",
                finished_at_utc=now,
            )
            return
        if schedule.schedule_id in self._active_by_schedule:
            if schedule.spec.overlap_policy is OverlapPolicy.QUEUE_ONE:
                self._queue(history, now)
            else:
                self._finish_history(
                    history.run_id,
                    outcome=RunHistoryOutcome.SKIPPED,
                    message="The previous occurrence was still running.",
                    finished_at_utc=now,
                )
            return
        self._try_start(history, now)

    def _try_start(
        self,
        history: RunHistoryRecord,
        now: datetime,
        *,
        pending_deadline: datetime | None = None,
    ) -> None:
        if history.run_config_id is None:
            self._finish_blocked(history, "The saved run setup no longer exists.", now)
            return
        saved_config = self._database.load_run_config_by_id(history.run_config_id)
        if saved_config is None:
            self._finish_blocked(history, "The saved run setup no longer exists.", now)
            return
        try:
            prepare_scheduled = getattr(self._coordinator, "prepare_scheduled", None)
            if callable(prepare_scheduled):
                prepared = prepare_scheduled(saved_config)
            else:
                prepared = self._coordinator.prepare(saved_config)
        except RunPreparationError as exc:
            self._finish_blocked(history, exc.public_message, now)
            return
        confirmation_issues = prepared.preflight.errors + prepared.preflight.warnings
        if confirmation_issues:
            self._finish_blocked(
                history,
                "Scheduled preflight needs attention: "
                + confirmation_issues[0].message,
                now,
            )
            return
        try:
            register_options: dict[str, object] = {"run_id": history.run_id}
            if isinstance(self._coordinator, RunCoordinator):
                register_options.update(
                    source="scheduled",
                    display_name=history.schedule_name,
                )
            registered = self._coordinator.register(prepared, **register_options)
        except RunCapacityError:
            self._queue(history, now, deadline_utc=pending_deadline)
            return

        worker = registered.worker
        active = _ActiveRun(history=history, worker=worker)
        self._active_by_schedule[history.schedule_id or ""] = active
        worker.status_changed.connect(
            lambda status, run_id=history.run_id: self._status_changed(run_id, status)
        )
        worker.completed.connect(
            lambda message, run_id=history.run_id: self._completed(run_id, message)
        )
        worker.finished.connect(
            lambda run_id=history.run_id: self._worker_finished(run_id)
        )
        try:
            self._coordinator.start(registered)
        except (BrowserProfileInUseError, DeviceOperationConflictError):
            if self._coordinator.remove(history.run_id):
                self._active_by_schedule.pop(history.schedule_id or "", None)
                self._queue(history, now, deadline_utc=pending_deadline)
            else:
                self._finish_blocked(
                    history,
                    "Target ownership cleanup is pending; review Diagnostics.",
                    now,
                )
                active.history_finished = True
            return
        except BaseException as exc:
            if qthread_start_may_have_succeeded(worker):
                logger.exception(
                    "Scheduled worker reported a startup error after beginning"
                )
                worker.ctx.stop()
                self._repository.mark_running(
                    history.run_id,
                    started_at_utc=now,
                )
                return
            logger.exception("Scheduled worker could not start")
            self._finish_history(
                history.run_id,
                outcome=RunHistoryOutcome.FAILED,
                message=f"Scheduled run could not start: {type(exc).__name__}.",
                finished_at_utc=now,
            )
            active.history_finished = True
            self._retry_cleanup()

    def _queue(
        self,
        history: RunHistoryRecord,
        now: datetime,
        *,
        deadline_utc: datetime | None = None,
    ) -> None:
        key = history.schedule_id or ""
        existing = self._pending_by_schedule.get(key)
        if existing is not None:
            self._finish_history(
                history.run_id,
                outcome=RunHistoryOutcome.SKIPPED,
                message="One occurrence is already queued for this schedule.",
                finished_at_utc=now,
            )
            return
        self._pending_by_schedule[key] = _PendingRun(
            history=history,
            deadline_utc=deadline_utc or now + _PENDING_WINDOW,
        )

    def _retry_pending(self, now: datetime) -> None:
        for key, pending in list(self._pending_by_schedule.items()):
            if key in self._active_by_schedule:
                continue
            if now >= pending.deadline_utc:
                self._pending_by_schedule.pop(key, None)
                self._finish_blocked(
                    pending.history,
                    "The target or run capacity remained unavailable for 15 minutes.",
                    now,
                )
                continue
            self._pending_by_schedule.pop(key, None)
            self._try_start(
                pending.history,
                now,
                pending_deadline=pending.deadline_utc,
            )

    def _retry_cleanup(self) -> None:
        for key, active in list(self._active_by_schedule.items()):
            if not active.history_finished:
                continue
            if self._coordinator.remove(active.history.run_id):
                self._active_by_schedule.pop(key, None)
            else:
                mark_pending = getattr(active.worker, "mark_cleanup_pending", None)
                if callable(mark_pending):
                    mark_pending()

    def _status_changed(self, run_id: str, status_value: str) -> None:
        if status_value == RunStatus.RUNNING.value:
            self._repository.mark_running(run_id, started_at_utc=self._now())

    def _completed(self, run_id: str, message: str) -> None:
        active = self._active_for_run(run_id)
        if active is not None:
            active.message = message

    def _worker_finished(self, run_id: str) -> None:
        active = self._active_for_run(run_id)
        if active is None or active.history_finished:
            return
        status = getattr(active.worker, "status", RunStatus.ERROR)
        attention_message = getattr(active.worker, "attention_required_message", None)
        if isinstance(attention_message, str) and attention_message:
            outcome = RunHistoryOutcome.REQUIRES_ATTENTION
            message = f"Requires attention: {attention_message}"
        elif status is RunStatus.COMPLETED:
            outcome = RunHistoryOutcome.COMPLETED
            message = active.message
        elif status is RunStatus.STOPPED:
            outcome = RunHistoryOutcome.CANCELLED
            message = active.message
        else:
            outcome = RunHistoryOutcome.FAILED
            message = active.message
        self._finish_history(
            run_id,
            outcome=outcome,
            message=message,
            finished_at_utc=self._now(),
        )
        active.history_finished = True
        self._retry_cleanup()

    def _active_for_run(self, run_id: str) -> _ActiveRun | None:
        return next(
            (
                active
                for active in self._active_by_schedule.values()
                if active.history.run_id == run_id
            ),
            None,
        )

    def _finish_blocked(
        self,
        history: RunHistoryRecord,
        message: str,
        now: datetime,
    ) -> None:
        self._finish_history(
            history.run_id,
            outcome=RunHistoryOutcome.BLOCKED,
            message=message,
            finished_at_utc=now,
        )

    def _finish_history(
        self,
        run_id: str,
        *,
        outcome: RunHistoryOutcome,
        message: str,
        finished_at_utc: datetime,
    ) -> None:
        self._repository.finish(
            run_id,
            outcome=outcome,
            message=message,
            finished_at_utc=finished_at_utc,
        )
        self._repository.prune_history(now_utc=finished_at_utc)

    def _repair_missing_next_due(self) -> None:
        now = self._now()
        for schedule in self._repository.list():
            if (
                not schedule.enabled
                or schedule.next_due_at_utc is not None
                or schedule.spec.pattern is SchedulePattern.ONCE
            ):
                continue
            repaired = next_occurrence(
                schedule.spec,
                after_utc=now,
                local_timezone=self._local_timezone,
            )
            self._repository.set_next_due(
                schedule.schedule_id,
                repaired,
                now_utc=now,
            )

    def _now(self) -> datetime:
        return self._normalize_now(self._clock())

    @staticmethod
    def _normalize_now(value: datetime | None) -> datetime:
        result = value or datetime.now(UTC)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("Scheduler clock must return a timezone-aware time")
        return result.astimezone(UTC)
