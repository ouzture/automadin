from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.engine.session import DeviceSessionService
from automadin.execution.build_executor import build_executor
from automadin.execution.context import FlowContext
from automadin.execution.flow_values import FlowInvocationValues
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
    WindowsDevice,
)
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.run_config import RunStatus, SavedRunConfig
from automadin.runtime import RuntimeComponentStatus
from automadin.services.database import DatabaseService
from automadin.services.flow_preflight import FlowPreflightError
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.ui.flow_editor.editor_view import FlowEditorView
from automadin.ui.flow_runner.runner_view import FlowRunnerView
from automadin.ui.launcher.launcher_view import (
    LauncherView,
    _launcher_preflight_report,
)


class _SelectedSession:
    def __init__(self, session: DeviceSession) -> None:
        self._session = session

    def get_selected_session(self) -> DeviceSession:
        return self._session


@pytest.fixture(scope="module", autouse=True)
def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def database(tmp_path) -> DatabaseService:
    service = DatabaseService(str(tmp_path / "preflight-integration.db"))
    yield service
    service.close()


def _android_session() -> DeviceSession:
    return DeviceSession(device=AndroidDevice("emulator-5554"))


def test_editor_save_preflights_before_changing_identity_or_persisting(
    database: DatabaseService,
) -> None:
    editor = FlowEditorView(database, DeviceSessionService())
    editor._flow.commands = [FlowCommand("navigate_to", params={"url": ""})]
    captured = []

    with (
        patch(
            "automadin.ui.flow_editor.editor_view.ResourcePathDialog.get_path",
            return_value="Drafts/Broken",
        ),
        patch(
            "automadin.ui.flow_editor.editor_view.confirm_document_preflight",
            side_effect=lambda _parent, report, **_kwargs: captured.append(report) or False,
        ),
    ):
        assert not editor._save_flow()

    assert captured and captured[0].errors
    assert editor._flow.name == "Untitled"
    assert database.load_flow("Drafts/Broken") is None
    editor.deleteLater()


def test_editor_can_explicitly_save_a_semantically_invalid_draft(
    database: DatabaseService,
) -> None:
    editor = FlowEditorView(database, DeviceSessionService())
    editor._flow.commands = [FlowCommand("navigate_to", params={"url": ""})]

    with (
        patch(
            "automadin.ui.flow_editor.editor_view.ResourcePathDialog.get_path",
            return_value="Drafts/Repair Me",
        ),
        patch(
            "automadin.ui.flow_editor.editor_view.confirm_document_preflight",
            return_value=True,
        ),
    ):
        assert editor._save_flow()

    assert database.load_flow("Drafts/Repair Me") is not None
    editor.deleteLater()


def test_run_step_blocks_an_unsupported_target_before_building_executor(
    database: DatabaseService,
) -> None:
    selector = _SelectedSession(_android_session())
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=selector,
    )
    get_executor = Mock()
    editor._get_executor = get_executor
    captured = []

    with patch(
        "automadin.ui.flow_editor.editor_view.confirm_execution_preflight",
        side_effect=lambda _parent, report, **_kwargs: captured.append(report) or False,
    ):
        editor.run_single_command(
            FlowCommand("navigate_to", params={"url": "https://example.com"})
        )

    assert captured
    assert any(issue.code == "UNSUPPORTED_TARGET" for issue in captured[0].errors)
    get_executor.assert_not_called()
    editor.deleteLater()


def test_run_step_preserves_typed_integer_literals_from_current_flow(
    database: DatabaseService,
) -> None:
    command = FlowCommand(
        "win_click",
        params={
            "coordinate_unit": "percent",
            "x": {"kind": "literal", "type": "integer", "value": 14},
            "y": {"kind": "literal", "type": "integer", "value": 8},
        },
    )
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=_SelectedSession(
            DeviceSession(device=WindowsDevice(1234, "Test Window"))
        ),
    )
    editor._flow.commands = [command]
    editor._executor = Mock()
    editor._run_step_controller.start = Mock(return_value="operation")
    reports = []

    with patch(
        "automadin.ui.flow_editor.editor_view.confirm_execution_preflight",
        side_effect=lambda _parent, report, **_kwargs: reports.append(report) or True,
    ):
        editor.run_single_command(command)

    assert reports and not reports[0].errors
    start = editor._run_step_controller.start
    assert start.call_count == 1
    assert start.call_args.kwargs["context"].flow_values is not None
    editor.deleteLater()


def test_run_step_rechecks_command_after_confirmation_before_dispatch(
    database: DatabaseService,
) -> None:
    command = FlowCommand("wait", params={"ms": 1})
    handler = Mock()
    executor = build_executor(db=database)
    executor.register_handler("wait", handler)
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=_SelectedSession(_android_session()),
    )
    editor._executor = executor

    def mutate_after_review(_parent, report, **_kwargs):
        assert not report.errors
        command.params["pause_ms"] = -1
        return True

    with (
        patch(
            "automadin.ui.flow_editor.editor_view.confirm_execution_preflight",
            side_effect=mutate_after_review,
        ),
        patch(
            "automadin.ui.flow_editor.editor_view.QMessageBox.critical"
        ) as critical,
    ):
        editor.run_single_command(command)

    handler.assert_not_called()
    critical.assert_called_once()
    assert "cannot be negative" in critical.call_args.args[2]
    editor.deleteLater()


