from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtWidgets import QApplication

from automadin.engine.windows_engine import (
    WindowNotFoundError,
    WindowsDeviceEngine,
)
from automadin.execution.build_executor import build_executor
from automadin.execution.context import FlowContext
from automadin.execution.handlers.windows import handle_win_ensure_app
from automadin.models.device import (
    AndroidDevice,
    DeviceSession,
    WindowsDevice,
    device_from_saved_id,
    make_windows_application_saved_device_id,
    parse_windows_application_saved_device_id,
    saved_device_id_for_device,
)
from automadin.models.flow import FlowCommand
from automadin.models.flow import AutomationFlow
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import device_operation_key
from automadin.services.flow_preflight import TargetKind, supported_targets
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS
from automadin.ui.launcher.run_config_dialog import RunConfigDialog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_restartable_windows_saved_target_round_trips() -> None:
    device = WindowsDevice(
        hwnd=123,
        title="Raid | Live Arena",
        executable_path=r"C:\Games\Raid\raid.exe",
        auto_start=True,
    )

    saved = saved_device_id_for_device(device)

    assert saved.startswith("windows-app|1|")
    assert parse_windows_application_saved_device_id(saved) == device
    assert device_from_saved_id(saved) == device


def test_restartable_windows_target_defaults_auto_start_off() -> None:
    saved = make_windows_application_saved_device_id(
        123,
        "Raid",
        r"C:\Games\Raid\raid.exe",
    )

    assert device_from_saved_id(saved) == WindowsDevice(
        123,
        "Raid",
        r"C:\Games\Raid\raid.exe",
        auto_start=False,
    )


def test_preference_missing_from_early_saved_format_defaults_off() -> None:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "hwnd": 123,
                "title": "Raid",
                "executable_path": r"C:\Games\Raid\raid.exe",
            }
        ).encode("utf-8")
    ).decode("ascii")

    device = device_from_saved_id(f"windows-app|1|{payload}")

    assert isinstance(device, WindowsDevice)
    assert not device.auto_start


def test_legacy_windows_saved_target_remains_unchanged() -> None:
    device = WindowsDevice(hwnd=123, title="Game")

    assert saved_device_id_for_device(device) == "windows|123|Game"
    assert device_from_saved_id("windows|123|Game") == device


@pytest.mark.parametrize(
    "saved",
    ["windows-app|1|", "windows-app|1|not-base64", "windows-app|1|e30="],
)
def test_malformed_restartable_windows_target_is_rejected(saved: str) -> None:
    assert parse_windows_application_saved_device_id(saved) is None
    with pytest.raises(ValueError, match="Malformed"):
        device_from_saved_id(saved)


def test_executable_identity_is_stable_across_hwnd_and_title_changes() -> None:
    first = WindowsDevice(10, "Loading", r"C:\Games\App.exe")
    second = WindowsDevice(99, "Ready", r"c:/games/app.exe")

    assert device_operation_key(first) == device_operation_key(second)


def test_resolution_uses_title_to_disambiguate_same_executable() -> None:
    engine = WindowsDeviceEngine()
    engine.list_devices = Mock(  # type: ignore[method-assign]
        return_value=[
            WindowsDevice(10, "Main", r"C:\Apps\app.exe"),
            WindowsDevice(11, "Settings", r"C:\Apps\app.exe"),
        ]
    )

    resolved = engine._resolve_device(
        WindowsDevice(0, "Settings", r"c:/apps/app.exe")
    )

    assert resolved.hwnd == 11


