from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu

from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.runner import FlowRunWorker, RunnerService
from automadin.models.device import DeviceSession
from automadin.models.flow import AutomationFlow
from automadin.models.run_config import RunConfig, RunStatus
from automadin.ui.main_window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _worker() -> FlowRunWorker:
    worker = FlowRunWorker(
        FlowExecutor(),
        AutomationFlow("Rewards"),
        FlowContext(DeviceSession()),
        RunConfig(),
    )
    worker.configure_run_presentation(
        display_name="Daily Rewards",
        target_display_name="Pixel 8",
        source="manual",
    )
    return worker


def test_tray_menu_exposes_live_controls_and_pinned_starts() -> None:
    app = _app()
    menu = QMenu()
    service = RunnerService()
    worker = _worker()
    service.register_run("run-1", worker)
    fake = SimpleNamespace(
        _tray_menu=menu,
        _launcher=SimpleNamespace(
            runner_service=service,
            pinned_run_setups=lambda: ((42, "Pinned setup"),),
        ),
        _flow_runner=SimpleNamespace(runner_service=RunnerService()),
        _scheduler_host=SimpleNamespace(
            paused=False,
            next_schedule=lambda: None,
            set_paused=lambda _paused: None,
        ),
        _tray_next_action=None,
        _tray_pause_action=None,
        _show_run_status_detail=lambda *_args: None,
        _start_pinned_run=lambda _identity: None,
        _show_mini_run_window=lambda: None,
        activate_from_external_launch=lambda: None,
        request_exit=lambda: None,
    )

    MainWindow._rebuild_tray_menu(fake)  # type: ignore[arg-type]
    app.processEvents()

    texts = [action.text() for action in menu.actions()]
    assert "Running flows" in texts
    assert "▶ Pinned setup" in texts
    assert "Open mini player" in texts
    run_action = next(action for action in menu.actions() if action.menu() is not None)
    run_menu = run_action.menu()
    assert run_menu is not None
    assert [action.text() for action in run_menu.actions()] == [
        "Pause",
        "Stop",
        "Open details",
    ]

    run_menu.actions()[0].trigger()
    assert worker.status is RunStatus.PAUSING
    menu.deleteLater()
