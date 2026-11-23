from __future__ import annotations

import threading
import time
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QTextEdit, QWidget

from automadin.engine.session import DeviceSessionService
from automadin.execution.context import FlowContext
from automadin.execution.attention import AttentionRequest, AttentionResolution
from automadin.execution.executor import FlowExecutor
from automadin.execution.log_granularity import LogGranularity
from automadin.execution.runner import FlowRunWorker, RunnerService
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.run_config import RunConfig, SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import (
    DeviceOperationConflictError,
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
)
from automadin.ui.flow_runner.runner_view import FlowRunnerView
from automadin.ui.launcher.launcher_view import LauncherRunWorker, LauncherView
from automadin.ui.launcher.run_panel import RunPanel
from automadin.ui.log_formatting import append_semantic_log
from automadin.ui.theme import get_stylesheet
from automadin.ui.widgets.toggle_switch import ToggleSwitch


def _wait_until(
    application: QApplication,
    predicate,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for worker lifecycle event")
        application.processEvents()
        time.sleep(0.005)
    application.processEvents()


def _blocking_worker(
    *,
    session_id: str,
) -> tuple[FlowRunWorker, threading.Event, threading.Event]:
    entered = threading.Event()
    release = threading.Event()
    executor = FlowExecutor()

    def blocking_handler(_command: FlowCommand, _ctx: FlowContext) -> None:
        entered.set()
        release.wait(2)

    executor.register_handler("blocking", blocking_handler)
    worker = FlowRunWorker(
        executor,
        AutomationFlow("Blocking flow", [FlowCommand("blocking")]),
        FlowContext(
            DeviceSession(
                session_id=session_id,
                device=AndroidDevice("emulator-5554"),
            )
        ),
        RunConfig(),
        device_operation_registry=DeviceOperationLeaseRegistry(),
    )
    return worker, entered, release


def test_flow_runner_stop_returns_promptly_and_stays_stopping_until_finished(
    application: QApplication,
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "runner-lifecycle.db"))
    view = FlowRunnerView(database, DeviceSessionService())
    worker, entered, release = _blocking_worker(session_id="runner-stop-session")
    service = RunnerService()
    service.register_run("run-1", worker)
    view._runner_service = service
    view._current_run_id = "run-1"
    view._current_worker = worker
    view._run_btn.setEnabled(False)
    worker.finished.connect(view._on_worker_finished)
    worker.start()

    try:
        assert entered.wait(1)
        started_at = time.monotonic()
        view._stop_run()
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.25
        assert worker.isRunning()
        assert worker.ctx.is_stopped
        assert view._current_worker is worker
        assert service.get_worker("run-1") is worker
        assert view._stop_btn.text() == "Stopping…"
        assert not view._stop_btn.isEnabled()
        assert view._status_label.text() == "Status: Stopping…"
        assert not view._run_btn.isEnabled()

        release.set()
        _wait_until(application, lambda: view._current_worker is None)

        assert service.get_worker("run-1") is None
        assert view._stop_btn.text() == "Stop"
        assert view._run_btn.isEnabled()
    finally:
        release.set()
        worker.wait(2000)
        view.deleteLater()
        application.processEvents()
        database.close()


def test_flow_runner_shutdown_is_nonblocking_and_retains_live_worker(
    application: QApplication,
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "runner-shutdown.db"))
    view = FlowRunnerView(database, DeviceSessionService())
    worker, entered, release = _blocking_worker(session_id="runner-shutdown-session")
    service = RunnerService()
    service.register_run("run-2", worker)
    view._runner_service = service
    worker.start()

    try:
        assert entered.wait(1)
        started_at = time.monotonic()
        stopped = view.shutdown(wait_ms=0)
        elapsed = time.monotonic() - started_at

        assert not stopped
        assert elapsed < 0.25
        assert worker.ctx.is_stopped
        assert worker.isRunning()
        assert service.get_worker("run-2") is worker

        release.set()
        assert worker.wait(2000)
        assert view.shutdown(wait_ms=1000)
    finally:
        release.set()
        worker.wait(2000)
        assert service.remove_run("run-2")
        view.deleteLater()
        application.processEvents()
        database.close()


