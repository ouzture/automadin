from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from automadin.execution.context import FlowContext
from automadin.execution.handlers.windows import (
    handle_win_key,
    handle_win_scroll,
    handle_win_type,
)
from automadin.models.device import DeviceSession, WindowsDevice
from automadin.models.flow import FlowCommand


def _windows_context() -> FlowContext:
    return FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        )
    )


def _windows_runtime(user32: Mock):
    user32.IsWindow.return_value = True
    user32.GetForegroundWindow.return_value = 10
    return (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch(
            "automadin.execution.handlers.windows.ctypes.windll",
            SimpleNamespace(user32=user32),
            create=True,
        ),
    )


def test_win_key_uses_catalog_keys_as_simultaneous_chord() -> None:
    user32 = Mock()
    platform_patch, data_patch, windll_patch = _windows_runtime(user32)
    command = FlowCommand(
        "win_key",
        params={
            "keys": ["Control", "a"],
            "mode": "simultaneous",
            "delivery_mode": "message",
        },
    )

    with (
        platform_patch,
        data_patch,
        windll_patch,
        patch("automadin.execution.handlers.windows.press_window_keys") as press_keys,
    ):
        handle_win_key(command, _windows_context())

    press_keys.assert_called_once_with(
        10,
        [0x11, ord("A")],
        sequence_mode="simultaneous",
        delivery_mode="message",
        pause_ms=0,
        focus_delay_ms=200,
        restore_delay_ms=100,
    )


def test_win_key_passes_physical_delivery_and_timing_options() -> None:
    user32 = Mock()
    platform_patch, data_patch, windll_patch = _windows_runtime(user32)
    with (
        platform_patch,
        data_patch,
        windll_patch,
        patch("automadin.execution.handlers.windows.press_window_keys") as press_keys,
    ):
        handle_win_key(
            FlowCommand(
                "win_key",
                params={
                    "key": "Enter",
                    "delivery_mode": "input",
                    "focus_delay_ms": 350,
                    "restore_delay_ms": 175,
                },
            ),
            _windows_context(),
        )

    press_keys.assert_called_once_with(
        10,
        [0x0D],
        sequence_mode="simultaneous",
        delivery_mode="input",
        pause_ms=0,
        focus_delay_ms=350,
        restore_delay_ms=175,
    )


def test_win_key_supports_sequential_mode_and_legacy_key() -> None:
    user32 = Mock()
    platform_patch, data_patch, windll_patch = _windows_runtime(user32)

    with (
        platform_patch,
        data_patch,
        windll_patch,
        patch("automadin.execution.handlers.windows.press_window_keys") as press_keys,
    ):
        handle_win_key(
            FlowCommand(
                "win_key",
                params={"keys": ["ArrowUp", "Enter"], "mode": "sequential", "pause_ms": 25},
            ),
            _windows_context(),
        )
        handle_win_key(FlowCommand("win_key", params={"key": "Escape"}), _windows_context())

    assert press_keys.call_args_list == [
        call(
            10,
            [0x26, 0x0D],
            sequence_mode="sequential",
            delivery_mode="background_key",
            pause_ms=25,
            focus_delay_ms=200,
            restore_delay_ms=100,
        ),
        call(
            10,
            [0x1B],
            sequence_mode="simultaneous",
            delivery_mode="background_key",
            pause_ms=0,
            focus_delay_ms=200,
            restore_delay_ms=100,
        ),
    ]


def test_win_key_rejects_unsupported_catalog_value() -> None:
    user32 = Mock()
    platform_patch, data_patch, windll_patch = _windows_runtime(user32)

    with platform_patch, data_patch, windll_patch:
        with pytest.raises(RuntimeError, match="unsupported key 'AudioVolumeUp'"):
            handle_win_key(
                FlowCommand("win_key", params={"keys": ["AudioVolumeUp"]}),
                _windows_context(),
            )


def test_win_type_passes_background_delivery_and_delay_range() -> None:
    user32 = Mock()
    platform_patch, data_patch, windll_patch = _windows_runtime(user32)
    command = FlowCommand(
        "win_type",
        params={
            "text": "ab",
            "min_delay": 10,
            "max_delay": 20,
            "delivery_mode": "message",
        },
    )

    with (
        platform_patch,
        data_patch,
        windll_patch,
        patch("automadin.execution.handlers.windows.type_window_text") as type_text,
    ):
        handle_win_type(command, _windows_context())

    type_text.assert_called_once_with(
        10,
        "ab",
        delivery_mode="message",
        min_delay_ms=10,
        max_delay_ms=20,
        focus_delay_ms=200,
        restore_delay_ms=100,
    )


def test_win_type_legacy_delay_is_fixed_and_invalid_range_is_rejected() -> None:
    user32 = Mock()
    platform_patch, data_patch, windll_patch = _windows_runtime(user32)

    with (
        platform_patch,
        data_patch,
        windll_patch,
        patch("automadin.execution.handlers.windows.type_window_text") as type_text,
    ):
        handle_win_type(
            FlowCommand("win_type", params={"text": "x", "delay_ms": 15}),
            _windows_context(),
        )
    type_text.assert_called_once_with(
        10,
        "x",
        delivery_mode="background_text",
        min_delay_ms=15,
        max_delay_ms=15,
        focus_delay_ms=200,
        restore_delay_ms=100,
    )

    platform_patch, data_patch, windll_patch = _windows_runtime(user32)
    with platform_patch, data_patch, windll_patch:
        with pytest.raises(RuntimeError, match="max_delay.*less than.*min_delay"):
            handle_win_type(
                FlowCommand(
                    "win_type",
                    params={"text": "x", "min_delay": 20, "max_delay": 10},
                ),
                _windows_context(),
            )


def test_win_scroll_passes_background_delivery_to_selected_window() -> None:
    user32 = Mock()
    platform_patch, data_patch, windll_patch = _windows_runtime(user32)
    rect = SimpleNamespace(left=100, top=200, right=900, bottom=800)

    with (
        platform_patch,
        data_patch,
        windll_patch,
        patch("automadin.execution.handlers.windows._window_rect", return_value=rect),
        patch("automadin.execution.handlers.windows.scroll_window") as scroll,
    ):
        handle_win_scroll(
            FlowCommand(
                "win_scroll",
                params={
                    "x": 25,
                    "y": 40,
                    "amount": -3,
                    "delivery_mode": "message",
                },
            ),
            _windows_context(),
        )

    scroll.assert_called_once_with(
        10,
        25,
        40,
        -3,
        delivery_mode="message",
        focus_delay_ms=200,
        restore_delay_ms=100,
    )
