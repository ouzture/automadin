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
from automadin.ui.launcher.launcher_view import LauncherView
from automadin.ui.launcher.schedule_dialog import ScheduleDialog
from automadin.ui.launcher.schedule_views import (
    ScheduleHistoryView,
    ScheduleListView,
    schedule_summary,
)


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _database(tmp_path) -> tuple[DatabaseService, int]:
    database = DatabaseService(str(tmp_path / "automadin.db"))
    flow = AutomationFlow("Flow", [FlowCommand("wait", params={"ms": 1})])
    database.save_flow(flow.name, flow)
    setup = SavedRunConfig(
        name="Nightly setup",
        flow_name=flow.name,
        device_id="emulator-5554",
    )
    database.save_run_config(setup.name, setup)
    run_config_id = database.get_run_config_id(setup.name)
    assert run_config_id is not None
    return database, run_config_id


def test_schedule_summary_formats_supported_patterns(tmp_path) -> None:
    database, run_config_id = _database(tmp_path)
    repository = ScheduleRepository(database)
    cases = (
        (
            ScheduleSpec(
                pattern=SchedulePattern.ONCE,
                start_at_local=datetime(2026, 8, 1, 9, 30),
            ),
            "Once at 2026-08-01 09:30",
        ),
        (
            ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 30)),
            "Daily at 09:30",
        ),
        (
            ScheduleSpec(
                pattern=SchedulePattern.WEEKLY,
                local_time=time(9, 30),
                weekdays=(0, 4),
            ),
            "Mon, Fri at 09:30",
        ),
        (
            ScheduleSpec(
                pattern=SchedulePattern.INTERVAL,
                start_at_local=datetime(2026, 8, 1, 9, 30),
                interval_minutes=120,
            ),
            "Every 2 hours",
        ),
    )

    for index, (spec, expected) in enumerate(cases):
        schedule = repository.create(
            name=f"Schedule {index}",
            run_config_id=run_config_id,
            spec=spec,
            enabled=False,
        )
        assert schedule_summary(schedule) == expected
    database.close()


def test_schedule_dialog_returns_explicit_interval_values(tmp_path) -> None:
    app = _application()
    database, run_config_id = _database(tmp_path)
    dialog = ScheduleDialog(database)
    try:
        dialog._name.setText("Every two hours")
        dialog._pattern.setCurrentIndex(
            dialog._pattern.findData(SchedulePattern.INTERVAL)
        )
        dialog._interval_amount.setValue(2)
        dialog._interval_unit.setCurrentIndex(1)

        name, selected_id, spec, enabled = dialog.values()

        assert name == "Every two hours"
        assert selected_id == run_config_id
        assert spec.pattern is SchedulePattern.INTERVAL
        assert spec.interval_minutes == 120
        assert enabled
        assert dialog.next_due_at() is not None
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
        database.close()


def test_schedule_list_toggle_and_run_now_preserve_occurrence(tmp_path) -> None:
    app = _application()
    database, run_config_id = _database(tmp_path)
    repository = ScheduleRepository(database)
    due = datetime.now(UTC) + timedelta(days=1)
    schedule = repository.create(
        name="Daily",
        run_config_id=run_config_id,
        spec=ScheduleSpec(
            pattern=SchedulePattern.DAILY,
            local_time=due.astimezone().time().replace(second=0, microsecond=0),
        ),
        enabled=True,
        next_due_at_utc=due,
    )
    view = ScheduleListView(database)
    requested: list[int] = []
    view.run_now_requested.connect(requested.append)
    try:
        view._tree.setCurrentItem(view._tree.topLevelItem(0))
        view._run_now()
        assert requested == [run_config_id]
        assert repository.get(schedule.schedule_id).next_due_at_utc == due

        view._toggle()
        stored = repository.get(schedule.schedule_id)
        assert stored is not None
        assert not stored.enabled
        assert stored.next_due_at_utc == due
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
        database.close()


def test_history_view_shows_durable_terminal_result(tmp_path) -> None:
    app = _application()
    database, run_config_id = _database(tmp_path)
    repository = ScheduleRepository(database)
    due = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    schedule = repository.create(
        name="History schedule",
        run_config_id=run_config_id,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        next_due_at_utc=due,
    )
    history = repository.claim_occurrence(
        schedule.schedule_id,
        expected_due_at_utc=due,
        next_due_at_utc=due + timedelta(days=1),
    )
    assert history is not None
    repository.finish(
        history.run_id,
        outcome=RunHistoryOutcome.BLOCKED,
        message="Device unavailable",
    )

    view = ScheduleHistoryView(database)
    try:
        item = view._tree.topLevelItem(0)
        assert item.text(1) == "History schedule"
        assert item.text(4) == "Blocked"
        assert item.text(7) == "Device unavailable"
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
        database.close()


def test_launcher_exposes_run_setup_schedule_and_history_tabs(tmp_path) -> None:
    app = _application()
    database, _run_config_id = _database(tmp_path)
    launcher = LauncherView(database)
    try:
        labels = [
            launcher._workspace_tabs.tabText(index)
            for index in range(launcher._workspace_tabs.count())
        ]
        assert labels == ["Run setups", "Schedules", "History"]
    finally:
        launcher.close()
        launcher.deleteLater()
        app.processEvents()
        database.close()
