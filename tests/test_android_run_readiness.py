from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from automadin.engine.adb_engine import (
    AdbDeviceEngine,
    AndroidTargetReadinessError,
    AndroidTargetReadinessKind,
)
from automadin.engine.session import DeviceSessionService
from automadin.execution.executor import FlowExecutor
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import RunConfig, RunStatus
from automadin.ui.launcher.launcher_view import LauncherRunWorker


def _completed(
    args: list[str],
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


@pytest.mark.parametrize(
    ("detail", "kind", "recovery"),
    [
        (
            "error: device unauthorized",
            AndroidTargetReadinessKind.UNAUTHORIZED,
            "accept the USB debugging prompt",
        ),
        (
            "error: device offline",
            AndroidTargetReadinessKind.OFFLINE,
            "Restart ADB if it remains offline",
        ),
        (
            "error: device 'usb-1' not found",
            AndroidTargetReadinessKind.TARGET_UNAVAILABLE,
            "Connect the device",
        ),
    ],
)
def test_android_readiness_classifies_target_state_failures(
    detail: str,
    kind: AndroidTargetReadinessKind,
    recovery: str,
) -> None:
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
    failed = _completed(
        ["adb", "-s", "usb-1", "get-state"],
        stderr=detail,
        returncode=1,
    )

    with (
        patch("automadin.engine.adb_engine.subprocess.run", return_value=failed),
        pytest.raises(AndroidTargetReadinessError) as caught,
    ):
        engine.connect(AndroidDevice("usb-1"))

    assert caught.value.kind is kind
    assert recovery in caught.value.public_message


def test_android_readiness_reconnects_an_exact_tcp_target_before_probe() -> None:
    calls: list[list[str]] = []
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[1:] == ["connect", "127.0.0.1:16384"]:
            return _completed(args, "connected to 127.0.0.1:16384\n")
        if args[1:] == ["-s", "127.0.0.1:16384", "get-state"]:
            return _completed(args, "device\n")
        raise AssertionError(args)

    with patch("automadin.engine.adb_engine.subprocess.run", side_effect=run):
        data = engine.connect(AndroidDevice("127.0.0.1:16384"))

    assert data["device_id"] == "127.0.0.1:16384"
    assert calls == [
        ["adb", "connect", "127.0.0.1:16384"],
        ["adb", "-s", "127.0.0.1:16384", "get-state"],
    ]


def test_android_tcp_readiness_distinguishes_missing_adb_from_target_failure() -> None:
    engine = AdbDeviceEngine(adb_path="missing-adb", emulator_providers=[])

    with (
        patch(
            "automadin.engine.adb_engine.subprocess.run",
            side_effect=FileNotFoundError("missing"),
        ),
        pytest.raises(AndroidTargetReadinessError) as caught,
    ):
        engine.connect(AndroidDevice("127.0.0.1:16384"))

    assert caught.value.kind is AndroidTargetReadinessKind.RUNTIME_UNAVAILABLE
    assert "Runtime Components" in caught.value.public_message


class _Adb:
    def __init__(self) -> None:
        self.data: dict[str, object] | None = None
        self.error: Exception | None = None
        self.calls: list[AndroidDevice] = []

    def connect(self, device: AndroidDevice) -> dict[str, object]:
        self.calls.append(device)
        if self.error is not None:
            raise self.error
        assert self.data is not None
        return self.data


def test_cached_android_session_is_reverified_and_refreshed_atomically() -> None:
    adb = _Adb()
    device = AndroidDevice("usb-1")
    service = DeviceSessionService(adb_engine=adb)  # type: ignore[arg-type]
    stale = DeviceSession(device=device, data={"adb_data": {"device_id": "stale"}})
    service._sessions[stale.session_id] = stale
    adb.data = {"adb_path": "adb", "device_id": "usb-1"}

    refreshed = service.ensure_android_session(device)

    assert refreshed is stale
    assert refreshed.get_data("adb_data") == adb.data
    assert adb.calls == [device]

    old_data = refreshed.get_data("adb_data")
    adb.error = AndroidTargetReadinessError(
        AndroidTargetReadinessKind.OFFLINE,
        "usb-1",
        "Target is offline.",
        "Reconnect it.",
    )
    with pytest.raises(AndroidTargetReadinessError):
        service.ensure_android_session(device)
    assert refreshed.get_data("adb_data") is old_data


def test_launcher_readiness_failure_stops_before_flow_execution() -> None:
    executed: list[str] = []
    device = AndroidDevice("usb-1")

    class _Sessions:
        def ensure_android_session(self, _device: AndroidDevice) -> DeviceSession:
            raise AndroidTargetReadinessError(
                AndroidTargetReadinessKind.UNAUTHORIZED,
                "usb-1",
                "Android target 'usb-1' is waiting for authorization.",
                "Unlock it and accept the USB debugging prompt.",
            )

    executor = FlowExecutor()
    executor.register_handler(
        "probe",
        lambda _command, _context: executed.append("executed"),
    )
    worker = LauncherRunWorker(
        executor,
        AutomationFlow("Readiness", [FlowCommand("probe")]),
        RunConfig(),
        device,
        _Sessions(),
    )
    statuses: list[str] = []
    errors: list[str] = []
    completions: list[str] = []
    worker.status_changed.connect(statuses.append)
    worker.error_occurred.connect(errors.append)
    worker.completed.connect(completions.append)

    worker.run()

    assert executed == []
    assert statuses == [
        RunStatus.CHECKING_ANDROID.value,
        RunStatus.ERROR.value,
    ]
    assert errors == completions
    assert "accept the USB debugging prompt" in errors[0]
    assert worker.status is RunStatus.ERROR
