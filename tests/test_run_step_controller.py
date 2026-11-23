from __future__ import annotations

import threading
import time
from types import MethodType
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from automadin.engine.browser_session import BrowserSessionHandle
from automadin.execution.context import FlowContext
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
    WindowsDevice,
)
from automadin.models.flow import FlowCommand
from automadin.services.device_operation_leases import (
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    device_operation_key,
)
from automadin.ui.flow_editor.run_step_controller import (
    RunStepBusyError,
    RunStepOperationController,
    RunStepStartError,
    _RunStepWorker,
)


@pytest.fixture
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(
    application: QApplication,
    predicate,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for Run Step operation")
        application.processEvents()
        time.sleep(0.005)
    application.processEvents()


def _android_session(session_id: str = "android-session") -> DeviceSession:
    return DeviceSession(
        session_id=session_id,
        device=AndroidDevice("emulator-5554"),
        data={"adb_data": {"device_id": "emulator-5554", "adb_path": "adb"}},
    )


def _windows_session(session_id: str = "windows-session") -> DeviceSession:
    return DeviceSession(
        session_id=session_id,
        device=WindowsDevice(1234, "Test Window"),
        data={"windows_data": {"hwnd": 1234}},
    )


def _browser_session(
    execute_command,
    session_id: str = "browser-session",
) -> DeviceSession:
    handle = object.__new__(BrowserSessionHandle)
    handle.execute_command = MethodType(execute_command, handle)
    return DeviceSession(
        session_id=session_id,
        device=BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM)),
        data={"browser_data": handle},
    )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.called = threading.Event()
        self.thread_id: int | None = None
        self.command: FlowCommand | None = None
        self.flow_name: str | None = None

    def execute_command(self, command, _ctx, *, flow_name) -> None:
        self.thread_id = threading.get_ident()
        self.command = command
        self.flow_name = flow_name
        self.called.set()