def test_flow_runner_failed_start_keeps_ui_owner_until_cleanup_succeeds(
    application: QApplication,
    tmp_path,
) -> None:
    class _TransientCleanupService:
        def __init__(self) -> None:
            self.calls = 0
            self.worker = None

        def remove_run(self, _run_id: str) -> bool:
            self.calls += 1
            return self.calls > 1

        def get_worker(self, _run_id: str):
            return self.worker

    database = DatabaseService(str(tmp_path / "runner-start-cleanup.db"))
    view = FlowRunnerView(database, DeviceSessionService())
    service = _TransientCleanupService()
    marker = object()
    service.worker = marker
    view._runner_service = service  # type: ignore[assignment]
    view._current_run_id = "failed-start"
    view._current_worker = marker  # type: ignore[assignment]
    view._run_btn.setEnabled(False)

    try:
        assert not view._detach_failed_start("failed-start")
        assert view._current_run_id == "failed-start"
        assert view._current_worker is marker
        assert view._run_btn.isEnabled()
        assert view._run_btn.text() == "Retry Cleanup"
        assert view._status_label.text() == "Status: Cleanup Pending"

        view._run_btn.click()
        assert view._current_run_id is None
        assert view._current_worker is None
        assert view._run_btn.isEnabled()
        assert view._run_btn.text() == "Run"
        assert service.calls == 2
    finally:
        view.deleteLater()
        application.processEvents()
        database.close()


def test_flow_runner_finished_cleanup_failure_stays_owned_until_retry(
    application: QApplication,
    tmp_path,
) -> None:
    class _TransientCleanupRunnerService(RunnerService):
        def __init__(self) -> None:
            super().__init__()
            self.remove_calls = 0

        def remove_run(self, run_id: str, *, wait_ms: int = 0) -> bool:
            self.remove_calls += 1
            if self.remove_calls == 1:
                return False
            return super().remove_run(run_id, wait_ms=wait_ms)

    database = DatabaseService(str(tmp_path / "runner-finished-cleanup.db"))
    view = FlowRunnerView(database, DeviceSessionService())
    worker, entered, release = _blocking_worker(
        session_id="runner-finished-cleanup-session"
    )
    service = _TransientCleanupRunnerService()
    service.register_run("finished-cleanup", worker)
    view._runner_service = service
    view._current_run_id = "finished-cleanup"
    view._current_worker = worker
    view._run_btn.setEnabled(False)
    worker.finished.connect(view._on_worker_finished)
    worker.start()

    try:
        assert entered.wait(1)
        release.set()
        _wait_until(
            application,
            lambda: view._status_label.text() == "Status: Cleanup Pending",
        )

        assert view._current_run_id == "finished-cleanup"
        assert view._current_worker is worker
        assert service.get_worker("finished-cleanup") is worker
        assert view._run_btn.isEnabled()
        assert view._run_btn.text() == "Retry Cleanup"
        assert not view._next_step_btn.isEnabled()
        assert not view._flow_combo.isEnabled()

        view._run_btn.click()

        assert service.remove_calls == 2
        assert service.get_worker("finished-cleanup") is None
        assert view._current_run_id is None
        assert view._current_worker is None
        assert view._run_btn.text() == "Run"
        assert view._run_btn.isEnabled()
        assert view._next_step_btn.isEnabled()
        assert view._flow_combo.isEnabled()
    finally:
        release.set()
        worker.wait(2000)
        service.remove_run("finished-cleanup")
        view.deleteLater()
        application.processEvents()
        database.close()


