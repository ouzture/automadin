from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.flow_values import FlowInvocationValues
from automadin.execution.runner import BrowserAwareRunWorker
from automadin.engine.adb_engine import (
    AndroidTargetReadinessError,
    AndroidTargetReadinessKind,
)
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
)
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.run_config import BrowserRunMode, RunConfig, RunStatus, SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.ui.launcher.launcher_view import (
    ConnectedTargetRunWorker,
    LauncherView,
    _launcher_worker_for_saved_config,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _flow(name: str = "Mobile/Quick") -> AutomationFlow:
    return AutomationFlow(
        name,
        [FlowCommand("comment")],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )


def test_quick_run_registers_connected_worker_without_saving_setup(
    tmp_path,
    monkeypatch,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "quick-run.db"))
    flow = _flow()
    database.save_flow(flow.name, flow)
    session = DeviceSession(device=AndroidDevice("usb-1", model="Pixel"), data={})

    class Sessions:
        def ensure_android_session(self, device: AndroidDevice) -> DeviceSession:
            assert device is session.device
            return session

    monkeypatch.setattr(
        "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(ConnectedTargetRunWorker, "reserve_device_operation", lambda _self: None)
    monkeypatch.setattr(ConnectedTargetRunWorker, "start", lambda _self: None)
    view = LauncherView(database, Sessions())

    run_id = view._start_quick_run(session, flow.name)

    assert run_id is not None
    assert database.list_run_configs() == []
    worker = view.runner_service.get_worker(run_id)
    assert isinstance(worker, ConnectedTargetRunWorker)
    assert worker.ctx.session is session
    assert worker.run_display_name == "Quick Run · Mobile/Quick"
    assert worker.target_display_name == session.device.name

    assert view._close_run_panel(run_id)
    view.close()
    view.deleteLater()
    database.close()


def test_connected_android_worker_rechecks_readiness_without_closing_session() -> None:
    session = DeviceSession(device=AndroidDevice("usb-1", model="Pixel"), data={})
    readiness_calls: list[AndroidDevice] = []

    class Sessions:
        def ensure_android_session(self, device: AndroidDevice) -> DeviceSession:
            readiness_calls.append(device)
            return session

    executor = FlowExecutor()
    executor.register_handler("comment", lambda _command, _context: None)
    flow = _flow("Quick")
    worker = ConnectedTargetRunWorker(
        executor,
        flow,
        FlowContext(
            session,
            flow_values=FlowInvocationValues(flow.flow_id, flow.value_catalog, {}),
        ),
        RunConfig(),
        session_service=Sessions(),
    )

    worker.run()

    assert readiness_calls == [session.device]
    assert worker.ctx.session is session
    assert worker.status is RunStatus.COMPLETED


def test_connected_android_readiness_failure_releases_reserved_operation(
    monkeypatch,
) -> None:
    session = DeviceSession(device=AndroidDevice("usb-1", model="Pixel"), data={})

    class Sessions:
        def ensure_android_session(self, _device: AndroidDevice) -> DeviceSession:
            raise AndroidTargetReadinessError(
                AndroidTargetReadinessKind.OFFLINE,
                "usb-1",
                "Android target is offline.",
                "Reconnect it.",
            )

    flow = _flow("Quick")
    worker = ConnectedTargetRunWorker(
        FlowExecutor(),
        flow,
        FlowContext(
            session,
            flow_values=FlowInvocationValues(flow.flow_id, flow.value_catalog, {}),
        ),
        RunConfig(),
        session_service=Sessions(),
    )
    released: list[bool] = []
    monkeypatch.setattr(
        worker,
        "release_reserved_device_operation",
        lambda: released.append(True) or True,
    )

    worker.run()

    assert released == [True]
    assert worker.status is RunStatus.ERROR


def test_quick_run_keeps_browser_aware_worker_defaults_compatible() -> None:
    # The connected-browser worker remains constructible with its historical
    # debugger defaults after Quick Run adds optional presentation labels.
    executor = FlowExecutor()
    flow = _flow("Browser Quick")
    session = DeviceSession(device=AndroidDevice("placeholder"), data={})

    worker = BrowserAwareRunWorker(executor, flow, FlowContext(session), RunConfig())

    assert worker.status is RunStatus.IDLE


def test_saved_connected_browser_uses_browser_aware_worker_and_live_session() -> None:
    executor = FlowExecutor()
    flow = _flow("Scheduled Browser")
    device = BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    session = DeviceSession(device=device, data={"browser_data": object()})
    setup = SavedRunConfig(
        name="Reuse browser",
        flow_name=flow.name,
        device_id=device.id,
        browser_run_mode=BrowserRunMode.CONNECTED,
    )

    worker = _launcher_worker_for_saved_config(
        executor=executor,
        flow=flow,
        saved_config=setup,
        device=device,
        session_service=object(),
        connected_session=session,
        input_values={},
    )

    assert isinstance(worker, BrowserAwareRunWorker)
    assert worker.ctx.session is session
