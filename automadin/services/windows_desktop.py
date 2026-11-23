"""Safe, injectable checks for the Windows input desktop."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import struct
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class WindowsDesktopState(str, Enum):
    """The state that matters to foreground Windows UI automation."""

    INTERACTIVE = "interactive"
    NON_INTERACTIVE = "non_interactive"
    UNKNOWN = "unknown"


class WindowsSessionLockState(str, Enum):
    """The lock flag reported for the current Windows session."""

    LOCKED = "locked"
    UNLOCKED = "unlocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WindowsDesktopCheck:
    """A user-readable, fail-closed result from the desktop probe."""

    state: WindowsDesktopState
    detail: str = ""

    @property
    def is_interactive(self) -> bool:
        return self.state is WindowsDesktopState.INTERACTIVE


# These commands inject input or manipulate foreground state. Read-only
# capture and image/OCR inspection deliberately do not belong here: packaged
# field evidence confirms that Screenshot works against a locked workstation.
_WINDOWS_INTERACTIVE_COMMANDS = frozenset(
    {
        "clear_text_field",
        "click_position",
        "click_image",
        "click_best_target",
        "click_dynamic_target",
        "press_keycode",
        "swipe",
        "type_text",
        "win_focus",
    }
)


def windows_command_requires_interactive_desktop(
    command_id: object,
    params: Mapping[str, object] | None = None,
) -> bool:
    """Return whether one command needs an interactive Windows desktop.

    The exceptions preserve the documented background ``message`` mode for
    coordinate click/drag commands and allow app launch without focusing it.
    """
    if not isinstance(command_id, str):
        return False
    if command_id in _WINDOWS_INTERACTIVE_COMMANDS:
        return True
    values = params or {}
    if command_id in {"win_key", "win_scroll", "win_type"}:
        return values.get("delivery_mode", "message") == "input"
    if command_id == "win_click":
        return values.get("mode", "message") != "message"
    if command_id == "win_drag":
        return values.get("mode", "input") != "message"
    if command_id == "win_click_image":
        return values.get("mode", "message") != "message"
    if command_id == "win_drag_image":
        return values.get("mode", "input") != "message"
    if command_id in {"win_if_image", "win_while"}:
        click_if_found = values.get(
            "click_if_found", values.get("clickIfFound", False)
        )
        if click_if_found is False:
            return False
        return values.get("mode", "message") != "message"
    if command_id == "win_ensure_app":
        return values.get("focus", True) is not False
    return False


def detect_windows_session_lock() -> WindowsSessionLockState:
    """Read the current session lock flag through the WTS API.

    ``WTSInfoEx`` provides a direct session lock signal, unlike the input
    desktop name heuristic. The desktop probe uses this as an authoritative
    locked result and retains its name check as a compatibility fallback.
    """
    if sys.platform != "win32":
        return WindowsSessionLockState.UNKNOWN

    try:
        wtsapi32 = ctypes.windll.wtsapi32  # type: ignore[attr-defined]
        query = wtsapi32.WTSQuerySessionInformationW
        free = wtsapi32.WTSFreeMemory
        query.argtypes = (
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.wintypes.DWORD),
        )
        query.restype = ctypes.wintypes.BOOL
        free.argtypes = (ctypes.c_void_p,)
        free.restype = None

        buffer = ctypes.c_void_p()
        returned = ctypes.wintypes.DWORD()
        # WTSInfoEx = 25, WTS_CURRENT_SESSION = 0xffffffff.
        if not query(
            None,
            0xFFFFFFFF,
            25,
            ctypes.byref(buffer),
            ctypes.byref(returned),
        ):
            return WindowsSessionLockState.UNKNOWN
        try:
            if not buffer.value or returned.value < 16:
                return WindowsSessionLockState.UNKNOWN
            # WTSINFOEX.Level is followed by the level-1 union. The first
            # level-1 fields are SessionId, SessionState, SessionFlags.
            level, _session_id, _session_state, session_flags = struct.unpack(
                "<IIII", ctypes.string_at(buffer, 16)
            )
        finally:
            free(buffer)
    except Exception:
        return WindowsSessionLockState.UNKNOWN

    if level != 1:
        return WindowsSessionLockState.UNKNOWN
    if session_flags == 0:  # WTS_SESSIONSTATE_LOCK
        return WindowsSessionLockState.LOCKED
    if session_flags == 1:  # WTS_SESSIONSTATE_UNLOCK
        return WindowsSessionLockState.UNLOCKED
    return WindowsSessionLockState.UNKNOWN


def detect_windows_desktop() -> WindowsDesktopCheck:
    """Return whether the current process can access the interactive input desktop.

    The WTS session-lock signal is authoritative when available. The input
    desktop name remains a second check because it also catches secure or
    otherwise non-interactive desktops. Any platform or API uncertainty is
    returned as ``UNKNOWN`` so scheduled Windows UI runs fail closed.
    """
    if sys.platform != "win32":
        return WindowsDesktopCheck(
            WindowsDesktopState.UNKNOWN,
            "Windows desktop APIs are unavailable on this platform.",
        )

    session_lock = detect_windows_session_lock()
    if session_lock is WindowsSessionLockState.LOCKED:
        return WindowsDesktopCheck(
            WindowsDesktopState.NON_INTERACTIVE,
            "The current Windows session is locked.",
        )

    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        open_input_desktop = user32.OpenInputDesktop
        open_input_desktop.argtypes = (
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        )
        open_input_desktop.restype = ctypes.wintypes.HANDLE
        get_user_object_information = user32.GetUserObjectInformationW
        get_user_object_information.argtypes = (
            ctypes.wintypes.HANDLE,
            ctypes.c_int,
            ctypes.wintypes.LPVOID,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        )
        get_user_object_information.restype = ctypes.wintypes.BOOL
        close_desktop = user32.CloseDesktop
        close_desktop.argtypes = (ctypes.wintypes.HANDLE,)
        close_desktop.restype = ctypes.wintypes.BOOL

        desktop = open_input_desktop(0, False, 0x0001 | 0x0100)
        if not desktop:
            return WindowsDesktopCheck(
                WindowsDesktopState.UNKNOWN,
                "Windows could not open the current input desktop.",
            )

        try:
            required = ctypes.wintypes.DWORD()
            get_user_object_information(
                desktop,
                2,  # UOI_NAME
                None,
                0,
                ctypes.byref(required),
            )
            if not required.value:
                return WindowsDesktopCheck(
                    WindowsDesktopState.UNKNOWN,
                    "Windows could not read the current desktop name.",
                )
            name = ctypes.create_unicode_buffer(required.value)
            if not get_user_object_information(
                desktop,
                2,  # UOI_NAME
                name,
                ctypes.sizeof(name),
                ctypes.byref(required),
            ):
                return WindowsDesktopCheck(
                    WindowsDesktopState.UNKNOWN,
                    "Windows could not read the current desktop name.",
                )
        finally:
            close_desktop(desktop)
    except Exception as exc:  # OS/API failures must never open the safety gate.
        return WindowsDesktopCheck(
            WindowsDesktopState.UNKNOWN,
            f"Windows desktop detection failed ({type(exc).__name__}).",
        )

    desktop_name = name.value.strip().casefold()
    if desktop_name == "default":
        if session_lock is WindowsSessionLockState.UNKNOWN:
            # WTS is expected on supported Windows versions. Do not silently
            # convert a failed direct lock check into permission to inject
            # input; use the legacy fallback only when the DLL is absent.
            try:
                ctypes.windll.wtsapi32  # type: ignore[attr-defined]
            except AttributeError:
                return WindowsDesktopCheck(WindowsDesktopState.INTERACTIVE)
            return WindowsDesktopCheck(
                WindowsDesktopState.UNKNOWN,
                "Windows could not verify the current session lock state.",
            )
        return WindowsDesktopCheck(WindowsDesktopState.INTERACTIVE)
    if desktop_name:
        return WindowsDesktopCheck(
            WindowsDesktopState.NON_INTERACTIVE,
            f"The current Windows desktop is '{name.value.strip()}'.",
        )
    return WindowsDesktopCheck(
        WindowsDesktopState.UNKNOWN,
        "Windows returned an empty desktop name.",
    )


__all__ = [
    "WindowsDesktopCheck",
    "WindowsDesktopState",
    "WindowsSessionLockState",
    "detect_windows_desktop",
    "detect_windows_session_lock",
    "windows_command_requires_interactive_desktop",
]
