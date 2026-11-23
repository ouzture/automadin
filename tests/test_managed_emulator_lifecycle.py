from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

from automadin.models.device import AndroidDevice
from automadin.models.managed_emulator import (
    EmulatorCleanupPolicy,
    EmulatorProcessCommand,
    ManagedEmulatorProfile,
    ManagedEmulatorTargetResolution,
    ManagedEmulatorTargetMode,
)
from automadin.services.device_operation_leases import (
    DeviceOperationConflictError,
    DeviceOperationLeaseRegistry,
)
from automadin.services.managed_emulator_lifecycle import (
    ManagedEmulatorAcquisition,
    ManagedEmulatorCancelled,
    ManagedEmulatorLifecycle,
    ManagedEmulatorLifecycleError,
    acquire_managed_emulator_lease,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Process:
    def __init__(self, poll_values: list[int | None] | None = None) -> None:
        self.pid = 123
        self._poll_values = list(poll_values or [None])
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if len(self._poll_values) > 1:
            return self._poll_values.pop(0)
        return self._poll_values[0]

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0


class _StubbornProcess(_Process):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired("emulator", 1)
        return 0


def _profile(**changes: object) -> ManagedEmulatorProfile:
    profile = ManagedEmulatorProfile(
        name="MuMu main",
        launch_command=EmulatorProcessCommand(r"C:\MuMu\manager.exe", ("start", "0")),
        target_mode=ManagedEmulatorTargetMode.PROVIDER_INSTANCE,
        provider_id="mumu",
        provider_instance_id="0",
        startup_timeout_seconds=5,
        default_cleanup_policy=EmulatorCleanupPolicy.LEAVE_RUNNING,
    )
    return replace(profile, **changes)


def _ready_device() -> AndroidDevice:
    return AndroidDevice(
        "127.0.0.1:16384",
        provider_id="mumu",
        provider_instance_id="0",
    )


def test_existing_target_is_reused_without_process_ownership() -> None:
    lifecycle = ManagedEmulatorLifecycle(lambda: [_ready_device()])

    acquisition = lifecycle.acquire(_profile(), cancelled=lambda: False)

    assert acquisition.device == _ready_device()
    assert not acquisition.started_by_automadin
    assert acquisition.process is None


def test_running_provider_target_is_reconciled_without_launching() -> None:
    current = AndroidDevice(
        "127.0.0.1:20000",
        provider_id="mumu",
        provider_instance_id="0",
    )
    resolved_targets: list[tuple[str, str]] = []

    def resolve_target(provider_id: str, instance_id: str) -> AndroidDevice:
        resolved_targets.append((provider_id, instance_id))
        return current

    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        discover_managed_target=resolve_target,
        popen=lambda *_args, **_kwargs: pytest.fail(
            "a running managed instance must not be launched again"
        ),
    )

    acquisition = lifecycle.acquire(_profile(), cancelled=lambda: False)

    assert acquisition.device is current
    assert not acquisition.started_by_automadin
    assert resolved_targets == [("mumu", "0")]


def test_provider_reconciliation_programming_error_fails_without_launching() -> None:
    def broken_resolver(_provider_id: str, _instance_id: str) -> None:
        raise TypeError("incompatible provider callback")

    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        discover_managed_target=broken_resolver,
        popen=lambda *_args, **_kwargs: pytest.fail(
            "a reconciliation error must not be treated as a missing emulator"
        ),
    )

    with pytest.raises(
        ManagedEmulatorLifecycleError,
        match="Could not inspect managed emulator",
    ) as lifecycle_error:
        lifecycle.acquire(_profile(), cancelled=lambda: False)

    assert isinstance(lifecycle_error.value.__cause__, TypeError)


def test_provider_reconciliation_accepts_current_endpoint_after_saved_endpoint_changes() -> None:
    current = AndroidDevice(
        "127.0.0.1:20000",
        provider_id="mumu",
        provider_instance_id="0",
    )
    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        discover_managed_target=lambda provider_id, instance_id: current,
        popen=lambda *_args, **_kwargs: pytest.fail(
            "the saved provider profile must resolve its current endpoint"
        ),
    )

    acquisition = lifecycle.acquire(_profile(), cancelled=lambda: False)

    assert acquisition.device.device_id == "127.0.0.1:20000"