def test_debugger_run_blocks_before_worker_registration(
    database: DatabaseService,
) -> None:
    database.save_flow(
        "Browser Only",
        AutomationFlow(
            "Browser Only",
            [FlowCommand("navigate_to", params={"url": "https://example.com"})],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        ),
    )
    runner = FlowRunnerView(
        database,
        DeviceSessionService(),
        device_selector=_SelectedSession(_android_session()),
    )
    captured = []

    with patch(
        "automadin.ui.flow_runner.runner_view.confirm_execution_preflight",
        side_effect=lambda _parent, report, **_kwargs: captured.append(report) or False,
    ):
        runner._start_run()

    assert captured
    assert any(issue.code == "UNSUPPORTED_TARGET" for issue in captured[0].errors)
    assert runner._current_worker is None
    assert not runner._runner_service._workers
    runner.deleteLater()


def test_browser_debugger_next_step_resumes_initial_prepaused_worker(
    database: DatabaseService,
) -> None:
    database.save_flow(
        "Browser Debug",
        AutomationFlow(
            "Browser Debug",
            [FlowCommand("navigate_to", params={"url": "https://example.com"})],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        ),
    )
    session = DeviceSession(
        device=BrowserDevice(
            config=BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM)
        ),
        data={"browser_data": object()},
    )
    runner = FlowRunnerView(
        database,
        DeviceSessionService(),
        device_selector=_SelectedSession(session),
    )

    with (
        patch(
            "automadin.ui.flow_runner.runner_view.confirm_execution_preflight",
            return_value=True,
        ),
        patch("automadin.ui.flow_runner.runner_view.BrowserAwareRunWorker.start"),
    ):
        runner._start_run(step_through=True)

    worker = runner._current_worker
    run_id = runner._current_run_id
    assert worker is not None
    assert run_id is not None
    assert worker.status is RunStatus.PAUSING
    assert not worker.ctx._paused.is_set()

    runner._step_once()

    assert worker.status is RunStatus.RUNNING
    assert worker.ctx._paused.is_set()
    assert runner._runner_service.remove_run(run_id)
    runner.deleteLater()


def test_launcher_blocks_before_profile_lease_or_worker_allocation(
    database: DatabaseService,
) -> None:
    database.save_flow(
        "Browser Only",
        AutomationFlow(
            "Browser Only",
            [FlowCommand("navigate_to", params={"url": "https://example.com"})],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        ),
    )
    config = SavedRunConfig(
        name="Android run",
        flow_name="Browser Only",
        device_id="emulator-5554",
    )
    launcher = LauncherView(database, session_service=SimpleNamespace())
    launcher._selected_config = lambda: config
    captured = []

    with (
        patch(
            "automadin.execution.run_coordinator.device_runtime_status",
            return_value=RuntimeComponentStatus(
                source="test",
                path=None,
                usable=True,
            ),
        ),
        patch(
            "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
            side_effect=lambda _parent, report, **_kwargs: captured.append(report) or False,
        ),
        patch(
            "automadin.execution.run_coordinator.acquire_profile_lease"
        ) as acquire,
    ):
        launcher._on_run()

    assert captured
    assert any(issue.code == "UNSUPPORTED_TARGET" for issue in captured[0].errors)
    acquire.assert_not_called()
    assert not launcher._runner_service._workers
    launcher.deleteLater()


def test_launcher_preflight_reports_an_unavailable_target_runtime(
    database: DatabaseService,
) -> None:
    flow = AutomationFlow(
        "Ready Flow",
        [FlowCommand("wait", params={"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(
            source="unavailable",
            path=None,
            usable=False,
            error="ADB is not installed.",
        ),
    ):
        report = _launcher_preflight_report(
            db=database,
            flow=flow,
            device=AndroidDevice("emulator-5554"),
            session_service=SimpleNamespace(),
        )

    runtime_errors = [
        issue for issue in report.errors if issue.code == "RUNTIME_UNAVAILABLE"
    ]
    assert len(runtime_errors) == 1
    assert "ADB is not installed" in runtime_errors[0].message


def test_launcher_preflight_reports_a_profile_lease_conflict(
    database: DatabaseService,
) -> None:
    profile_id = str(uuid4())
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(
        profile_id,
        ProfileUse("interactive", "Connected browser", "test-owner"),
    )
    device = BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_id=profile_id,
        )
    )
    try:
        with patch(
            "automadin.execution.run_coordinator.device_runtime_status",
            return_value=RuntimeComponentStatus(
                source="test",
                path=None,
                usable=True,
            ),
        ):
            report = _launcher_preflight_report(
                db=database,
                flow=AutomationFlow(
                    "Ready Flow",
                    [FlowCommand("wait", params={"ms": 1})],
                    value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
                ),
                device=device,
                session_service=SimpleNamespace(
                    browser_profile_lease_registry=registry
                ),
                run_setup_name="Rewards - Complete",
            )
        conflicts = [issue for issue in report.errors if issue.code == "PROFILE_IN_USE"]
        assert len(conflicts) == 1
        assert "Connected browser" in conflicts[0].message
        assert "Run setup 'Rewards - Complete'" in conflicts[0].location.display_text
        assert "browser profile" in conflicts[0].location.display_text
        assert profile_id in conflicts[0].location.display_text
        assert conflicts[0].remediation == (
            "Stop or disconnect the named browser operation, then try again."
        )
    finally:
        owner.release()