def test_launcher_failed_start_keeps_panel_until_cleanup_succeeds(
    application: QApplication,
    tmp_path,
) -> None:
    class _TransientProfileLease:
        def __init__(self) -> None:
            self.calls = 0
            self.released = False

        def release(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient profile cleanup failure")
            self.released = True
            return True

    database = DatabaseService(str(tmp_path / "launcher-start-cleanup.db"))
    session_service = DeviceSessionService()
    view = LauncherView(database, session_service)
    service = RunnerService()
    profile_lease = _TransientProfileLease()
    worker = LauncherRunWorker(
        FlowExecutor(),
        AutomationFlow("Failed launcher start"),
        RunConfig(),
        AndroidDevice("launcher-cleanup-device"),
        session_service,
        profile_lease=profile_lease,  # type: ignore[arg-type]
    )
    worker.reserve_device_operation()
    service.register_run("failed-start", worker)
    placeholder = QWidget(view)
    view._runner_service = service
    view._run_panels["failed-start"] = placeholder  # type: ignore[assignment]

    try:
        assert not view._detach_failed_start("failed-start")
        assert view._run_panels["failed-start"] is placeholder
        assert placeholder.parent() is view
        assert service.get_worker("failed-start") is worker
        assert worker._device_operation_lease is not None
        assert worker._profile_lease is profile_lease

        assert view._detach_failed_start("failed-start")
        assert "failed-start" not in view._run_panels
        assert placeholder.parent() is None
        assert service.get_worker("failed-start") is None
        assert worker._device_operation_lease is None
        assert worker._profile_lease is None
        assert profile_lease.released
    finally:
        placeholder.deleteLater()
        view.deleteLater()
        application.processEvents()
        database.close()


def test_launcher_stop_during_session_creation_survives_context_resolution(
    application: QApplication,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    executed: list[str] = []
    device = AndroidDevice("launcher-slow-connect-device")

    class _SlowSessionService:
        def ensure_android_session(self, selected_device):
            entered.set()
            release.wait(2)
            return DeviceSession(device=selected_device)

    executor = FlowExecutor()
    executor.register_handler(
        "probe",
        lambda _command, _ctx: executed.append("executed"),
    )
    worker = LauncherRunWorker(
        executor,
        AutomationFlow("Slow launcher", [FlowCommand("probe")]),
        RunConfig(),
        device,
        _SlowSessionService(),
    )
    original_context = worker.ctx
    worker.start()

    try:
        assert entered.wait(1)
        worker.ctx.stop()
        release.set()
        assert worker.wait(2000)

        assert worker.ctx is original_context
        assert worker.ctx.is_stopped
        assert executed == []
    finally:
        release.set()
        worker.wait(2000)
        worker.deleteLater()
        application.processEvents()


def test_launcher_panel_close_retries_profile_cleanup_before_detaching(
    application: QApplication,
    tmp_path,
) -> None:
    class _TransientProfileLease:
        profile_id = "transient-profile"

        def __init__(self) -> None:
            self.calls = 0

        def release(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient profile cleanup failure")
            return True

    database = DatabaseService(str(tmp_path / "launcher-close-cleanup.db"))
    view = LauncherView(database, DeviceSessionService())
    service = RunnerService()
    profile_lease = _TransientProfileLease()
    worker = LauncherRunWorker(
        FlowExecutor(),
        AutomationFlow("Finished launcher"),
        RunConfig(),
        AndroidDevice("launcher-close-cleanup-device"),
        DeviceSessionService(),
        profile_lease=profile_lease,  # type: ignore[arg-type]
    )
    service.register_run("finished-run", worker)
    panel = RunPanel(
        "Finished setup",
        "finished-run",
        worker,
        on_close=view._close_run_panel,
        parent=view,
    )
    view._runner_service = service
    view._run_panels["finished-run"] = panel

    try:
        with patch.object(QMessageBox, "information"):
            panel._on_close_clicked()

        assert service.get_worker("finished-run") is worker
        assert view._run_panels["finished-run"] is panel
        assert panel._close_btn.isEnabled()
        assert panel._status_badge.text() == "CLEANUP PENDING"
        assert worker._profile_lease is profile_lease

        panel._on_close_clicked()

        assert service.get_worker("finished-run") is None
        assert "finished-run" not in view._run_panels
        assert worker._profile_lease is None
        assert profile_lease.calls == 2
    finally:
        panel.deleteLater()
        view.deleteLater()
        application.processEvents()
        database.close()


def test_launcher_shutdown_retries_profile_cleanup_without_detaching_owner(
    application: QApplication,
    tmp_path,
) -> None:
    class _TransientProfileLease:
        profile_id = "shutdown-profile"

        def __init__(self) -> None:
            self.calls = 0

        def release(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient profile cleanup failure")
            return True

    database = DatabaseService(str(tmp_path / "launcher-shutdown-cleanup.db"))
    view = LauncherView(database, DeviceSessionService())
    service = RunnerService()
    profile_lease = _TransientProfileLease()
    worker = LauncherRunWorker(
        FlowExecutor(),
        AutomationFlow("Finished launcher"),
        RunConfig(),
        AndroidDevice("launcher-shutdown-cleanup-device"),
        DeviceSessionService(),
        profile_lease=profile_lease,  # type: ignore[arg-type]
    )
    service.register_run("shutdown-run", worker)
    view._runner_service = service

    try:
        assert view.shutdown(wait_ms=0)
        assert service.get_worker("shutdown-run") is worker
        assert worker._profile_lease is profile_lease

        assert view.shutdown(wait_ms=0)
        assert service.get_worker("shutdown-run") is worker
        assert worker._profile_lease is None
        assert profile_lease.calls == 2
    finally:
        assert service.remove_run("shutdown-run")
        view.deleteLater()
        application.processEvents()
        database.close()


def test_launcher_quarantined_profile_blocks_panel_close_but_not_process_exit(
    application: QApplication,
    tmp_path,
) -> None:
    class _QuarantinedProfileLease:
        profile_id = "quarantined-profile"

        def __init__(self) -> None:
            self.calls = 0

        def release(self) -> bool:
            self.calls += 1
            return True

    database = DatabaseService(str(tmp_path / "launcher-quarantine-shutdown.db"))
    view = LauncherView(database, DeviceSessionService())
    service = RunnerService()
    profile_lease = _QuarantinedProfileLease()
    worker = LauncherRunWorker(
        FlowExecutor(),
        AutomationFlow("Quarantined launcher"),
        RunConfig(),
        AndroidDevice("launcher-quarantined-device"),
        DeviceSessionService(),
        profile_lease=profile_lease,  # type: ignore[arg-type]
    )
    worker._profile_release_permitted = False
    service.register_run("quarantined-run", worker)
    panel = RunPanel(
        "Quarantined setup",
        "quarantined-run",
        worker,
        on_close=view._close_run_panel,
        parent=view,
    )
    view._runner_service = service
    view._run_panels["quarantined-run"] = panel

    try:
        with patch.object(QMessageBox, "information"):
            panel._on_close_clicked()

        assert service.get_worker("quarantined-run") is worker
        assert view._run_panels["quarantined-run"] is panel
        assert panel._status_badge.text() == "CLEANUP PENDING"
        assert profile_lease.calls == 0

        assert view.shutdown(wait_ms=0)
        assert service.get_worker("quarantined-run") is worker
        assert view._run_panels["quarantined-run"] is panel
        assert worker._profile_lease is profile_lease
        assert profile_lease.calls == 0
    finally:
        worker._profile_release_permitted = True
        assert worker.release_reserved_profile_lease()
        assert service.remove_run("quarantined-run")
        panel.deleteLater()
        view.deleteLater()
        application.processEvents()
        database.close()


def test_launcher_concurrency_precheck_precedes_profile_acquisition(
    application: QApplication,
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "launcher-concurrency-precheck.db"))
    flow = AutomationFlow(
        "Concurrency flow",
        [FlowCommand("wait", {"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    config = SavedRunConfig(
        name="Concurrency setup",
        flow_name=flow.name,
        device_id="emulator-concurrency",
    )
    view = LauncherView(database, DeviceSessionService())
    service = RunnerService(max_concurrent=1)
    retained = LauncherRunWorker(
        FlowExecutor(),
        AutomationFlow("Retained"),
        RunConfig(),
        AndroidDevice("retained-device"),
        DeviceSessionService(),
    )
    service.register_run("retained", retained)
    view._runner_service = service
    view._selected_config = lambda: config

    try:
        with (
            patch(
                "automadin.execution.run_coordinator.launcher_preflight_report"
            ),
            patch(
                "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
                return_value=True,
            ),
            patch(
                "automadin.execution.run_coordinator.acquire_profile_lease"
            ) as acquire_profile,
            patch.object(QMessageBox, "critical") as critical,
        ):
            view._on_run()

        acquire_profile.assert_not_called()
        assert service.get_worker("retained") is retained
        assert len(service._workers) == 1
        assert critical.call_args.args[1] == "Error"
        assert "Maximum concurrent runs" in critical.call_args.args[2]
    finally:
        assert service.remove_run("retained")
        view.deleteLater()
        application.processEvents()
        database.close()


def test_launcher_register_rejection_never_acquires_profile(
    application: QApplication,
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "launcher-register-rejection.db"))
    flow = AutomationFlow(
        "Register rejection",
        [FlowCommand("wait", {"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    config = SavedRunConfig(
        name="Rejected setup",
        flow_name=flow.name,
        device_id="emulator-register-rejection",
    )
    view = LauncherView(database, DeviceSessionService())
    view._selected_config = lambda: config

    try:
        with (
            patch(
                "automadin.execution.run_coordinator.launcher_preflight_report"
            ),
            patch(
                "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
                return_value=True,
            ),
            patch.object(
                view._runner_service,
                "register_run",
                side_effect=RuntimeError("concurrency changed"),
            ),
            patch(
                "automadin.execution.run_coordinator.acquire_profile_lease"
            ) as acquire_profile,
            patch.object(QMessageBox, "critical"),
        ):
            view._on_run()

        acquire_profile.assert_not_called()
        assert not view._runner_service._workers
        assert not view._run_panels
    finally:
        view.deleteLater()
        application.processEvents()
        database.close()


def test_launcher_device_conflict_retains_panel_until_profile_release_retries(
    application: QApplication,
    tmp_path,
) -> None:
    class _TransientProfileLease:
        profile_id = "device-conflict-profile"

        def __init__(self) -> None:
            self.calls = 0

        def release(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient profile cleanup failure")
            return True

    database = DatabaseService(str(tmp_path / "launcher-device-conflict.db"))
    flow = AutomationFlow(
        "Conflict flow",
        [FlowCommand("wait", {"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    config = SavedRunConfig(
        name="Conflict setup",
        flow_name=flow.name,
        device_id="emulator-conflict",
    )
    view = LauncherView(database, DeviceSessionService())
    view._selected_config = lambda: config
    profile_lease = _TransientProfileLease()
    current = DeviceOperationUse("flow-run", "Existing run", "existing")
    requested = DeviceOperationUse("flow-run", "Conflict setup", "requested")
    conflict = DeviceOperationConflictError(
        "android:emulator-conflict",
        current,
        requested,
    )

    try:
        with (
            patch(
                "automadin.execution.run_coordinator.launcher_preflight_report"
            ),
            patch(
                "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
                return_value=True,
            ),
            patch(
                "automadin.execution.run_coordinator.acquire_profile_lease",
                return_value=profile_lease,
            ),
            patch.object(
                LauncherRunWorker,
                "reserve_device_operation",
                side_effect=conflict,
            ),
            patch.object(QMessageBox, "critical"),
            patch.object(QMessageBox, "information"),
        ):
            view._on_run()

        assert len(view._runner_service._workers) == 1
        run_id, worker = next(iter(view._runner_service._workers.items()))
        panel = view._run_panels[run_id]
        assert worker._profile_lease is profile_lease
        assert profile_lease.calls == 1
        assert panel._status_badge.text() == "CLEANUP PENDING"

        panel._on_close_clicked()

        assert not view._runner_service._workers
        assert not view._run_panels
        assert worker._profile_lease is None
        assert profile_lease.calls == 2
    finally:
        for run_id in list(view._runner_service._workers):
            worker = view._runner_service.get_worker(run_id)
            if isinstance(worker, LauncherRunWorker):
                worker._profile_release_permitted = True
                worker.release_reserved_profile_lease()
            view._runner_service.remove_run(run_id)
        view.deleteLater()
        application.processEvents()
        database.close()


def test_flow_runner_surfaces_unavailable_preview_and_error(
    application: QApplication,
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "runner-preview.db"))
    view = FlowRunnerView(database, DeviceSessionService())
    try:
        view._on_screenshot_updated(b"not a valid image")
        view._on_error("device command failed")

        assert (
            view._screenshot_info_label.text()
            == "Preview unavailable — last valid screenshot retained"
        )
        log_text = view._log_view.toPlainText()
        assert "Post-step screenshot could not be displayed" in log_text
        assert "ERROR: device command failed" in log_text
    finally:
        view.deleteLater()
        application.processEvents()
        database.close()


def test_log_headers_use_switches_and_consistent_action_sizing(
    application: QApplication,
    tmp_path,
) -> None:
    previous_stylesheet = application.styleSheet()
    application.setStyleSheet(get_stylesheet())
    database = DatabaseService(str(tmp_path / "log-controls.db"))
    runner_view = FlowRunnerView(database, DeviceSessionService())
    worker, _entered, _release = _blocking_worker(session_id="log-controls-session")
    run_panel = RunPanel("Log controls", "log-controls-run", worker)
    try:
        runner_view.show()
        run_panel.show()
        application.processEvents()

        assert isinstance(runner_view._tail_log_btn, ToggleSwitch)
        assert runner_view._tail_log_btn.isChecked()
        assert isinstance(runner_view._log_granularity_combo, QComboBox)
        assert runner_view._log_granularity() is LogGranularity.NORMAL
        assert runner_view._log_granularity_combo.property("controlDensity") == "compact"
        assert runner_view._log_granularity_combo.minimumContentsLength() == len("Detailed")
        assert runner_view._clear_log_btn.minimumWidth() >= 64
        assert runner_view._clear_log_btn.height() >= runner_view._clear_log_btn.sizeHint().height()

        assert isinstance(run_panel._tail_btn, ToggleSwitch)
        assert run_panel._tail_btn.isChecked()
        assert isinstance(run_panel._log_granularity_combo, QComboBox)
        assert run_panel._log_granularity() is LogGranularity.NORMAL
        assert run_panel._log_granularity_combo.property("controlDensity") == "compact"
        assert run_panel._log_granularity_combo.minimumContentsLength() == len("Detailed")
        assert (
            run_panel._log_granularity_combo.sizeHint().height()
            == run_panel._pause_btn.sizeHint().height()
        )

        runner_view._on_detailed_log("hidden runner detail")
        run_panel._append_detailed_log("hidden panel detail")
        assert "hidden runner detail" not in runner_view._log_view.toPlainText()
        assert "hidden panel detail" not in run_panel._log_area.toPlainText()

        runner_view._log_granularity_combo.setCurrentIndex(
            runner_view._log_granularity_combo.findData(LogGranularity.DETAILED)
        )
        run_panel._log_granularity_combo.setCurrentIndex(
            run_panel._log_granularity_combo.findData(LogGranularity.DETAILED)
        )
        runner_view._on_detailed_log("visible runner detail")
        run_panel._append_detailed_log("visible panel detail")
        runner_view._on_trace_log("hidden runner trace")
        run_panel._append_trace_log("hidden panel trace")
        assert "visible runner detail" in runner_view._log_view.toPlainText()
        assert "visible panel detail" in run_panel._log_area.toPlainText()
        assert "hidden runner trace" not in runner_view._log_view.toPlainText()
        assert "hidden panel trace" not in run_panel._log_area.toPlainText()

        runner_view._log_granularity_combo.setCurrentIndex(
            runner_view._log_granularity_combo.findData(LogGranularity.TRACE)
        )
        run_panel._log_granularity_combo.setCurrentIndex(
            run_panel._log_granularity_combo.findData(LogGranularity.TRACE)
        )
        runner_view._on_trace_log("visible runner trace")
        run_panel._append_trace_log("visible panel trace")
        assert "visible runner trace" in runner_view._log_view.toPlainText()
        assert "visible panel trace" in run_panel._log_area.toPlainText()
        for button in (
            run_panel._pause_btn,
            run_panel._stop_btn,
            run_panel._restart_btn,
            run_panel._clear_btn,
        ):
            assert button.minimumWidth() >= 64
            assert button.width() >= button.sizeHint().width()
            assert button.height() >= button.sizeHint().height()

        run_panel.resize(300, run_panel.height())
        application.processEvents()
        action_buttons = (
            run_panel._pause_btn,
            run_panel._stop_btn,
            run_panel._restart_btn,
            run_panel._clear_btn,
        )
        assert len({button.y() for button in action_buttons}) > 1
        for button in action_buttons:
            assert run_panel._controls_widget.rect().contains(button.geometry())
    finally:
        run_panel.deleteLater()
        runner_view.deleteLater()
        application.processEvents()
        application.setStyleSheet(previous_stylesheet)
        database.close()


def test_run_panel_log_expands_with_available_height(
    application: QApplication,
) -> None:
    worker, _entered, _release = _blocking_worker(session_id="expanding-log-session")
    panel = RunPanel("Expanding log", "expanding-log-run", worker)
    try:
        assert panel._log_area.minimumHeight() == 150
        assert panel._log_area.maximumHeight() > 300
        assert panel.layout().stretch(2) == 1
    finally:
        panel.deleteLater()
        application.processEvents()


def test_run_panel_close_returns_promptly_and_defers_removal_until_finish(
    application: QApplication,
) -> None:
    worker, entered, release = _blocking_worker(session_id="panel-close-session")
    closed: list[str] = []
    panel = RunPanel(
        "Blocking setup",
        "panel-run",
        worker,
        on_close=closed.append,
    )
    worker.start()

    try:
        assert entered.wait(1)
        started_at = time.monotonic()
        panel._on_close_clicked()
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.25
        assert worker.isRunning()
        assert worker.ctx.is_stopped
        assert closed == []
        assert panel._close_requested
        assert not panel._close_btn.isEnabled()
        assert panel._stop_btn.text() == "Stopping…"
        assert not panel._stop_btn.isEnabled()
        assert panel._status_badge.text() == "STOPPING"

        panel._on_error("saved run failed")
        assert "ERROR: saved run failed" in panel._log_area.toPlainText()

        release.set()
        _wait_until(application, lambda: closed == ["panel-run"])

        assert not worker.isRunning()
        assert not panel._stop_btn.isEnabled()
        assert panel._stop_btn.text() == "Stopped"
        assert panel._status_badge.text() == "STOPPED"
    finally:
        release.set()
        worker.wait(2000)
        panel.deleteLater()
        application.processEvents()


def test_run_panel_enables_and_resolves_interactive_attention(
    application: QApplication,
) -> None:
    worker, _entered, _release = _blocking_worker(session_id="panel-attention-session")
    panel = RunPanel("Interactive setup", "panel-attention", worker)
    request = AttentionRequest(
        "Complete sign-in, then continue.",
        "user_checkpoint",
        "step",
        (AttentionResolution.CONTINUE, AttentionResolution.STOP),
    )

    try:
        assert worker.attention_enabled
        with patch(
            "automadin.ui.launcher.run_panel.prompt_for_attention",
            return_value=AttentionResolution.CONTINUE,
        ):
            panel._on_attention_required(request)

        assert request.resolution is AttentionResolution.CONTINUE
        assert "ACTION REQUIRED" in panel._log_area.toPlainText()
    finally:
        panel.deleteLater()
        application.processEvents()


def test_log_tail_off_preserves_scroll_position(application: QApplication) -> None:
    view = QTextEdit()
    view.resize(500, 180)
    view.show()
    try:
        for index in range(80):
            append_semantic_log(view, f"line {index}")
        application.processEvents()
        scroll_bar = view.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum() // 3)
        anchored_position = scroll_bar.value()

        append_semantic_log(view, "new line while reviewing", follow=False)
        application.processEvents()

        assert scroll_bar.value() == anchored_position
        assert scroll_bar.value() < scroll_bar.maximum()
    finally:
        view.close()
        view.deleteLater()
        application.processEvents()


def test_run_panel_run_again_reuses_panel_with_fresh_worker(
    application: QApplication,
) -> None:
    first = FlowRunWorker(
        FlowExecutor(),
        AutomationFlow("First attempt"),
        FlowContext(DeviceSession()),
        RunConfig(),
        device_operation_registry=DeviceOperationLeaseRegistry(),
    )
    replacements: list[FlowRunWorker] = []

    def restart(panel: RunPanel) -> bool:
        replacement = FlowRunWorker(
            FlowExecutor(),
            AutomationFlow("Second attempt"),
            FlowContext(DeviceSession()),
            RunConfig(),
            device_operation_registry=DeviceOperationLeaseRegistry(),
        )
        replacements.append(replacement)
        panel.rebind("attempt-2", replacement)
        replacement.start()
        panel.commit_rebind()
        return True

    panel = RunPanel(
        "Reusable setup",
        "attempt-1",
        first,
        run_config_id=7,
        on_restart=restart,
    )
    first.start()
    try:
        _wait_until(application, first.isFinished)
        assert panel._restart_btn.isEnabled()

        original_panel = panel
        panel._restart_btn.click()
        _wait_until(application, lambda: bool(replacements))
        _wait_until(application, replacements[0].isFinished)

        assert panel is original_panel
        assert panel.run_id == "attempt-2"
        assert "Run again" in panel._log_area.toPlainText()
        assert "Previous result: Completed" in panel._log_area.toPlainText()
        assert panel._restart_btn.isEnabled()
    finally:
        first.wait(2000)
        for replacement in replacements:
            replacement.wait(2000)
        panel.deleteLater()
        application.processEvents()


def test_run_panel_pause_button_switches_to_resume(
    application: QApplication,
) -> None:
    worker, entered, release = _blocking_worker(session_id="panel-pause-session")
    panel = RunPanel("Pausable setup", "pause-run", worker)
    worker.start()
    try:
        assert entered.wait(1)
        _wait_until(application, lambda: panel._pause_btn.isEnabled())

        panel._pause_btn.click()
        application.processEvents()
        assert worker.status.value == "pausing"
        assert panel._pause_btn.text() == "Pausing…"
        assert not panel._pause_btn.isEnabled()

        worker._pause_reached()
        application.processEvents()
        assert panel._pause_btn.text() == "Resume"
        assert panel._pause_btn.isEnabled()

        panel._pause_btn.click()
        application.processEvents()
        assert worker.status.value == "running"
        assert panel._pause_btn.text() == "Pause"
    finally:
        worker.request_stop()
        release.set()
        worker.wait(2000)
        panel.deleteLater()
        application.processEvents()
