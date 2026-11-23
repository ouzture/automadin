from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from automadin.engine.session import DeviceSessionService
from automadin.execution.executor import FlowExecutor
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.models.flow import FlowCommand
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import DeviceOperationLeaseRegistry
from automadin.ui.flow_editor.editor_view import FlowEditorView
from automadin.ui.flow_editor.run_step_controller import (
    RunStepOperationController,
    _RunStepWorker,
)


class _Selector:
    def __init__(self, session: DeviceSession) -> None:
        self._session = session

    def get_selected_session(self) -> DeviceSession:
        return self._session


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 2.0) -> None:
    app = _app()
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for Qt operation")
        time.sleep(0.005)


def test_editor_run_step_is_nonblocking_identified_and_cancellable(
    tmp_path: Path,
) -> None:
    app = _app()
    database = DatabaseService(str(tmp_path / "editor-run-step.db"))
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=_Selector(session),
        run_step_controller=controller,
    )
    active = FlowCommand("wait", params={"ms": 1})
    other = FlowCommand("wait", params={"ms": 1})
    editor._flow.commands = [active, other]
    editor._rebuild_cards()
    entered = threading.Event()
    release_after_cancel = threading.Event()
    worker_thread_ids: list[int] = []
    executor = FlowExecutor()

    def wait_until_cancelled(_command, context) -> None:
        worker_thread_ids.append(threading.get_ident())
        entered.set()
        while not context.is_stopped:
            time.sleep(0.005)
        release_after_cancel.wait(1)

    executor.register_handler("wait", wait_until_cancelled)
    editor._executor = executor
    active_card = editor._card_by_step_id[active.step_id]
    other_card = editor._card_by_step_id[other.step_id]
    active_card._insert_btn.setEnabled(False)
    editable_controls = [
        active_card._enabled_btn,
        active_card._insert_btn,
        active_card._more_btn,
        active_card._drag_handle,
        active_card._comment_edit,
        active_card._params_container,
        active_card._error_container,
        *active_card._actions_menu.actions(),
    ]
    original_enabled_states = [
        control.isEnabled() for control in editable_controls
    ]

    try:
        with patch(
            "automadin.ui.flow_editor.editor_view.confirm_execution_preflight",
            return_value=True,
        ):
            started = time.monotonic()
            editor.run_single_command(active)
            dispatch_time = time.monotonic() - started

            assert dispatch_time < 0.1
            assert entered.wait(1)
            app.processEvents()
            assert controller.is_busy
            assert worker_thread_ids != [threading.get_ident()]
            assert active_card._run_btn.text() == "■"
            assert active_card._run_btn.isEnabled()
            assert "Stop running step" in active_card._run_btn.accessibleName()
            assert not other_card._run_btn.isEnabled()
            assert all(not control.isEnabled() for control in editable_controls)
            assert not active_card.acceptDrops()
            assert editor._run_step_status_label.text().startswith("Running:")

            active_card._on_enabled_toggle(False)
            active_card._on_insert_above()
            assert active.enabled
            assert editor._flow.commands == [active, other]
            assert active_card._run_btn.isEnabled()
            assert active_card._run_btn.text() == "■"

            editor.run_single_command(active)
            app.processEvents()
            assert controller.state == "cancel_requested"
            assert active_card._run_btn.text() == "…"
            assert not active_card._run_btn.isEnabled()
            assert all(not control.isEnabled() for control in editable_controls)
            assert editor._run_step_status_label.text().startswith("Stopping:")

        release_after_cancel.set()
        _wait_until(lambda: not controller.is_busy)
        assert active_card._run_btn.text() == "▶"
        assert active_card._run_btn.isEnabled()
        assert other_card._run_btn.isEnabled()
        assert [
            control.isEnabled() for control in editable_controls
        ] == original_enabled_states
        assert active_card.acceptDrops()
        assert editor._run_step_status_label.text().startswith("Stopped:")
    finally:
        release_after_cancel.set()
        assert controller.shutdown(1000)
        editor.deleteLater()
        controller.deleteLater()
        app.processEvents()
        database.close()