def test_launcher_handles_profile_lease_acquired_during_confirmation(
    database: DatabaseService,
) -> None:
    flow = AutomationFlow(
        "Browser Ready",
        [FlowCommand("wait", params={"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    profile_id = str(uuid4())
    device = BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_id=profile_id,
        )
    )
    config = SavedRunConfig(
        name="Browser race",
        flow_name=flow.name,
        device_id="patched-browser-device",
    )
    registry = BrowserProfileLeaseRegistry()
    launcher = LauncherView(
        database,
        session_service=SimpleNamespace(
            browser_profile_lease_registry=registry,
        ),
    )
    launcher._selected_config = lambda: config
    competing_owner = None

    def acquire_competing_owner(_parent, report, **_kwargs):
        nonlocal competing_owner
        assert not report.errors
        competing_owner = registry.acquire_owner(
            profile_id,
            ProfileUse("interactive", "Opened during confirmation", "race-owner"),
        )
        return True

    try:
        with (
            patch(
                "automadin.execution.run_coordinator.device_from_run_config",
                return_value=device,
            ),
            patch(
                "automadin.execution.run_coordinator.profile_reference_error",
                return_value=None,
            ),
            patch(
                "automadin.execution.run_coordinator.device_runtime_status",
                return_value=RuntimeComponentStatus(
                    source="test",
                    path=None,
                    usable=True,
                ),
            ),
            patch(
                "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
                side_effect=acquire_competing_owner,
            ),
            patch(
                "automadin.ui.launcher.launcher_view.QMessageBox.critical"
            ) as critical,
        ):
            launcher._on_run()

        assert competing_owner is not None
        assert registry.snapshot(profile_id) is not None
        assert not launcher._runner_service._workers
        assert not launcher._run_panels
        assert critical.call_args.args[1] == "Profile Already in Use"
    finally:
        if competing_owner is not None:
            competing_owner.release()
        launcher.deleteLater()


def test_launcher_cleans_up_when_worker_start_fails_after_confirmation(
    database: DatabaseService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    flow = AutomationFlow(
        "Android Ready",
        [FlowCommand("wait", params={"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    config = SavedRunConfig(
        name="Runtime race",
        flow_name=flow.name,
        device_id="emulator-5554",
    )
    launcher = LauncherView(
        database,
        session_service=DeviceSessionService(),
    )
    launcher._selected_config = lambda: config

    with (
        patch(
            "automadin.execution.run_coordinator.device_runtime_status",
            return_value=RuntimeComponentStatus(
                source="test",
                path=None,
                usable=True,
            ),
        ),
        patch(
            "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
            return_value=True,
        ),
        patch(
            "automadin.ui.launcher.launcher_view.LauncherRunWorker.start",
            side_effect=RuntimeError("ADB disappeared after confirmation"),
        ),
        patch(
            "automadin.ui.launcher.launcher_view.QMessageBox.critical"
        ) as critical,
        caplog.at_level(
            logging.ERROR,
            logger="automadin.ui.launcher.launcher_view",
        ),
    ):
        launcher._on_run()

    assert not launcher._runner_service._workers
    assert not launcher._run_panels
    assert critical.call_args.args[1] == "Run Could Not Start"
    assert critical.call_args.args[2] == (
        "The run could not be started. Review Diagnostics for details, then try again."
    )
    assert "ADB disappeared" not in critical.call_args.args[2]
    assert "ADB disappeared after confirmation" in caplog.text
    assert any(record.exc_info for record in caplog.records)
    launcher.deleteLater()


def test_standard_executor_fails_closed_when_ui_preflight_is_bypassed(
    database: DatabaseService,
) -> None:
    executor = build_executor(db=database)
    invalid = AutomationFlow(
        "Browser Only",
        [FlowCommand("navigate_to", params={"url": "https://example.com"})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )

    context = FlowContext(
        _android_session(),
        flow_values=FlowInvocationValues(
            invalid.flow_id,
            invalid.value_catalog,
            {},
        ),
    )
    with pytest.raises(FlowPreflightError) as exc_info:
        executor.execute(invalid, context)

    assert any(
        issue.code == "UNSUPPORTED_TARGET"
        for issue in exc_info.value.report.errors
    )