@pytest.mark.parametrize(
    "session_factory",
    [_android_session, _windows_session],
    ids=["android", "windows"],
)
def test_nonbrowser_targets_execute_off_gui_thread_and_release_lease(
    application: QApplication,
    session_factory,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    executor = _RecordingExecutor()
    session = session_factory()
    succeeded: list[tuple[str, str]] = []
    controller.succeeded.connect(lambda op, step: succeeded.append((op, step)))

    operation_id = controller.start(
        executor,
        FlowCommand("wait", step_id="step-1", params={"ms": 1}),
        session,
        "Flow A",
    )
    _wait_until(application, lambda: bool(succeeded))

    assert executor.called.is_set()
    assert executor.thread_id != threading.get_ident()
    assert succeeded == [(operation_id, "step-1")]
    assert registry.snapshot(device_operation_key(session.device)) is None
    assert controller.state == "idle"
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_browser_target_delegates_off_gui_thread_without_device_lease(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    delegated_thread: list[int] = []
    delegated_use = []

    def execute_command(
        _handle,
        executor,
        command,
        ctx,
        *,
        use,
        timeout,
    ) -> None:
        delegated_thread.append(threading.get_ident())
        delegated_use.append(use)
        assert timeout is None
        executor.execute_command(command, ctx, flow_name="browser-owner")

    session = _browser_session(execute_command)
    executor = _RecordingExecutor()
    controller = RunStepOperationController(lease_registry=registry)
    results: list[str] = []
    controller.succeeded.connect(lambda operation_id, _step: results.append(operation_id))

    operation_id = controller.start(
        executor,
        FlowCommand("navigate_to", step_id="browser-step"),
        session,
        "Browser Flow",
    )
    _wait_until(application, lambda: bool(results))

    assert results == [operation_id]
    assert delegated_thread[0] != threading.get_ident()
    assert delegated_use[0].operation_id == operation_id
    assert registry.active_count == 0
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_start_uses_deep_command_snapshot(
    application: QApplication,
) -> None:
    gate = threading.Event()
    captured: list[FlowCommand] = []

    class _GatedExecutor:
        def execute_command(self, command, _ctx, *, flow_name) -> None:
            assert flow_name == "Original Flow"
            gate.wait(1)
            captured.append(command)

    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    command = FlowCommand(
        "wait",
        step_id="snapshot-step",
        params={"ms": 10, "nested": {"value": "original"}},
    )
    controller.start(_GatedExecutor(), command, _android_session(), "Original Flow")
    command.id = "random_wait"
    command.params["ms"] = 999
    command.params["nested"]["value"] = "changed"
    gate.set()
    _wait_until(application, lambda: not controller.is_busy)

    assert captured[0].id == "wait"
    assert captured[0].step_id == "snapshot-step"
    assert captured[0].params == {"ms": 10, "nested": {"value": "original"}}
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_busy_controller_rejects_every_second_start(
    application: QApplication,
) -> None:
    release = threading.Event()

    class _BlockingExecutor:
        def execute_command(self, _command, _ctx, *, flow_name) -> None:
            release.wait(1)

    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    first_id = controller.start(
        _BlockingExecutor(),
        FlowCommand("wait", step_id="first"),
        _android_session(),
        "Flow",
    )

    with pytest.raises(RunStepBusyError, match="already active"):
        controller.start(
            _RecordingExecutor(),
            FlowCommand("wait", step_id="second"),
            _windows_session(),
            "Flow",
        )

    assert controller.active_operation_id == first_id
    release.set()
    _wait_until(application, lambda: not controller.is_busy)
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_cancel_is_exact_and_emits_one_cancelled_terminal(
    application: QApplication,
) -> None:
    started = threading.Event()

    class _CooperativeExecutor:
        def execute_command(self, _command, ctx, *, flow_name) -> None:
            started.set()
            while not ctx.is_stopped:
                time.sleep(0.005)

    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    states: list[str] = []
    cancelled: list[tuple[str, str]] = []
    controller.state_changed.connect(
        lambda _operation, _step, state: states.append(state)
    )
    controller.cancelled.connect(
        lambda operation, step: cancelled.append((operation, step))
    )
    operation_id = controller.start(
        _CooperativeExecutor(),
        FlowCommand("wait", step_id="cancel-step"),
        _android_session(),
        "Flow",
    )
    assert started.wait(1)

    assert not controller.cancel("stale-operation")
    assert controller.cancel(operation_id)
    assert controller.cancel(operation_id)
    assert controller.state == "cancel_requested"
    _wait_until(application, lambda: bool(cancelled))

    assert cancelled == [(operation_id, "cancel-step")]
    assert states == ["running", "cancel_requested", "idle"]
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_worker_start_failure_rolls_back_state_and_lease(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    states: list[str] = []
    controller.state_changed.connect(
        lambda _operation, _step, state: states.append(state)
    )

    with patch(
        "automadin.ui.flow_editor.run_step_controller._RunStepWorker.start",
        side_effect=OSError("private launch detail"),
    ):
        with pytest.raises(RunStepStartError) as caught:
            controller.start(
                _RecordingExecutor(),
                FlowCommand("wait", step_id="start-failure"),
                _android_session(),
                "Flow",
            )

    assert "private launch detail" not in str(caught.value)
    assert states == []
    assert controller.state == "idle"
    assert not controller.is_busy
    assert registry.active_count == 0
    application.processEvents()
    controller.deleteLater()


def test_worker_start_exception_after_native_start_retains_worker_and_lease(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    entered = threading.Event()
    release = threading.Event()
    real_start = _RunStepWorker.start

    class _BlockingExecutor:
        def execute_command(self, _command, _ctx, *, flow_name) -> None:
            entered.set()
            release.wait(1)

    def start_then_raise(worker: _RunStepWorker) -> None:
        real_start(worker)
        assert entered.wait(1)
        raise OSError("private post-start detail")

    with patch.object(_RunStepWorker, "start", start_then_raise):
        with pytest.raises(RunStepStartError) as caught:
            controller.start(
                _BlockingExecutor(),
                FlowCommand("wait", step_id="post-start-failure"),
                _android_session("post-start-failure-session"),
                "Flow",
            )

    assert "private post-start detail" not in str(caught.value)
    assert controller.is_busy
    assert controller.state == "cancel_requested"
    assert registry.active_count == 1

    release.set()
    _wait_until(application, lambda: not controller.is_busy)
    assert registry.active_count == 0
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_worker_that_finishes_before_start_raises_is_not_unstarted_rollback(
    application: QApplication,
) -> None:
    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    real_start = _RunStepWorker.start
    succeeded: list[str] = []
    controller.succeeded.connect(
        lambda _operation, step_id: succeeded.append(step_id)
    )

    def finish_then_raise(worker: _RunStepWorker) -> None:
        real_start(worker)
        assert worker.wait(2000)
        raise OSError("private post-completion start detail")

    with patch.object(_RunStepWorker, "start", finish_then_raise):
        with pytest.raises(RunStepStartError) as caught:
            controller.start(
                _RecordingExecutor(),
                FlowCommand("wait", step_id="fast-finish"),
                _android_session("fast-finish-session"),
                "Flow",
            )

    assert caught.value.cleanup_pending
    assert controller.is_busy
    _wait_until(application, lambda: not controller.is_busy)
    assert succeeded == ["fast-finish"]
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_run_step_worker_retains_lease_after_release_exception() -> None:
    registry = DeviceOperationLeaseRegistry()
    session = _android_session("lease-retry-session")
    lease = registry.acquire(
        device_operation_key(session.device),
        DeviceOperationUse("run-step", "Run Step", "lease-retry"),
    )
    worker = _RunStepWorker(
        operation_id="lease-retry",
        executor=_RecordingExecutor(),
        command=FlowCommand("wait"),
        session=session,
        flow_name="Flow",
        context=FlowContext(session),
        device_lease=lease,
    )

    with patch.object(
        registry,
        "_release",
        side_effect=RuntimeError("transient registry failure"),
    ):
        assert not worker.release_device_lease()

    assert worker._device_lease is lease
    assert registry.active_count == 1
    assert worker.release_device_lease()
    assert worker._device_lease is None
    assert registry.active_count == 0
    worker.deleteLater()


def test_worker_construction_failure_also_rolls_back_preacquired_lease(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)

    with patch(
        "automadin.ui.flow_editor.run_step_controller._RunStepWorker",
        side_effect=MemoryError("private construction detail"),
    ):
        with pytest.raises(RunStepStartError) as caught:
            controller.start(
                _RecordingExecutor(),
                FlowCommand("wait", step_id="construction-failure"),
                _android_session(),
                "Flow",
            )

    assert "private construction detail" not in str(caught.value)
    assert registry.active_count == 0
    assert not controller.is_busy
    application.processEvents()
    controller.deleteLater()


def test_stale_terminal_cannot_clear_or_complete_active_operation(
    application: QApplication,
) -> None:
    release = threading.Event()

    class _BlockingExecutor:
        def execute_command(self, _command, _ctx, *, flow_name) -> None:
            release.wait(1)

    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    succeeded: list[str] = []
    controller.succeeded.connect(lambda operation, _step: succeeded.append(operation))
    operation_id = controller.start(
        _BlockingExecutor(),
        FlowCommand("wait", step_id="live-step"),
        _android_session(),
        "Flow",
    )

    controller._on_worker_terminal(
        "stale-operation",
        "live-step",
        "succeeded",
        "",
    )
    controller._on_worker_terminal(
        operation_id,
        "stale-step",
        "succeeded",
        "",
    )

    assert controller.active_operation_id == operation_id
    assert succeeded == []
    release.set()
    _wait_until(application, lambda: bool(succeeded))
    assert succeeded == [operation_id]
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_terminal_outcome_waits_for_exact_finished_before_idle(
    application: QApplication,
) -> None:
    release = threading.Event()

    class _BlockingExecutor:
        def execute_command(self, _command, _ctx, *, flow_name) -> None:
            release.wait(1)

    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    states: list[str] = []
    successes: list[str] = []
    controller.state_changed.connect(
        lambda _operation, _step, state: states.append(state)
    )
    controller.succeeded.connect(
        lambda operation, _step: successes.append(operation)
    )
    operation_id = controller.start(
        _BlockingExecutor(),
        FlowCommand("wait", step_id="ordered-step"),
        _android_session("ordered-session"),
        "Flow",
    )

    controller._on_worker_terminal(
        operation_id,
        "ordered-step",
        "succeeded",
        "",
    )

    assert controller.active_operation_id == operation_id
    assert controller.active_step_id == "ordered-step"
    assert controller.state == "running"
    assert controller.is_busy
    assert successes == []
    assert states == ["running"]

    release.set()
    _wait_until(application, lambda: not controller.is_busy)

    assert successes == [operation_id]
    assert states == ["running", "idle"]
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_finished_release_failures_keep_identity_and_retry_cleanup_once(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    original_release = registry._release
    release_calls = 0
    states: list[str] = []
    failures: list[tuple[str, str, str]] = []

    def fail_release_twice(session_id, generation, token):
        nonlocal release_calls
        release_calls += 1
        if release_calls <= 2:
            raise RuntimeError("transient cleanup failure")
        return original_release(session_id, generation, token)

    controller.state_changed.connect(
        lambda _operation, _step, state: states.append(state)
    )
    controller.failed.connect(
        lambda operation, step, message: failures.append(
            (operation, step, message)
        )
    )

    with patch.object(registry, "_release", side_effect=fail_release_twice):
        operation_id = controller.start(
            _RecordingExecutor(),
            FlowCommand("wait", step_id="cleanup-retry-step"),
            _android_session("cleanup-retry-session"),
            "Flow",
        )
        _wait_until(
            application,
            lambda: controller.state == "cleanup_pending",
        )

        worker = controller._workers[operation_id]
        assert worker.isFinished()
        assert controller.active_operation_id == operation_id
        assert controller.active_step_id == "cleanup-retry-step"
        assert controller.is_busy
        assert failures == []
        assert states == ["running", "cleanup_pending"]
        assert registry.active_count == 1

        assert controller.cancel(operation_id)

    assert release_calls == 3
    assert not controller.is_busy
    assert controller.active_operation_id is None
    assert controller.state == "idle"
    assert states == ["running", "cleanup_pending", "idle"]
    assert len(failures) == 1
    assert failures[0][:2] == (operation_id, "cleanup-retry-step")
    assert not controller.cancel(operation_id)
    assert len(failures) == 1
    assert registry.active_count == 0
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_worker_finished_without_terminal_releases_lease_and_fails_once(
    application: QApplication,
) -> None:
    class _SilentWorker(_RunStepWorker):
        def run(self) -> None:
            return

    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    failures: list[tuple[str, str, str]] = []
    cancellations: list[tuple[str, str]] = []
    successes: list[tuple[str, str]] = []
    controller.failed.connect(
        lambda operation, step, message: failures.append(
            (operation, step, message)
        )
    )
    controller.cancelled.connect(
        lambda operation, step: cancellations.append((operation, step))
    )
    controller.succeeded.connect(
        lambda operation, step: successes.append((operation, step))
    )

    with patch(
        "automadin.ui.flow_editor.run_step_controller._RunStepWorker.run",
        _SilentWorker.run,
    ):
        operation_id = controller.start(
            _RecordingExecutor(),
            FlowCommand("wait", step_id="omitted-terminal"),
            _android_session("omitted-terminal-session"),
            "Flow",
        )
        _wait_until(application, lambda: bool(failures))

    assert failures == [
        (
            operation_id,
            "omitted-terminal",
            "Run Step failed. Check the application log for details, then "
            "reconnect the device and try again.",
        )
    ]
    assert successes == []
    assert cancellations == []
    assert (
        registry.snapshot(device_operation_key(AndroidDevice("emulator-5554")))
        is None
    )
    assert not controller.is_busy
    assert controller.state == "idle"
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_stale_finished_cannot_clear_or_complete_replacement_operation(
    application: QApplication,
) -> None:
    replacement_started = threading.Event()
    replacement_release = threading.Event()

    class _ReplacementExecutor:
        def execute_command(self, _command, _ctx, *, flow_name) -> None:
            replacement_started.set()
            replacement_release.wait(1)

    class _StaleWorker:
        step_id = "stale-step"

        def __init__(self) -> None:
            self.deleted = False

        def deleteLater(self) -> None:
            self.deleted = True

    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    completed: list[str] = []
    controller.succeeded.connect(
        lambda operation, _step: completed.append(operation)
    )
    replacement_id = controller.start(
        _ReplacementExecutor(),
        FlowCommand("wait", step_id="replacement-step"),
        _android_session("replacement-session"),
        "Flow",
    )
    assert replacement_started.wait(1)

    stale_worker = _StaleWorker()
    controller._on_worker_finished(  # type: ignore[arg-type]
        "stale-operation",
        stale_worker,
    )

    assert controller.active_operation_id == replacement_id
    assert controller.active_step_id == "replacement-step"
    assert controller.state == "running"
    assert completed == []
    assert stale_worker.deleted

    replacement_release.set()
    _wait_until(application, lambda: replacement_id in completed)
    assert completed == [replacement_id]
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_shutdown_cancels_but_retains_worker_until_it_really_finishes(
    application: QApplication,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class _UncooperativeExecutor:
        def execute_command(self, _command, _ctx, *, flow_name) -> None:
            started.set()
            release.wait(2)

    controller = RunStepOperationController(
        lease_registry=DeviceOperationLeaseRegistry()
    )
    operation_id = controller.start(
        _UncooperativeExecutor(),
        FlowCommand("wait", step_id="shutdown-step"),
        _android_session(),
        "Flow",
    )
    assert started.wait(1)

    assert not controller.shutdown(1)
    assert controller.active_operation_id == operation_id
    assert controller.state == "cancel_requested"
    assert operation_id in controller._workers

    release.set()
    _wait_until(application, lambda: not controller.is_busy)
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_definite_start_failure_retains_worker_until_lease_release_retry(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    original_release = registry._release
    release_calls = 0

    def transient_release_failure(session_id, generation, token):
        nonlocal release_calls
        release_calls += 1
        if release_calls <= 2:
            raise RuntimeError("transient registry failure")
        return original_release(session_id, generation, token)

    with (
        patch.object(_RunStepWorker, "start", side_effect=RuntimeError("start failed")),
        patch.object(registry, "_release", side_effect=transient_release_failure),
        pytest.raises(RunStepStartError) as caught,
    ):
        controller.start(
            _RecordingExecutor(),
            FlowCommand("wait", step_id="failed-start-step"),
            _android_session("failed-start-session"),
            "Flow",
        )

    operation_id = controller.active_operation_id
    assert caught.value.cleanup_pending
    assert operation_id is not None
    assert controller.is_busy
    assert controller.state == "cleanup_pending"
    assert operation_id in controller._workers
    assert registry.active_count == 1

    with patch.object(
        registry,
        "_release",
        side_effect=transient_release_failure,
    ):
        assert not controller.shutdown()
    assert controller.is_busy
    assert controller.state == "cleanup_pending"
    assert operation_id in controller._workers
    assert registry.active_count == 1

    assert controller.shutdown()
    assert not controller.is_busy
    assert controller.state == "idle"
    assert not controller._workers
    assert registry.active_count == 0
    controller.deleteLater()
    application.processEvents()


def test_nonbrowser_device_conflict_is_actionable_and_non_sensitive(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    session = _android_session("shared-session")
    existing = registry.acquire(
        device_operation_key(session.device),
        DeviceOperationUse("flow-debugger", "Private Flow Name", "debugger-operation"),
    )
    controller = RunStepOperationController(lease_registry=registry)

    with pytest.raises(RunStepStartError) as caught:
        controller.start(
            _RecordingExecutor(),
            FlowCommand("wait", step_id="conflict-step"),
            session,
            "Flow",
        )

    message = str(caught.value)
    assert "already busy" in message
    assert "Stop that operation" in message
    assert "Private Flow Name" not in message
    assert "shared-session" not in message
    assert not controller.is_busy
    existing.release()
    application.processEvents()
    controller.deleteLater()


def test_lease_cleanup_precedes_success_delivery(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    session = _android_session("cleanup-session")
    observed: list[str] = []

    def on_succeeded(_operation: str, _step: str) -> None:
        assert registry.snapshot(device_operation_key(session.device)) is None
        observed.append("succeeded")

    controller.succeeded.connect(on_succeeded)
    controller.start(
        _RecordingExecutor(),
        FlowCommand("wait", step_id="cleanup-step"),
        session,
        "Flow",
    )
    _wait_until(application, lambda: bool(observed))

    assert observed == ["succeeded"]
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_base_exception_becomes_one_fixed_safe_failure_after_cleanup(
    application: QApplication,
) -> None:
    class _BaseExceptionExecutor:
        def execute_command(self, _command, _ctx, *, flow_name) -> None:
            raise SystemExit("private executor detail")

    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    session = _android_session("base-exception-session")
    failures: list[tuple[str, str, str]] = []
    successes: list[str] = []
    cancellations: list[str] = []
    controller.failed.connect(
        lambda operation, step, message: failures.append(
            (operation, step, message)
        )
    )
    controller.succeeded.connect(
        lambda operation, _step: successes.append(operation)
    )
    controller.cancelled.connect(
        lambda operation, _step: cancellations.append(operation)
    )

    operation_id = controller.start(
        _BaseExceptionExecutor(),
        FlowCommand("wait", step_id="base-exception-step"),
        session,
        "Flow",
    )
    _wait_until(application, lambda: bool(failures))

    assert failures == [
        (
            operation_id,
            "base-exception-step",
            "Run Step failed. Check the application log for details, then "
            "reconnect the device and try again.",
        )
    ]
    assert successes == []
    assert cancellations == []
    assert "private executor detail" not in failures[0][2]
    assert registry.snapshot(device_operation_key(session.device)) is None
    assert controller.shutdown(1000)
    controller.deleteLater()


def test_restart_flow_is_rejected_before_ownership_or_dispatch(
    application: QApplication,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = RunStepOperationController(lease_registry=registry)
    executor = _RecordingExecutor()

    with pytest.raises(RunStepStartError, match="cannot run as an isolated step"):
        controller.start(
            executor,
            FlowCommand("restart_flow", step_id="restart-step"),
            _android_session(),
            "Flow",
        )

    assert not executor.called.is_set()
    assert registry.active_count == 0
    assert not controller.is_busy
    application.processEvents()
    controller.deleteLater()