def test_running_but_not_ready_provider_instance_is_not_launched_again() -> None:
    clock = _Clock()
    statuses: list[str] = []
    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        discover_managed_target=lambda provider_id, instance_id: ManagedEmulatorTargetResolution(
            running=True,
        ),
        popen=lambda *_args, **_kwargs: pytest.fail(
            "an already-running instance must not be launched again"
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(ManagedEmulatorLifecycleError, match="did not become ready"):
        lifecycle.acquire(
            _profile(),
            cancelled=lambda: False,
            status=statuses.append,
        )

    assert statuses == [
        "Acquiring emulator",
        "Waiting for Android",
        "Android unavailable",
    ]


def test_missing_target_is_launched_and_waited_for() -> None:
    clock = _Clock()
    discoveries = iter(([], [], [_ready_device()]))
    process = _Process()
    statuses: list[str] = []
    lifecycle = ManagedEmulatorLifecycle(
        lambda: next(discoveries),
        popen=lambda *_args, **_kwargs: process,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    acquisition = lifecycle.acquire(
        _profile(),
        cancelled=lambda: False,
        status=statuses.append,
    )

    assert acquisition.started_by_automadin
    assert acquisition.process is process
    assert statuses == [
        "Acquiring emulator",
        "Starting emulator",
        "Waiting for Android",
    ]


def test_startup_cancellation_stops_waiting() -> None:
    clock = _Clock()
    process = _Process()
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        popen=lambda *_args, **_kwargs: process,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(ManagedEmulatorCancelled) as cancelled_error:
        lifecycle.acquire(_profile(), cancelled=cancelled)

    partial = cancelled_error.value.partial_acquisition
    assert partial is not None
    assert partial.started_by_automadin
    assert partial.process is process


def test_nonzero_launcher_exit_fails_before_timeout() -> None:
    clock = _Clock()
    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        popen=lambda *_args, **_kwargs: _Process([7]),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(
        ManagedEmulatorLifecycleError,
        match="exited with code 7",
    ) as lifecycle_error:
        lifecycle.acquire(_profile(), cancelled=lambda: False)

    assert lifecycle_error.value.partial_acquisition is not None


def test_wrong_provider_instance_is_rejected_and_timeout_is_terminal() -> None:
    clock = _Clock()
    process = _Process()
    statuses: list[str] = []
    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        discover_managed_target=lambda provider_id, instance_id: AndroidDevice(
            "127.0.0.1:20000",
            provider_id="mumu",
            provider_instance_id="1",
        ),
        popen=lambda *_args, **_kwargs: process,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(
        ManagedEmulatorLifecycleError,
        match="did not become ready within 5 seconds",
    ):
        lifecycle.acquire(
            _profile(),
            cancelled=lambda: False,
            status=statuses.append,
        )

    assert statuses[-1] == "Android unavailable"
    assert process.poll() is None


def test_graceful_cleanup_runs_configured_command() -> None:
    shutdown = EmulatorProcessCommand(r"C:\MuMu\manager.exe", ("stop", "0"))
    profile = _profile(shutdown_command=shutdown)
    commands: list[list[str]] = []
    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        run_command=lambda args, **_kwargs: (
            commands.append(args)
            or subprocess.CompletedProcess(args, 0, "closed", "")
        ),
    )
    acquisition = ManagedEmulatorAcquisition(
        profile,
        _ready_device(),
        True,
        _Process(),
    )

    result = lifecycle.cleanup(acquisition, EmulatorCleanupPolicy.CLOSE_IF_STARTED)

    assert not result.warning
    assert commands == [[r"C:\MuMu\manager.exe", "stop", "0"]]


def test_force_fallback_can_terminate_only_owned_process() -> None:
    process = _Process()
    profile = _profile(force_terminate_owned_process=True)
    lifecycle = ManagedEmulatorLifecycle(lambda: [])

    result = lifecycle.cleanup(
        ManagedEmulatorAcquisition(profile, _ready_device(), True, process),
        EmulatorCleanupPolicy.CLOSE_IF_STARTED,
    )

    assert result.warning
    assert process.terminated
    assert not process.killed

    with pytest.raises(ManagedEmulatorLifecycleError, match="no safe shutdown"):
        lifecycle.cleanup(
            ManagedEmulatorAcquisition(profile, _ready_device(), False, None),
            EmulatorCleanupPolicy.ALWAYS_CLOSE,
        )


def test_windows_force_fallback_targets_only_owned_pid_tree() -> None:
    process = _StubbornProcess()
    commands: list[list[str]] = []
    lifecycle = ManagedEmulatorLifecycle(
        lambda: [],
        run_command=lambda args, **_kwargs: (
            commands.append(args)
            or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "automadin.services.managed_emulator_lifecycle.sys.platform",
            "win32",
        )
        lifecycle.cleanup(
            ManagedEmulatorAcquisition(
                _profile(force_terminate_owned_process=True),
                _ready_device(),
                True,
                process,
            ),
            EmulatorCleanupPolicy.CLOSE_IF_STARTED,
        )

    assert commands == [["taskkill.exe", "/PID", "123", "/T", "/F"]]
    assert not process.killed


def test_managed_profile_lease_is_nonblocking_and_reports_owner() -> None:
    registry = DeviceOperationLeaseRegistry()
    profile = _profile()
    first = acquire_managed_emulator_lease(
        profile,
        run_id="first",
        run_label="First",
        registry=registry,
    )
    try:
        with pytest.raises(DeviceOperationConflictError) as conflict:
            acquire_managed_emulator_lease(
                profile,
                run_id="second",
                run_label="Second",
                registry=registry,
            )
        assert conflict.value.current.label == "Run setup 'First'"
    finally:
        first.release()
