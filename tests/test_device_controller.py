from __future__ import annotations

import subprocess
from dataclasses import dataclass
from unittest.mock import Mock, call

from automadin.engine.device_controller import (
    AndroidDeviceController,
    ScreenshotCapability,
    TapCapability,
    get_device_capability,
    get_device_controller,
)
from automadin.engine.session import DeviceSessionService
from automadin.execution.context import FlowContext
from automadin.execution.handlers.click import handle_click_position
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.models.flow import FlowCommand
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


def _adb_data() -> dict[str, object]:
    return {"adb_path": "selected-adb.exe", "device_id": "emulator-5554"}


def test_legacy_android_session_lazily_receives_one_cached_controller() -> None:
    session = DeviceSession(
        device=AndroidDevice("emulator-5554"),
        data={"adb_data": _adb_data()},
    )

    first = get_device_controller(session)
    second = get_device_controller(session)

    assert isinstance(first, AndroidDeviceController)
    assert second is first
    assert get_device_capability(session, ScreenshotCapability) is first


def test_android_readiness_replaces_controller_with_fresh_session_data() -> None:
    first_data = {"adb_path": "adb", "device_id": "first-endpoint"}
    second_data = {"adb_path": "adb", "device_id": "second-endpoint"}
    engine = Mock()
    engine.connect.side_effect = [first_data, second_data]
    service = DeviceSessionService(adb_engine=engine)
    device = AndroidDevice("saved-device")

    session = service.ensure_android_session(device)
    first = get_device_controller(session)
    refreshed = service.ensure_android_session(device)
    second = get_device_controller(refreshed)

    assert refreshed is session
    assert isinstance(first, AndroidDeviceController)
    assert isinstance(second, AndroidDeviceController)
    assert second is not first
    assert second.session_data is second_data
    assert second.engine is engine


def test_android_controller_translates_tap_swipe_text_clear_and_keys_to_adb() -> None:
    run = Mock()
    sleep = Mock()
    controller = AndroidDeviceController(
        Mock(),
        _adb_data(),
        run_command=run,
        sleep=sleep,
    )

    controller.tap(10, 20, click_type="double", double_tap_interval_s=0.15)
    controller.swipe(1, 2, 3, 4, duration_ms=12000)
    controller.type_text("hello 'world'", min_delay_ms=0, max_delay_ms=0)
    controller.clear_text_field()
    controller.press_keys(["A", "B"], mode="sequential", pause_ms=250)

    assert run.call_args_list[0] == call(
        [
            "selected-adb.exe",
            "-s",
            "emulator-5554",
            "shell",
            "input",
            "swipe",
            "10",
            "20",
            "10",
            "20",
            "100",
        ],
        timeout=5,
        creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
    )
    assert run.call_args_list[2].kwargs["timeout"] == 17
    assert run.call_args_list[3].args[0][-3:] == ["input", "text", "hello%s\\'world\\'"]
    assert run.call_args_list[4].args[0][-3:] == [
        "input",
        "keyevent",
        "KEYCODE_MOVE_HOME",
    ]
    assert sleep.call_args_list == [call(0.15), call(0.25), call(0.25)]


def test_android_controller_reads_surface_size_and_delegates_screenshot() -> None:
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            0,
            "Physical size: 1080x2400\n",
            "",
        )
    )
    engine = Mock()
    engine.take_screenshot.return_value = b"png"
    controller = AndroidDeviceController(engine, _adb_data(), run_command=run)

    assert controller.get_surface_size() == (1080, 2400)
    assert controller.take_screenshot() == b"png"
    engine.take_screenshot.assert_called_once_with(controller.session_data)


def test_android_controller_allows_adb_hold_command_to_finish() -> None:
    run = Mock()
    controller = AndroidDeviceController(
        Mock(),
        _adb_data(),
        run_command=run,
    )

    controller.tap(10, 20, hold_duration_ms=60_000)

    run.assert_called_once_with(
        [
            "selected-adb.exe",
            "-s",
            "emulator-5554",
            "shell",
            "input",
            "swipe",
            "10",
            "20",
            "10",
            "20",
            "60000",
        ],
        timeout=65,
        creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
    )


@dataclass
class _FutureDevice:
    id: str = "future-1"
    name: str = "Future phone"


class _FutureTapController:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, int, float]] = []

    def tap(
        self,
        x: int,
        y: int,
        *,
        click_type: str = "single",
        hold_duration_ms: int = 0,
        double_tap_interval_s: float = 0.1,
    ) -> None:
        self.calls.append(
            (x, y, click_type, hold_duration_ms, double_tap_interval_s)
        )


def test_click_handler_accepts_a_capability_without_a_known_device_type() -> None:
    controller = _FutureTapController()
    session = DeviceSession(
        device=_FutureDevice(),
        data={"device_controller": controller},
    )

    handle_click_position(
        FlowCommand(
            "click_position",
            params={"x": 12, "y": 34, "click": "double"},
        ),
        FlowContext(session),
    )

    assert isinstance(controller, TapCapability)
    assert controller.calls == [(12, 34, "double", 0, 0.15)]
