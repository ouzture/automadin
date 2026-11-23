from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from automadin.engine.adb_engine import AdbDeviceEngine
from automadin.engine.device_controller import AndroidDeviceController
from automadin.execution.context import FlowContext
from automadin.execution.handlers.android_apps import (
    handle_android_force_stop_app,
    handle_android_install_apk,
    handle_android_launch_app,
    handle_android_open_deep_link,
    handle_android_restart_app,
    handle_android_start_activity,
)
from automadin.models.device import AndroidDevice, DeviceSession, WindowsDevice
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.services.flow_preflight import (
    PreflightMode,
    TargetKind,
    preflight_flow,
    supported_targets,
)


def _session_data() -> dict[str, object]:
    return {"device_id": "emulator-5554", "adb_path": "selected-adb.exe"}


def _context(
    device: object | None = None,
    *,
    controller: object | None = None,
) -> FlowContext:
    data: dict[str, object] = {"adb_data": _session_data()}
    if controller is not None:
        data["device_controller"] = controller
    return FlowContext(
        DeviceSession(
            device=device or AndroidDevice("emulator-5554"),  # type: ignore[arg-type]
            data=data,
        )
    )


@pytest.mark.parametrize(
    "value",
    ["", "com", "com.example-app", "com..example", " com.example.app;stop "],
)
def test_package_validation_rejects_unsafe_names(value: str) -> None:
    with pytest.raises(ValueError):
        AdbDeviceEngine.validate_package_name(value)


def test_installed_package_parser_filters_noise_and_sorts() -> None:
    assert AdbDeviceEngine.parse_installed_packages(
        "package:com.zed.app\nwarning\npackage:com.alpha.app\n"
        "package:com.zed.app\npackage:bad-name\n"
    ) == ["com.alpha.app", "com.zed.app"]


def test_list_installed_packages_defaults_to_user_apps() -> None:
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
    engine.shell_text = Mock(return_value="package:com.example.app")  # type: ignore[method-assign]

    assert engine.list_installed_packages(_session_data()) == ["com.example.app"]
    engine.shell_text.assert_called_once_with(
        "emulator-5554",
        "pm",
        "list",
        "packages",
        "-3",
        timeout=15,
    )


@pytest.mark.parametrize(
    "value",
    ["", "Main Activity", "com.example/.MainActivity", "MainActivity;stop"],
)
def test_activity_validation_rejects_unsafe_names(value: str) -> None:
    with pytest.raises(ValueError):
        AdbDeviceEngine.validate_activity_name(value)


@pytest.mark.parametrize("value", ["", "example.com/page", "https://bad link"])
def test_deep_link_validation_requires_safe_uri(value: str) -> None:
    with pytest.raises(ValueError):
        AdbDeviceEngine.validate_deep_link(value)


def test_start_activity_uses_explicit_component_and_waits() -> None:
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
    engine.shell_text = Mock(return_value="Status: ok")  # type: ignore[method-assign]
    engine.wait_for_foreground_package = Mock()  # type: ignore[method-assign]

    engine.start_activity(
        _session_data(),
        "com.example.app",
        ".MainActivity",
        timeout=2.2,
    )

    engine.shell_text.assert_called_once_with(
        "emulator-5554",
        "am",
        "start",
        "-W",
        "-n",
        "com.example.app/.MainActivity",
        timeout=3,
    )
    engine.wait_for_foreground_package.assert_called_once_with(
        _session_data(),
        "com.example.app",
        timeout=2.2,
    )


def test_activity_manager_error_is_not_hidden_by_zero_exit_code() -> None:
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
    engine.shell_text = Mock(return_value="Error: Activity class does not exist")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Activity class"):
        engine.start_activity(
            _session_data(),
            "com.example.app",
            ".MissingActivity",
        )


def test_open_deep_link_constrains_package_with_separate_arguments() -> None:
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
    engine.shell_text = Mock(return_value="Status: ok")  # type: ignore[method-assign]
    engine.wait_for_foreground_package = Mock()  # type: ignore[method-assign]

    engine.open_deep_link(
        _session_data(),
        "myapp://orders/42?source=test",
        package_name="com.example.app",
        timeout=4,
    )

    engine.shell_text.assert_called_once_with(
        "emulator-5554",
        "am",
        "start",
        "-W",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        "myapp://orders/42?source=test",
        "-p",
        "com.example.app",
        timeout=4,
    )


def test_install_apk_uses_selected_device_and_explicit_flags(tmp_path: Path) -> None:
    apk = tmp_path / "sample.apk"
    apk.write_bytes(b"apk")
    engine = AdbDeviceEngine(adb_path="selected-adb.exe", emulator_providers=[])
    completed = SimpleNamespace(returncode=0, stdout="Success\n", stderr="")

    with patch("automadin.engine.adb_engine.subprocess.run", return_value=completed) as run:
        assert engine.install_apk(
            _session_data(),
            apk,
            replace_existing=True,
            grant_permissions=True,
            timeout=45,
        ) == "Success"

    command = run.call_args.args[0]
    assert command == [
        "selected-adb.exe",
        "-s",
        "emulator-5554",
        "install",
        "-r",
        "-g",
        str(apk.resolve()),
    ]
    assert run.call_args.kwargs["timeout"] == 45


