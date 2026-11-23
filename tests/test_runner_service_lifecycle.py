from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt

from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.runner import FlowRunWorker, RunnerService
from automadin.models.device import AndroidDevice, DeviceSession, WindowsDevice
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import RunConfig
from automadin.services.device_operation_leases import (
    DeviceOperationConflictError,
    DeviceOperationLeaseRegistry,
    device_operation_key,
)
from automadin.ui.flow_editor.run_step_controller import (
    RunStepOperationController,
    RunStepStartError,
)
from automadin.ui.launcher.launcher_view import LauncherRunWorker


def test_running_worker_is_retained_until_it_really_stops() -> None:
    entered = threading.Event()
    release = threading.Event()
    executor = FlowExecutor()

    def blocking_handler(_command, _ctx) -> None:
        entered.set()
        release.wait(2)

    executor.register_handler("blocking", blocking_handler)
    flow = AutomationFlow(
        name="blocking flow",
        commands=[FlowCommand("blocking")],
    )
    ctx = FlowContext(
        DeviceSession(device=AndroidDevice(device_id="emulator-5554"))
    )
    service = RunnerService()
    worker = service.start_run("run-1", executor, flow, ctx, RunConfig())

    try:
        assert worker.target_display_name == "emulator-5554"
        assert entered.wait(1)
        assert service.remove_run("run-1", wait_ms=1) is False
        assert service.get_worker("run-1") is worker
        assert service.stop_all(wait_ms=1) is False
        assert service.get_worker("run-1") is worker
    finally:
        release.set()
        worker.wait(2000)

    assert service.remove_run("run-1") is True
    assert service.get_worker("run-1") is None


def test_registered_unstarted_worker_counts_toward_concurrency_limit() -> None:
    service = RunnerService(max_concurrent=1)
    executor = FlowExecutor()
    flow = AutomationFlow(name="empty")
    ctx = FlowContext(
        DeviceSession(device=AndroidDevice(device_id="emulator-5554"))
    )
    first = FlowRunWorker(executor, flow, ctx, RunConfig())
    second = FlowRunWorker(executor, flow, ctx, RunConfig())

    service.register_run("reserved", first)
    with pytest.raises(RuntimeError, match="Maximum concurrent runs"):
        service.register_run("too-many", second)

    assert service.remove_run("reserved")


def test_duplicate_run_id_failure_never_removes_preexisting_worker() -> None:
    registry = DeviceOperationLeaseRegistry()
    service = RunnerService()
    existing = FlowRunWorker(
        FlowExecutor(),
        AutomationFlow("Existing"),
        FlowContext(DeviceSession(device=AndroidDevice(device_id="existing-device"))),
        RunConfig(),
        device_operation_registry=registry,
    )
    service.register_run("duplicate", existing)

    with (
        patch(
            "automadin.execution.runner.default_device_operation_registry",
            return_value=registry,
        ),
        pytest.raises(RuntimeError, match="already registered"),
    ):
        service.start_run(
            "duplicate",
            FlowExecutor(),
            AutomationFlow("Rejected"),
            FlowContext(
                DeviceSession(device=AndroidDevice(device_id="rejected-device"))
            ),
            RunConfig(),
        )

    assert service.get_worker("duplicate") is existing
    assert registry.active_count == 0
    assert service.remove_run("duplicate")


def test_registration_failure_happens_before_device_reservation() -> None:
    registry = DeviceOperationLeaseRegistry()
    service = RunnerService(max_concurrent=0)

    with (
        patch(
            "automadin.execution.runner.default_device_operation_registry",
            return_value=registry,
        ),
        patch.object(
            FlowRunWorker,
            "reserve_device_operation",
            side_effect=AssertionError("reservation must follow registration"),
        ),
        pytest.raises(RuntimeError, match="Maximum concurrent runs"),
    ):
        service.start_run(
            "rejected-before-reserve",
            FlowExecutor(),
            AutomationFlow("Rejected"),
            FlowContext(
                DeviceSession(device=AndroidDevice(device_id="never-reserved-device"))
            ),
            RunConfig(),
        )

    assert registry.active_count == 0
    assert service.get_worker("rejected-before-reserve") is None