def test_ensure_application_launches_with_argument_vector_and_reacquires(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"test")
    engine = WindowsDeviceEngine()
    live = WindowsDevice(99, "Ready", str(executable.resolve()))
    engine._resolve_device = Mock(  # type: ignore[method-assign]
        side_effect=[
            WindowNotFoundError("closed"),
            live,
        ]
    )
    session_data: dict[str, object] = {
        "hwnd": 10,
        "title": "Ready",
        "executable_path": str(executable),
    }

    with patch("automadin.engine.windows_engine.subprocess.Popen") as popen:
        launched = engine.ensure_application(
            session_data,
            arguments=["--profile", "Arena One"],
            timeout=5,
        )

    assert launched
    assert session_data["hwnd"] == 99
    popen.assert_called_once_with(
        [str(executable.resolve()), "--profile", "Arena One"],
        cwd=str(executable.resolve().parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
    )


def test_ensure_application_does_not_launch_when_window_is_live() -> None:
    engine = WindowsDeviceEngine()
    live = WindowsDevice(42, "Ready", r"C:\Apps\app.exe")
    engine._resolve_device = Mock(return_value=live)  # type: ignore[method-assign]
    session_data: dict[str, object] = {
        "hwnd": 42,
        "title": "Ready",
        "executable_path": r"C:\Apps\app.exe",
    }

    with patch("automadin.engine.windows_engine.subprocess.Popen") as popen:
        assert not engine.ensure_application(session_data)

    popen.assert_not_called()


def test_connect_does_not_launch_closed_app_when_policy_is_off() -> None:
    engine = WindowsDeviceEngine()
    engine._resolve_device = Mock(  # type: ignore[method-assign]
        side_effect=WindowNotFoundError("closed")
    )
    engine._launch_and_wait = Mock()  # type: ignore[method-assign]

    with (
        patch("automadin.engine.windows_engine._is_windows", return_value=True),
        patch(
            "automadin.engine.windows_engine.ctypes.windll",
            SimpleNamespace(user32=Mock()),
        ),
        pytest.raises(WindowNotFoundError, match="closed"),
    ):
        engine.connect(
            WindowsDevice(
                10,
                "Raid",
                r"C:\Games\Raid\raid.exe",
                auto_start=False,
            )
        )

    engine._launch_and_wait.assert_not_called()


def test_connect_launches_closed_app_only_when_policy_is_on() -> None:
    engine = WindowsDeviceEngine()
    live = WindowsDevice(99, "Raid", r"C:\Games\Raid\raid.exe")
    engine._resolve_device = Mock(  # type: ignore[method-assign]
        side_effect=WindowNotFoundError("closed")
    )
    engine._launch_and_wait = Mock(return_value=live)  # type: ignore[method-assign]
    user32 = Mock()
    user32.IsWindow.return_value = True

    with (
        patch("automadin.engine.windows_engine._is_windows", return_value=True),
        patch(
            "automadin.engine.windows_engine.ctypes.windll",
            SimpleNamespace(user32=user32),
        ),
    ):
        data = engine.connect(
            WindowsDevice(
                10,
                "Raid",
                r"C:\Games\Raid\raid.exe",
                auto_start=True,
            )
        )

    assert data["hwnd"] == 99
    assert data["auto_start"] is True
    engine._launch_and_wait.assert_called_once()


def test_restore_and_focus_uses_refreshed_handle() -> None:
    engine = WindowsDeviceEngine()
    data: dict[str, object] = {"hwnd": 77, "title": "Game"}
    engine.resolve_session_data = Mock(return_value=data)  # type: ignore[method-assign]
    user32 = Mock()
    user32.IsWindow.return_value = True
    with (
        patch("automadin.engine.windows_engine.focus_window") as focus,
        patch(
            "automadin.engine.windows_engine.ctypes.windll",
            SimpleNamespace(user32=user32),
        ),
    ):
        assert engine.restore_and_focus(data) is data

    focus.assert_called_once_with(77)


def test_ensure_handler_updates_session_identity_and_focuses() -> None:
    session = DeviceSession(
        device=WindowsDevice(10, "Old", r"C:\Apps\app.exe"),
        data={
            "windows_data": {
                "hwnd": 10,
                "title": "Old",
                "executable_path": r"C:\Apps\app.exe",
            }
        },
    )
    engine = Mock()
    engine.ensure_application.return_value = True

    def update(data: dict[str, object], **_kwargs: object) -> bool:
        data.update(hwnd=99, title="Ready")
        return True

    engine.ensure_application.side_effect = update
    context = FlowContext(session)
    context.variables["mode"] = "arena"
    with (
        patch(
            "automadin.execution.handlers.windows._require_windows"
        ),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine",
            return_value=engine,
        ),
    ):
        handle_win_ensure_app(
            FlowCommand(
                "win_ensure_app",
                params={
                    "arguments": ["--mode", "{{mode}}"],
                    "timeout": 2500,
                    "focus": True,
                },
            ),
            context,
        )

    engine.ensure_application.assert_called_once_with(
        session.data["windows_data"],
        executable_path="",
        arguments=["--mode", "arena"],
        window_title="",
        timeout=2.5,
    )
    engine.restore_and_focus.assert_called_once_with(session.data["windows_data"])
    assert session.device == WindowsDevice(99, "Ready", r"C:\Apps\app.exe")


def test_windows_lifecycle_command_is_registered_and_windows_only() -> None:
    assert "win_ensure_app" in build_executor().registered_handler_ids
    assert supported_targets("win_ensure_app") == {TargetKind.WINDOWS}

    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    with (
        patch("automadin.execution.handlers.windows._require_windows"),
        pytest.raises(RuntimeError, match="Windows device"),
    ):
        handle_win_ensure_app(FlowCommand("win_ensure_app"), FlowContext(session))


def test_run_setup_exposes_default_off_windows_startup_choice(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "run-setup.db"))
    database.save_flow("Windows flow", AutomationFlow("Windows flow"))
    selected = WindowsDevice(10, "Raid", r"C:\Games\Raid\raid.exe")
    dialog = RunConfigDialog(
        database,
        current_device_id=saved_device_id_for_device(selected),
    )
    try:
        assert not dialog._windows_section.isHidden()
        assert not dialog._windows_auto_start.isChecked()

        off = device_from_saved_id(dialog.get_saved_run_config().device_id)
        assert isinstance(off, WindowsDevice)
        assert not off.auto_start

        dialog._windows_auto_start.setChecked(True)
        enabled = device_from_saved_id(dialog.get_saved_run_config().device_id)
        assert isinstance(enabled, WindowsDevice)
        assert enabled.auto_start
    finally:
        dialog.deleteLater()
        database.close()