def test_install_apk_rejects_missing_or_non_apk_paths(tmp_path: Path) -> None:
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
    with pytest.raises(FileNotFoundError):
        engine.install_apk(_session_data(), tmp_path / "missing.apk")
    text = tmp_path / "not-an-apk.txt"
    text.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        engine.install_apk(_session_data(), text)


def test_ensure_foreground_only_launches_when_needed() -> None:
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
    engine.foreground_package = Mock(return_value="com.example.app")  # type: ignore[method-assign]
    engine.launch_package = Mock()  # type: ignore[method-assign]
    engine.wait_for_foreground_package = Mock()  # type: ignore[method-assign]

    assert not engine.ensure_package_foreground(
        _session_data(),
        "com.example.app",
    )
    engine.launch_package.assert_not_called()

    engine.foreground_package = Mock(return_value="com.other.app")  # type: ignore[method-assign]
    assert engine.ensure_package_foreground(
        _session_data(),
        "com.example.app",
        timeout=9,
    )
    engine.launch_package.assert_called_once_with(_session_data(), "com.example.app")
    engine.wait_for_foreground_package.assert_called_once_with(
        _session_data(),
        "com.example.app",
        timeout=9,
    )


def test_restart_force_stops_before_launch_and_wait() -> None:
    engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
    events: list[str] = []
    engine.force_stop_package = Mock(side_effect=lambda *_args: events.append("stop"))  # type: ignore[method-assign]
    engine.launch_package = Mock(side_effect=lambda *_args: events.append("launch"))  # type: ignore[method-assign]
    engine.wait_for_foreground_package = Mock(
        side_effect=lambda *_args, **_kwargs: events.append("wait")
    )  # type: ignore[method-assign]

    engine.restart_package(_session_data(), "com.example.app", timeout=8)

    assert events == ["stop", "launch", "wait"]


def test_android_handlers_use_session_adb_and_convert_timeouts() -> None:
    controller = Mock(spec=AndroidDeviceController)
    controller.ensure_package_foreground.return_value = True
    context = _context(controller=controller)

    handle_android_install_apk(
        FlowCommand(
            "android_install_apk",
            params={"apk_path": "app.apk", "timeout": 1501},
        ),
        context,
    )
    handle_android_launch_app(
        FlowCommand(
            "android_launch_app",
            params={"package_name": "com.example.app", "timeout": 2500},
        ),
        context,
    )
    handle_android_force_stop_app(
        FlowCommand(
            "android_force_stop_app",
            params={"package_name": "com.example.app"},
        ),
        context,
    )
    handle_android_restart_app(
        FlowCommand(
            "android_restart_app",
            params={"package_name": "com.example.app", "timeout": 3500},
        ),
        context,
    )
    handle_android_start_activity(
        FlowCommand(
            "android_start_activity",
            params={
                "package_name": "com.example.app",
                "activity_name": ".MainActivity",
                "timeout": 4500,
            },
        ),
        context,
    )
    handle_android_open_deep_link(
        FlowCommand(
            "android_open_deep_link",
            params={
                "uri": "myapp://home",
                "package_name": "",
                "timeout": 5500,
            },
        ),
        context,
    )

    assert controller.install_apk.call_args.kwargs["timeout"] == 2
    controller.ensure_package_foreground.assert_called_once_with(
        "com.example.app",
        timeout=2.5,
    )
    controller.force_stop_package.assert_called_once_with("com.example.app")
    controller.restart_package.assert_called_once_with(
        "com.example.app",
        timeout=3.5,
    )
    controller.start_activity.assert_called_once_with(
        "com.example.app",
        ".MainActivity",
        timeout=4.5,
    )
    controller.open_deep_link.assert_called_once_with(
        "myapp://home",
        package_name="",
        timeout=5.5,
    )


def test_android_handlers_reject_non_android_targets() -> None:
    with pytest.raises(RuntimeError, match="require an Android device"):
        handle_android_launch_app(
            FlowCommand(
                "android_launch_app",
                params={"package_name": "com.example.app"},
            ),
            _context(WindowsDevice(123, "Window")),
        )


def test_android_commands_are_android_only_in_preflight() -> None:
    command_ids = (
        "android_install_apk",
        "android_launch_app",
        "android_force_stop_app",
        "android_restart_app",
        "android_start_activity",
        "android_open_deep_link",
    )
    assert all(supported_targets(command_id) == {TargetKind.ANDROID} for command_id in command_ids)

    report = preflight_flow(
        AutomationFlow(
            "Wrong target",
            [
                FlowCommand(
                    "android_launch_app",
                    params={"package_name": "com.example.app"},
                )
            ],
        ),
        target=WindowsDevice(123, "Window"),
        mode=PreflightMode.EXECUTION,
    )
    assert [issue.code for issue in report.errors] == ["UNSUPPORTED_TARGET"]