def test_flow_worker_retains_retryable_lease_after_release_exception() -> None:
    registry = DeviceOperationLeaseRegistry()
    device = AndroidDevice(device_id="retry-release-device")
    worker = FlowRunWorker(
        FlowExecutor(),
        AutomationFlow("Cleanup"),
        FlowContext(DeviceSession(device=device)),
        RunConfig(),
        device_operation_registry=registry,
    )
    worker.reserve_device_operation()
    lease = worker._device_operation_lease
    assert lease is not None

    with patch.object(
        registry,
        "_release",
        side_effect=RuntimeError("transient registry failure"),
    ):
        assert not worker.release_reserved_device_operation()

    assert worker._device_operation_lease is lease
    assert registry.active_count == 1
    assert worker.release_reserved_device_operation()
    assert worker._device_operation_lease is None
    assert registry.active_count == 0


def test_start_failure_retains_registered_worker_until_lease_cleanup_retries() -> None:
    registry = DeviceOperationLeaseRegistry()
    service = RunnerService(max_concurrent=1)
    original_release = registry._release
    release_calls = 0

    def transient_release_failure(session_id, generation, token):
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise RuntimeError("transient registry failure")
        return original_release(session_id, generation, token)

    with (
        patch(
            "automadin.execution.runner.default_device_operation_registry",
            return_value=registry,
        ),
        patch.object(FlowRunWorker, "start", side_effect=RuntimeError("start failed")),
        patch.object(registry, "_release", side_effect=transient_release_failure),
        pytest.raises(RuntimeError, match="start failed"),
    ):
        service.start_run(
            "failed-start",
            FlowExecutor(),
            AutomationFlow("Failed start"),
            FlowContext(
                DeviceSession(device=AndroidDevice(device_id="failed-start-device"))
            ),
            RunConfig(),
        )

    retained = service.get_worker("failed-start")
    assert retained is not None
    assert registry.active_count == 1

    replacement = FlowRunWorker(
        FlowExecutor(),
        AutomationFlow("Replacement"),
        FlowContext(DeviceSession()),
        RunConfig(),
    )
    with pytest.raises(RuntimeError, match="Maximum concurrent runs"):
        service.register_run("replacement", replacement)

    assert service.remove_run("failed-start")
    assert service.get_worker("failed-start") is None
    assert registry.active_count == 0
    service.register_run("replacement", replacement)
    assert service.remove_run("replacement")


def test_flow_worker_emits_one_sanitized_error_for_handler_failure() -> None:
    executor = FlowExecutor()

    def fail_with_private_detail(_command, _ctx) -> None:
        raise RuntimeError(r"private C:\Users\Example\secret-token.txt")

    executor.register_handler("failure", fail_with_private_detail)
    worker = FlowRunWorker(
        executor,
        AutomationFlow("Failure", [FlowCommand("failure")]),
        FlowContext(DeviceSession()),
        RunConfig(),
    )
    errors: list[str] = []
    reports: list[object] = []
    completions: list[str] = []
    worker.error_occurred.connect(errors.append, Qt.ConnectionType.DirectConnection)
    worker.error_details_available.connect(
        reports.append,
        Qt.ConnectionType.DirectConnection,
    )
    worker.completed.connect(completions.append, Qt.ConnectionType.DirectConnection)

    worker.start()
    assert worker.wait(2000)

    assert len(errors) == 1
    assert "private" not in errors[0]
    assert "secret-token" not in errors[0]
    assert "reference " in errors[0]
    assert len(reports) == 1
    assert "secret-token" in reports[0].details
    assert len(completions) == 1
    assert "private" not in completions[0]
    assert "View details" in completions[0]


