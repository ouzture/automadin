from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import SavedRunConfig
from automadin.models.schedule import (
    RunHistoryOutcome,
    SchedulePattern,
    ScheduleSpec,
)
from automadin.services.database import DatabaseService
from automadin.services.schedule_repository import ScheduleRepository
from automadin.services.scheduler_host import (
    SCHEDULER_PAUSED_SETTING,
    SchedulerHost,
    system_local_timezone,
)


class _UnusedCoordinator:
    pass


def _qt_application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_system_local_timezone_is_aware() -> None:
    zone = system_local_timezone()

    assert datetime.now(zone).utcoffset() is not None


def test_pause_state_is_persisted_and_restored(tmp_path) -> None:
    app = _qt_application()
    database = DatabaseService(str(tmp_path / "scheduler.db"))
    host = SchedulerHost(database, _UnusedCoordinator())

    host.set_paused(True)
    assert host.paused
    assert database.get_app_setting(SCHEDULER_PAUSED_SETTING) == "1"

    restored = SchedulerHost(database, _UnusedCoordinator())
    assert restored.paused

    restored.set_paused(False)
    assert not restored.paused
    assert database.get_app_setting(SCHEDULER_PAUSED_SETTING) == "0"

    host.stop()
    restored.stop()
    app.processEvents()
    database.close()


def test_host_without_enabled_schedules_leaves_timer_idle(tmp_path) -> None:
    app = _qt_application()
    database = DatabaseService(str(tmp_path / "scheduler.db"))
    host = SchedulerHost(database, _UnusedCoordinator())

    host.start()

    assert not host.has_enabled_schedules()
    assert host.next_due_at() is None
    assert not host._timer.isActive()

    host.stop()
    app.processEvents()
    database.close()


def test_host_emits_new_terminal_history_after_start(tmp_path) -> None:
    app = _qt_application()
    database = DatabaseService(str(tmp_path / "scheduler.db"))
    flow = AutomationFlow("Flow", [FlowCommand("wait", params={"ms": 1})])
    database.save_flow(flow.name, flow)
    setup = SavedRunConfig(
        name="Setup",
        flow_name=flow.name,
        device_id="emulator-5554",
    )
    database.save_run_config(setup.name, setup)
    run_config_id = database.get_run_config_id(setup.name)
    assert run_config_id is not None
    due = datetime.now(UTC) + timedelta(days=1)
    repository = ScheduleRepository(database)
    schedule = repository.create(
        name="Schedule",
        run_config_id=run_config_id,
        spec=ScheduleSpec(
            pattern=SchedulePattern.DAILY,
            local_time=time(9, 0),
        ),
        next_due_at_utc=due,
    )
    host = SchedulerHost(database, _UnusedCoordinator())
    records = []
    host.run_finished.connect(records.append)
    host.start()

    history = repository.claim_occurrence(
        schedule.schedule_id,
        expected_due_at_utc=due,
        next_due_at_utc=due + timedelta(days=1),
    )
    assert history is not None
    repository.finish(
        history.run_id,
        outcome=RunHistoryOutcome.BLOCKED,
        message="Target unavailable",
    )
    host._on_timeout()

    assert [record.run_id for record in records] == [history.run_id]
    host.stop()
    app.processEvents()
    database.close()
