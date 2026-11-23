from __future__ import annotations

import ctypes
import struct
from types import SimpleNamespace

from automadin.services import windows_desktop
from automadin.services.windows_desktop import (
    WindowsDesktopState,
    WindowsSessionLockState,
    detect_windows_desktop,
    detect_windows_session_lock,
    windows_command_requires_interactive_desktop,
)


def test_windows_desktop_detection_is_unknown_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(windows_desktop.sys, "platform", "linux")

    result = detect_windows_desktop()

    assert result.state is WindowsDesktopState.UNKNOWN


def test_windows_desktop_detection_fails_closed_on_os_api_error(monkeypatch) -> None:
    user32 = SimpleNamespace(
        OpenInputDesktop=lambda *_args: (_ for _ in ()).throw(OSError("denied")),
    )
    monkeypatch.setattr(windows_desktop.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_desktop.ctypes,
        "windll",
        SimpleNamespace(user32=user32),
        raising=False,
    )

    result = detect_windows_desktop()

    assert result.state is WindowsDesktopState.UNKNOWN


def test_windows_interactive_capability_preserves_background_modes() -> None:
    # Screenshot is a read-only capture and is intentionally lock-compatible.
    assert not windows_command_requires_interactive_desktop("screenshot")
    assert not windows_command_requires_interactive_desktop("find_image")
    assert not windows_command_requires_interactive_desktop("read_screen_text")
    assert windows_command_requires_interactive_desktop("click_position")
    assert windows_command_requires_interactive_desktop("clear_text_field")
    assert windows_command_requires_interactive_desktop("press_keycode")
    assert windows_command_requires_interactive_desktop("swipe")
    assert not windows_command_requires_interactive_desktop("wait")
    assert not windows_command_requires_interactive_desktop(
        "win_click", {"mode": "message"}
    )
    assert windows_command_requires_interactive_desktop(
        "win_click", {"mode": "input"}
    )
    assert not windows_command_requires_interactive_desktop(
        "win_click_image", {"mode": "message"}
    )
    assert windows_command_requires_interactive_desktop(
        "win_click_image", {"mode": "input"}
    )
    for command_id in ("win_key", "win_scroll", "win_type"):
        assert not windows_command_requires_interactive_desktop(
            command_id, {"delivery_mode": "message"}
        )
        assert windows_command_requires_interactive_desktop(
            command_id, {"delivery_mode": "input"}
        )
        assert not windows_command_requires_interactive_desktop(command_id, {})
    assert windows_command_requires_interactive_desktop("win_drag_image", {})
    assert not windows_command_requires_interactive_desktop(
        "win_drag_image", {"mode": "message"}
    )
    assert not windows_command_requires_interactive_desktop(
        "win_if_image", {"click_if_found": False}
    )
    assert windows_command_requires_interactive_desktop(
        "win_if_image", {"click_if_found": True, "mode": "input"}
    )
    assert not windows_command_requires_interactive_desktop(
        "win_drag", {"mode": "message"}
    )
    assert windows_command_requires_interactive_desktop("win_drag", {})
    assert not windows_command_requires_interactive_desktop(
        "win_ensure_app", {"focus": False}
    )
    assert windows_command_requires_interactive_desktop(
        "win_ensure_app", {"focus": True}
    )


def _fake_wts_api(session_flags: int):
    payload = ctypes.create_string_buffer(
        struct.pack("<IIII", 1, 7, 0, session_flags)
    )

    def query(_server, _session, _info_class, buffer, returned):
        buffer._obj.value = ctypes.cast(payload, ctypes.c_void_p).value
        returned._obj.value = len(payload)
        return True

    def free(_buffer):
        return None

    return SimpleNamespace(
        WTSQuerySessionInformationW=query,
        WTSFreeMemory=free,
    )


def test_wts_session_lock_signal_is_injectable(monkeypatch) -> None:
    monkeypatch.setattr(windows_desktop.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_desktop.ctypes,
        "windll",
        SimpleNamespace(wtsapi32=_fake_wts_api(0)),
        raising=False,
    )

    assert detect_windows_session_lock() is WindowsSessionLockState.LOCKED

    monkeypatch.setattr(
        windows_desktop.ctypes,
        "windll",
        SimpleNamespace(wtsapi32=_fake_wts_api(1)),
        raising=False,
    )
    assert detect_windows_session_lock() is WindowsSessionLockState.UNLOCKED


def test_wts_session_lock_detection_fails_closed_on_malformed_response(
    monkeypatch,
) -> None:
    payload = ctypes.create_string_buffer(b"bad")

    def query(_server, _session, _info_class, buffer, returned):
        buffer._obj.value = ctypes.cast(payload, ctypes.c_void_p).value
        returned._obj.value = len(payload)
        return True

    monkeypatch.setattr(windows_desktop.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_desktop.ctypes,
        "windll",
        SimpleNamespace(
            wtsapi32=SimpleNamespace(
                WTSQuerySessionInformationW=query,
                WTSFreeMemory=lambda _buffer: None,
            )
        ),
        raising=False,
    )

    assert detect_windows_session_lock() is WindowsSessionLockState.UNKNOWN