def test_same_android_session_cannot_run_two_workers_concurrently() -> None:
    registry = DeviceOperationLeaseRegistry()
    entered = threading.Event()
    release = threading.Event()
    first_executor = FlowExecutor()
    second_executor = FlowExecutor()
    second_calls: list[str] = []

    def blocking_handler(_command, _ctx) -> None:
        entered.set()
        release.wait(2)

    first_executor.register_handler("blocking", blocking_handler)
    second_executor.register_handler(
        "blocking",
        lambda _command, _ctx: second_calls.append("executed"),
    )
    session = DeviceSession(device=AndroidDevice(device_id="emulator-5554"))
    flow = AutomationFlow("shared device", [FlowCommand("blocking")])
    first = FlowRunWorker(
        first_executor,
        flow,
        FlowContext(session),
        RunConfig(),
        device_operation_registry=registry,
    )
    second = FlowRunWorker(
        second_executor,
        flow,
        FlowContext(session),
        RunConfig(),
        device_operation_registry=registry,
    )
    errors: list[str] = []
    second.error_occurred.connect(
        errors.append,
        Qt.ConnectionType.DirectConnection,
    )

    first.start()
    try:
        assert entered.wait(1)
        second.start()
        assert second.wait(1000)

        assert not second_calls
        assert errors
        assert "already being used" in errors[-1]
        assert registry.active_count == 1
    finally:
        release.set()
        assert first.wait(2000)

    assert registry.active_count == 0