def test_screen_text_card_exposes_a_side_effect_free_preview(tmp_path: Path) -> None:
    app = _app()
    database = DatabaseService(str(tmp_path / "screen-text-preview.db"))
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=_Selector(session),
    )
    command = FlowCommand(
        "read_screen_text",
        params={"start_x": 0, "start_y": 0, "end_x": 10, "end_y": 10},
    )
    editor._flow.commands = [command]
    editor._rebuild_cards()
    card = editor._card_by_step_id[command.step_id]
    previews: list[FlowCommand] = []
    editor.preview_screen_text = previews.append  # type: ignore[method-assign]
    try:
        assert card._preview_btn.text() == "Preview"
        card._preview_btn.click()
        assert previews == [command]
    finally:
        editor.deleteLater()
        app.processEvents()
        database.close()


def test_editor_keeps_stopping_state_when_native_start_then_raises(
    tmp_path: Path,
) -> None:
    app = _app()
    database = DatabaseService(str(tmp_path / "editor-uncertain-start.db"))
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=_Selector(session),
        run_step_controller=controller,
    )
    command = FlowCommand("wait", params={"ms": 1})
    editor._flow.commands = [command]
    editor._rebuild_cards()
    entered = threading.Event()
    release = threading.Event()
    executor = FlowExecutor()
    real_start = _RunStepWorker.start

    def blocking_handler(_command, _context) -> None:
        entered.set()
        release.wait(1)

    def start_then_raise(worker: _RunStepWorker) -> None:
        real_start(worker)
        assert entered.wait(1)
        raise OSError("private post-start detail")

    executor.register_handler("wait", blocking_handler)
    editor._executor = executor

    try:
        with (
            patch(
                "automadin.ui.flow_editor.editor_view.confirm_execution_preflight",
                return_value=True,
            ),
            patch.object(_RunStepWorker, "start", start_then_raise),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(QMessageBox, "critical") as critical,
        ):
            editor.run_single_command(command)

        assert controller.is_busy
        assert controller.state == "cancel_requested"
        assert editor._run_step_status_label.text().startswith("Stopping:")
        warning.assert_called_once()
        assert warning.call_args.args[1] == "Run Step Is Stopping"
        assert "private post-start detail" not in warning.call_args.args[2]
        critical.assert_not_called()

        release.set()
        _wait_until(lambda: not controller.is_busy)
        assert editor._run_step_status_label.text().startswith("Stopped:")
    finally:
        release.set()
        assert controller.shutdown(1000)
        editor.deleteLater()
        controller.deleteLater()
        app.processEvents()
        database.close()


def test_command_card_symbol_buttons_have_dynamic_accessible_names(
    tmp_path: Path,
) -> None:
    app = _app()
    database = DatabaseService(str(tmp_path / "editor-accessibility.db"))
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=_Selector(session),
        run_step_controller=controller,
    )
    command = FlowCommand("wait", params={"ms": 1})
    editor._flow.commands = [command]
    editor._rebuild_cards()
    card = editor._card_by_step_id[command.step_id]

    try:
        assert card._expand_btn.accessibleName().startswith("Expand command:")
        assert card._expand_btn.accessibleDescription()
        assert card._insert_btn.accessibleName().startswith(
            "Insert command above:"
        )
        assert card._insert_btn.accessibleDescription()
        assert card._more_btn.accessibleName().startswith("Command actions:")
        assert card._more_btn.accessibleDescription()

        card._toggle_collapsed()

        assert card._expand_btn.accessibleName().startswith("Collapse command:")
        assert card._expand_btn.toolTip() == "Collapse command"
        assert "Hides" in card._expand_btn.accessibleDescription()
    finally:
        assert controller.shutdown(1000)
        editor.deleteLater()
        controller.deleteLater()
        app.processEvents()
        database.close()


