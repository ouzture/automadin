from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automadin.execution.build_executor import build_executor
from automadin.execution.context import FlowContext
from automadin.models.device import DeviceSession, WindowsDevice
from automadin.models.flow import FlowCommand


def _windows_context() -> FlowContext:
    session = DeviceSession(
        device=WindowsDevice(hwnd=10, title="Game"),
        data={"windows_data": {"hwnd": 10, "title": "Game"}},
    )
    return FlowContext(session)


def _windows_handlers():
    from automadin.execution.handlers import windows

    return windows.handle_win_drag, windows.handle_win_drag_image


class WindowsDragCommandTests(unittest.TestCase):
    def test_win_drag_offsets_position_start_and_calls_drag_window(self) -> None:
        handle_win_drag, _handle_win_drag_image = _windows_handlers()
        cmd = FlowCommand(
            "win_drag",
            params={
                "coordinate_unit": "pixels",
                "x": 25,
                "y": 40,
                "offset_unit": "pixels",
                "xoffset": 30,
                "yoffset": -20,
                "button": "right",
                "mode": "cursor",
                "drag_duration_ms": 450,
                "drag_steps": 5,
                "focus_delay_ms": 150,
                "restore_delay_ms": 80,
            },
        )

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch("automadin.execution.handlers.windows.drag_window") as drag,
        ):
            handle_win_drag(cmd, _windows_context())

        drag.assert_called_once_with(
            10,
            25,
            40,
            55,
            20,
            button="right",
            mode="cursor",
            drag_duration_ms=450,
            steps=5,
            focus_delay_ms=150,
            restore_delay_ms=80,
        )

    def test_win_drag_image_uses_matched_image_start_plus_drag_offset(self) -> None:
        _handle_win_drag, handle_win_drag_image = _windows_handlers()
        cmd = FlowCommand(
            "win_drag_image",
            params={
                "images": ["target"],
                "threshold": 0.8,
                "start_offset_unit": "pixels",
                "xmargin": 2,
                "ymargin": -3,
                "offset_unit": "pixels",
                "xoffset": 30,
                "yoffset": -20,
                "button": "left",
                "mode": "input",
            },
        )
        result = SimpleNamespace(center_x=100, center_y=200)

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch("automadin.execution.handlers.windows._take_screenshot_for_device", return_value=b"png"),
            patch("automadin.execution.handlers.windows._find_any_image", return_value=("target", result)),
            patch("automadin.execution.handlers.windows.drag_window") as drag,
        ):
            handle_win_drag_image(cmd, _windows_context())

        drag.assert_called_once_with(
            10,
            102,
            197,
            132,
            177,
            button="left",
            mode="input",
            drag_duration_ms=300,
            steps=12,
            focus_delay_ms=200,
            restore_delay_ms=100,
        )

    def test_executor_registers_windows_drag_handlers(self) -> None:
        _windows_handlers()
        executor = build_executor()

        self.assertIn("win_drag", executor._handlers)
        self.assertIn("win_drag_image", executor._handlers)

    def test_command_metadata_includes_windows_drag_commands(self) -> None:
        path = Path("automadin/resources/command_types.json")
        command_types = json.loads(path.read_text(encoding="utf-8"))
        ids = {entry["id"] for entry in command_types}

        self.assertIn("win_drag", ids)
        self.assertIn("win_drag_image", ids)


if __name__ == "__main__":
    unittest.main()