@pytest.mark.parametrize(
    ("first_device", "second_device"),
    [
        (
            AndroidDevice(device_id="emulator-5554", model="First label"),
            AndroidDevice(device_id="emulator-5554", model="Second label"),
        ),
        (
            WindowsDevice(hwnd=100, title="  Calculator   App "),
            WindowsDevice(hwnd=900, title="calculator app"),
        ),
    ],
    ids=["android", "windows"],
)
def test_same_target_conflicts_across_different_session_uuids(
    first_device,
    second_device,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    flow = AutomationFlow("shared target")
    first = FlowRunWorker(
        FlowExecutor(),
        flow,
        FlowContext(DeviceSession(session_id="session-a", device=first_device)),
        RunConfig(),
        device_operation_registry=registry,
    )
    second = FlowRunWorker(
        FlowExecutor(),
        flow,
        FlowContext(DeviceSession(session_id="session-b", device=second_device)),
        RunConfig(),
        device_operation_registry=registry,
    )

    first.reserve_device_operation()
    try:
        assert (
            registry.snapshot(device_operation_key(first_device))
            == first._device_operation_use
        )
        assert device_operation_key(first_device) == device_operation_key(second_device)
        with pytest.raises(DeviceOperationConflictError) as caught:
            second.reserve_device_operation()
        assert caught.value.current == first._device_operation_use
        assert second._device_operation_lease is None
    finally:
        first.release_reserved_device_operation()

    assert registry.active_count == 0


def test_pre_reserved_flow_worker_wins_before_later_run_step_dispatch() -> None:
    registry = DeviceOperationLeaseRegistry()
    device = AndroidDevice(device_id="emulator-5554")
    flow_worker = FlowRunWorker(
        FlowExecutor(),
        AutomationFlow("Debugger flow"),
        FlowContext(DeviceSession(session_id="debug-session", device=device)),
        RunConfig(),
        device_operation_registry=registry,
    )
    flow_worker.reserve_device_operation()
    run_step = RunStepOperationController(lease_registry=registry)
    executor = FlowExecutor()
    called: list[str] = []
    executor.register_handler(
        "probe",
        lambda _command, _ctx: called.append("executed"),
    )

    try:
        with pytest.raises(RunStepStartError, match="already busy"):
            run_step.start(
                executor,
                FlowCommand("probe"),
                DeviceSession(
                    session_id="different-editor-session",
                    device=AndroidDevice(device_id="emulator-5554"),
                ),
                "Editor flow",
            )

        assert called == []
        assert (
            registry.snapshot(device_operation_key(device))
            == flow_worker._device_operation_use
        )
    finally:
        flow_worker.release_reserved_device_operation()
        run_step.deleteLater()

    assert registry.active_count == 0


@pytest.mark.parametrize("failure_stage", ["register", "start"])
def test_runner_service_start_rollback_releases_synchronous_reservation(
    failure_stage: str,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    device = AndroidDevice(device_id="emulator-5554")
    service = RunnerService(max_concurrent=0 if failure_stage == "register" else 1)
    start_patch = (
        patch.object(
            FlowRunWorker,
            "start",
            side_effect=OSError("worker start failed"),
        )
        if failure_stage == "start"
        else patch.object(FlowRunWorker, "start", autospec=True)
    )

    with (
        patch(
            "automadin.execution.runner.default_device_operation_registry",
            return_value=registry,
        ),
        start_patch,
        pytest.raises((RuntimeError, OSError)),
    ):
        service.start_run(
            "rollback-run",
            FlowExecutor(),
            AutomationFlow("Rollback flow"),
            FlowContext(
                DeviceSession(session_id="ephemeral-session", device=device)
            ),
            RunConfig(),
        )

    assert service.get_worker("rollback-run") is None
    assert registry.snapshot(device_operation_key(device)) is None
    assert registry.active_count == 0


def test_runner_service_post_native_start_raise_retains_worker_and_reservation() -> None:
    registry = DeviceOperationLeaseRegistry()
    entered = threading.Event()
    release = threading.Event()
    executor = FlowExecutor()

    def blocking_handler(_command, _ctx) -> None:
        entered.set()
        release.wait(2)

    executor.register_handler("blocking", blocking_handler)
    device = AndroidDevice(device_id="post-native-emulator")
    service = RunnerService()
    original_start = FlowRunWorker.start

    def start_then_raise(worker) -> None:
        original_start(worker)
        assert entered.wait(1)
        raise OSError("post-native runner start detail")

    with (
        patch(
            "automadin.execution.runner.default_device_operation_registry",
            return_value=registry,
        ),
        patch.object(FlowRunWorker, "start", start_then_raise),
        pytest.raises(OSError, match="post-native runner start detail"),
    ):
        service.start_run(
            "post-native-run",
            executor,
            AutomationFlow(
                "Post-native flow",
                commands=[FlowCommand("blocking")],
            ),
            FlowContext(DeviceSession(device=device)),
            RunConfig(),
        )

    worker = service.get_worker("post-native-run")
    try:
        assert worker is not None
        assert worker.isRunning()
        assert registry.snapshot(device_operation_key(device)) is not None
    finally:
        release.set()
        if worker is not None:
            worker.wait(2000)

    assert registry.snapshot(device_operation_key(device)) is None
    assert service.remove_run("post-native-run")
    assert service.get_worker("post-native-run") is None


def test_runner_service_retains_worker_that_finished_before_start_raised() -> None:
    registry = DeviceOperationLeaseRegistry()
    original_start = FlowRunWorker.start

    def finish_then_raise(worker: FlowRunWorker) -> None:
        original_start(worker)
        assert worker.wait(2000)
        raise OSError("private post-completion start detail")

    service = RunnerService()
    with (
        patch(
            "automadin.execution.runner.default_device_operation_registry",
            return_value=registry,
        ),
        patch.object(FlowRunWorker, "start", finish_then_raise),
        pytest.raises(OSError, match="post-completion"),
    ):
        service.start_run(
            "completed-before-error",
            FlowExecutor(),
            AutomationFlow("Fast flow"),
            FlowContext(
                DeviceSession(device=AndroidDevice(device_id="emulator-fast"))
            ),
            RunConfig(),
        )

    worker = service.get_worker("completed-before-error")
    assert worker is not None
    assert worker.isFinished()
    assert registry.active_count == 0
    assert service.remove_run("completed-before-error")


def test_launcher_like_pre_reservation_uses_target_before_session_resolution() -> None:
    registry = DeviceOperationLeaseRegistry()
    saved_target = WindowsDevice(hwnd=100, title="  Calculator ")
    with patch(
        "automadin.execution.runner.default_device_operation_registry",
        return_value=registry,
    ):
        launcher_worker = LauncherRunWorker(
            FlowExecutor(),
            AutomationFlow("Saved run"),
            RunConfig(),
            saved_target,
            object(),
        )
    launcher_worker.reserve_device_operation()

    refreshed_target = WindowsDevice(hwnd=999, title="calculator")
    later_worker = FlowRunWorker(
        FlowExecutor(),
        AutomationFlow("Later debugger"),
        FlowContext(
            DeviceSession(
                session_id="later-session",
                device=refreshed_target,
            )
        ),
        RunConfig(),
        device_operation_registry=registry,
    )
    try:
        assert (
            registry.snapshot(device_operation_key(saved_target))
            == launcher_worker._device_operation_use
        )
        assert device_operation_key(saved_target) == device_operation_key(
            refreshed_target
        )
        with pytest.raises(DeviceOperationConflictError):
            later_worker.reserve_device_operation()
    finally:
        launcher_worker.release_reserved_device_operation()

    assert registry.active_count == 0