def test_run_step_locks_flow_replacement_actions_and_restores_exact_states(
    tmp_path: Path,
) -> None:
    app = _app()
    database = DatabaseService(str(tmp_path / "editor-run-step-flow-locks.db"))
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=_Selector(session),
        run_step_controller=controller,
    )
    command = FlowCommand("wait", params={"ms": 1})
    editor._flow.commands = [command]
    editor._rebuild_cards()

    controls = editor._run_step_flow_mutation_controls
    controls[1].setEnabled(False)
    original_states = [control.isEnabled() for control in controls]

    try:
        editor._on_run_step_state_changed(
            "operation-1",
            command.step_id,
            "running",
        )
        assert all(not control.isEnabled() for control in controls)
        assert editor._card_by_step_id[command.step_id]._run_btn.isEnabled()

        editor._on_run_step_state_changed(
            "operation-1",
            command.step_id,
            "cancel_requested",
        )
        assert all(not control.isEnabled() for control in controls)

        editor._on_run_step_state_changed(
            "operation-1",
            command.step_id,
            "idle",
        )
        assert [control.isEnabled() for control in controls] == original_states
    finally:
        assert controller.shutdown(1000)
        editor.deleteLater()
        controller.deleteLater()
        app.processEvents()
        database.close()


def test_cleanup_retry_keeps_exact_editor_and_card_states_until_release(
    tmp_path: Path,
) -> None:
    app = _app()
    database = DatabaseService(str(tmp_path / "editor-cleanup-retry.db"))
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        device_selector=_Selector(session),
        run_step_controller=controller,
    )
    active = FlowCommand("wait", params={"ms": 1})
    other = FlowCommand("wait", params={"ms": 1})
    editor._flow.commands = [active, other]
    editor._rebuild_cards()
    active_card = editor._card_by_step_id[active.step_id]
    other_card = editor._card_by_step_id[other.step_id]
    active_card._insert_btn.setEnabled(False)
    card_controls = [
        active_card._enabled_btn,
        active_card._insert_btn,
        active_card._more_btn,
        active_card._drag_handle,
        active_card._comment_edit,
        active_card._params_container,
        active_card._error_container,
        *active_card._actions_menu.actions(),
    ]
    original_card_states = [
        control.isEnabled() for control in card_controls
    ]
    flow_controls = editor._run_step_flow_mutation_controls
    flow_controls[1].setEnabled(False)
    original_flow_states = [
        control.isEnabled() for control in flow_controls
    ]
    original_release = registry._release
    release_calls = 0

    def fail_release_twice(session_id, generation, token):
        nonlocal release_calls
        release_calls += 1
        if release_calls <= 2:
            raise RuntimeError("transient cleanup failure")
        return original_release(session_id, generation, token)

    try:
        with (
            patch(
                "automadin.ui.flow_editor.editor_view.confirm_execution_preflight",
                return_value=True,
            ),
            patch.object(registry, "_release", side_effect=fail_release_twice),
            patch.object(QMessageBox, "critical") as critical,
        ):
            editor.run_single_command(active)
            operation_id = controller.active_operation_id
            assert operation_id is not None
            _wait_until(lambda: controller.state == "cleanup_pending")

            assert controller.active_operation_id == operation_id
            assert controller.active_step_id == active.step_id
            assert active_card._run_btn.text() == "↻"
            assert active_card._run_btn.isEnabled()
            assert (
                "Retry cleanup"
                in active_card._run_btn.accessibleName()
            )
            assert not other_card._run_btn.isEnabled()
            assert all(not control.isEnabled() for control in card_controls)
            assert all(not control.isEnabled() for control in flow_controls)
            assert not active_card.acceptDrops()
            assert editor._run_step_status_label.text().startswith(
                "Cleanup pending:"
            )

            active_card._on_run_step()

        assert release_calls == 3
        assert not controller.is_busy
        assert active_card._run_btn.text() == "▶"
        assert active_card._run_btn.isEnabled()
        assert other_card._run_btn.isEnabled()
        assert [
            control.isEnabled() for control in card_controls
        ] == original_card_states
        assert [
            control.isEnabled() for control in flow_controls
        ] == original_flow_states
        assert active_card.acceptDrops()
        assert critical.call_count == 1
    finally:
        assert controller.shutdown(1000)
        editor.deleteLater()
        controller.deleteLater()
        app.processEvents()
        database.close()
