from __future__ import annotations

import logging
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import QObject, QTimeZone, QTimer, Signal

from automadin.execution.run_coordinator import RunCoordinator
from automadin.models.schedule import ScheduledFlow
from automadin.services.database import DatabaseService
from automadin.services.schedule_repository import ScheduleRepository
from automadin.services.scheduler_dispatcher import ScheduleDispatcher

logger = logging.getLogger(__name__)

SCHEDULER_PAUSED_SETTING = "scheduler_paused"
_MAX_TIMER_MS = 60_000
_MIN_TIMER_MS = 100


def system_local_timezone() -> tzinfo:
    """Resolve Qt's Windows-aware system zone through Python's IANA database."""
    zone_id = bytes(QTimeZone.systemTimeZoneId()).decode("utf-8", errors="strict")
    if zone_id:
        try:
            return ZoneInfo(zone_id)
        except ZoneInfoNotFoundError:
            logger.warning("System IANA time zone is unavailable: %s", zone_id)
    fallback = datetime.now().astimezone().tzinfo
    if fallback is None:  # pragma: no cover - supported platforms always provide one
        return UTC
    return fallback


def _timezone_identity(value: tzinfo) -> str:
    return str(getattr(value, "key", None) or value)


class SchedulerHost(QObject):
    """Qt timer adapter around the deterministic schedule dispatcher."""

    state_changed = Signal()
    run_finished = Signal(object)

    def __init__(
        self,
        database: DatabaseService,
        coordinator: RunCoordinator,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._repository = ScheduleRepository(database)
        self._local_timezone = system_local_timezone()
        self._dispatcher = ScheduleDispatcher(
            database,
            self._repository,
            coordinator,
            local_timezone=self._local_timezone,
        )
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        self._started = False
        self._known_terminal_run_ids: set[str] = set()
        self._dispatcher.set_paused(
            database.get_app_setting(SCHEDULER_PAUSED_SETTING, "0") == "1"
        )

    @property
    def paused(self) -> bool:
        return self._dispatcher.paused

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        recovered = self._dispatcher.start()
        if recovered:
            logger.warning("Recovered %d interrupted scheduled run(s)", recovered)
        self._known_terminal_run_ids = self._terminal_run_ids()
        self._arm()
        self.state_changed.emit()

    def stop(self) -> None:
        self._timer.stop()
        self._started = False

    def refresh(self) -> None:
        if not self._started:
            return
        self._on_timeout()

    def set_paused(self, paused: bool) -> None:
        self._dispatcher.set_paused(paused)
        self._database.set_app_setting(
            SCHEDULER_PAUSED_SETTING,
            "1" if paused else "0",
        )
        if self._started:
            self._on_timeout()
        self.state_changed.emit()

    def has_enabled_schedules(self) -> bool:
        return self._dispatcher.has_enabled_schedules()

    def has_active_runs(self) -> bool:
        return self._dispatcher.has_active_runs()

    def next_due_at(self) -> datetime | None:
        return self._repository.next_enabled_due()

    def next_schedule(self) -> ScheduledFlow | None:
        return self._repository.next_enabled()

    def _on_timeout(self) -> None:
        if not self._started:
            return
        try:
            current_timezone = system_local_timezone()
            if _timezone_identity(current_timezone) != _timezone_identity(
                self._local_timezone
            ):
                logger.info(
                    "System timezone changed from %s to %s; recalculating schedules",
                    self._local_timezone,
                    current_timezone,
                )
                self._dispatcher.update_local_timezone(current_timezone)
                self._local_timezone = current_timezone
            self._dispatcher.tick()
        except BaseException:
            logger.exception("Scheduled-flow dispatcher tick failed")
        self._emit_new_terminal_runs()
        self._arm()
        self.state_changed.emit()

    def _arm(self) -> None:
        self._timer.stop()
        next_wake = self._dispatcher.next_wake_at()
        if next_wake is None:
            return
        delay_ms = round(
            max(0.0, (next_wake - datetime.now(UTC)).total_seconds()) * 1000
        )
        self._timer.start(max(_MIN_TIMER_MS, min(delay_ms, _MAX_TIMER_MS)))

    def _terminal_run_ids(self) -> set[str]:
        return {
            record.run_id
            for record in self._repository.list_history(limit=5000)
            if record.finished_at_utc is not None
        }

    def _emit_new_terminal_runs(self) -> None:
        for record in reversed(self._repository.list_history(limit=5000)):
            if (
                record.finished_at_utc is None
                or record.run_id in self._known_terminal_run_ids
            ):
                continue
            self._known_terminal_run_ids.add(record.run_id)
            self.run_finished.emit(record)
