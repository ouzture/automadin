"""Windows mouse input with background message and physical fallback modes."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


WM_MOUSEMOVE = 0x0200
WM_CANCELMODE = 0x001F
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MBUTTONDBLCLK = 0x0209
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_CHAR = 0x0102
WM_MOUSEWHEEL = 0x020A
WM_ACTIVATE = 0x0006
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
WM_NCACTIVATE = 0x0086
WM_ACTIVATEAPP = 0x001C

WA_INACTIVE = 0
WA_ACTIVE = 1

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

SW_RESTORE = 9
VK_MENU = 0x12
VK_SHIFT = 0x10
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
CWP_SKIPTRANSPARENT = 0x0004
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
GA_ROOT = 2
_EXTENDED_VIRTUAL_KEYS = {
    0x03,  # Cancel
    0x21, 0x22, 0x23, 0x24,  # Page Up/Down, End, Home
    0x25, 0x26, 0x27, 0x28,  # Arrow keys
    0x2D, 0x2E,  # Insert, Delete
    0x5B, 0x5C, 0x5D,  # Windows and Application keys
    0x6F,  # Numpad Divide
    0x90,  # Num Lock
    0xA3, 0xA5,  # Right Control, Right Alt
}


class WindowsInputPrivilegeError(RuntimeError):
    """The target runs at a higher Windows integrity level than Automadin."""


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.wintypes.DWORD),
    ]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("hwndActive", ctypes.wintypes.HWND),
        ("hwndFocus", ctypes.wintypes.HWND),
        ("hwndCapture", ctypes.wintypes.HWND),
        ("hwndMenuOwner", ctypes.wintypes.HWND),
        ("hwndMoveSize", ctypes.wintypes.HWND),
        ("hwndCaret", ctypes.wintypes.HWND),
        ("rcCaret", ctypes.wintypes.RECT),
    ]


def _process_integrity_rid(process_id: int) -> int | None:
    """Return a process token's mandatory integrity RID, when readable."""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = ctypes.wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.wintypes.DWORD)

    process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        int(process_id),
    )
    if not process:
        return None
    token = ctypes.wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return None
        required = ctypes.wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            TOKEN_INTEGRITY_LEVEL,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0:
            return None
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_INTEGRITY_LEVEL,
            buffer,
            required,
            ctypes.byref(required),
        ):
            return None
        label = ctypes.cast(
            buffer,
            ctypes.POINTER(_TOKEN_MANDATORY_LABEL),
        ).contents
        count_pointer = advapi32.GetSidSubAuthorityCount(label.Label.Sid)
        if not count_pointer or count_pointer[0] <= 0:
            return None
        rid_pointer = advapi32.GetSidSubAuthority(
            label.Label.Sid,
            int(count_pointer[0]) - 1,
        )
        return int(rid_pointer[0]) if rid_pointer else None
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


def _window_integrity_rid(hwnd: int) -> int | None:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    process_id = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return None
    return _process_integrity_rid(int(process_id.value))


def _current_integrity_rid() -> int | None:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    return _process_integrity_rid(int(kernel32.GetCurrentProcessId()))


def ensure_windows_input_access(hwnd: int) -> None:
    """Raise an actionable error when Windows UIPI will reject target input."""
    target_rid = _window_integrity_rid(hwnd)
    current_rid = _current_integrity_rid()
    if target_rid is None or current_rid is None or target_rid <= current_rid:
        return

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    length = int(user32.GetWindowTextLengthW(hwnd) or 0)
    title = "the target application"
    if length > 0:
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = f"'{buffer.value.strip()}'" if buffer.value.strip() else title
    raise WindowsInputPrivilegeError(
        f"Windows cannot send input to {title} because it is running as "
        "administrator while Automadin is not. Close Automadin and use "
        "Run as administrator, or relaunch the target application normally."
    )


@dataclass(frozen=True)
class MouseButtonMessages:
    down: int
    up: int
    double: int
    key_state: int
    physical_down: int
    physical_up: int


_BUTTONS = {
    "left": MouseButtonMessages(
        WM_LBUTTONDOWN,
        WM_LBUTTONUP,
        WM_LBUTTONDBLCLK,
        MK_LBUTTON,
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
    ),
    "right": MouseButtonMessages(
        WM_RBUTTONDOWN,
        WM_RBUTTONUP,
        WM_RBUTTONDBLCLK,
        MK_RBUTTON,
        MOUSEEVENTF_RIGHTDOWN,
        MOUSEEVENTF_RIGHTUP,
    ),
    "middle": MouseButtonMessages(
        WM_MBUTTONDOWN,
        WM_MBUTTONUP,
        WM_MBUTTONDBLCLK,
        MK_MBUTTON,
        MOUSEEVENTF_MIDDLEDOWN,
        MOUSEEVENTF_MIDDLEUP,
    ),
}


class WindowsInputBackend(Protocol):
    def window_origin(self, hwnd: int) -> tuple[int, int]: ...

    def resolve_message_target(
        self, hwnd: int, screen_x: int, screen_y: int
    ) -> tuple[int, int, int]: ...

    def post_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool: ...

    def resolve_keyboard_target(self, hwnd: int) -> int: ...

    def key_message_lparam(self, virtual_key: int, *, key_up: bool) -> int: ...

    def key_event(self, virtual_key: int, *, key_up: bool) -> None: ...

    def send_unicode_unit(self, code_unit: int, *, key_up: bool) -> None: ...

    def send_mouse_wheel(self, delta: int) -> None: ...

    def get_foreground_window(self) -> int: ...

    def get_cursor_pos(self) -> tuple[int, int]: ...

    def restore_and_focus(self, hwnd: int) -> None: ...

    def set_cursor_pos(self, x: int, y: int) -> None: ...

    def mouse_event(self, flags: int) -> None: ...

    def send_mouse_input(self, flags: int) -> None: ...


def _activate_foreground_window(user32: object, kernel32: object, hwnd: int) -> None:
    """Activate ``hwnd`` through the current foreground input queue.

    Windows may reject a plain ``SetForegroundWindow`` call made by a worker
    thread that did not receive the user's most recent input. Temporarily
    attaching that thread to the foreground queue provides a bounded,
    reversible handoff without synthesizing keyboard input.
    """
    foreground = int(user32.GetForegroundWindow() or 0)  # type: ignore[attr-defined]
    current_thread = int(kernel32.GetCurrentThreadId())  # type: ignore[attr-defined]
    foreground_thread = (
        int(user32.GetWindowThreadProcessId(foreground, None))  # type: ignore[attr-defined]
        if foreground
        else 0
    )
    target_thread = int(
        user32.GetWindowThreadProcessId(hwnd, None)  # type: ignore[attr-defined]
    )
    attached_threads: list[int] = []
    for thread_id in dict.fromkeys((foreground_thread, target_thread)):
        if thread_id and thread_id != current_thread:
            attached = bool(
                user32.AttachThreadInput(  # type: ignore[attr-defined]
                    current_thread,
                    thread_id,
                    True,
                )
            )
            if attached:
                attached_threads.append(thread_id)
    try:
        user32.BringWindowToTop(hwnd)  # type: ignore[attr-defined]
        user32.SetForegroundWindow(hwnd)  # type: ignore[attr-defined]
        user32.SetActiveWindow(hwnd)  # type: ignore[attr-defined]
        user32.SetFocus(hwnd)  # type: ignore[attr-defined]
    finally:
        for thread_id in reversed(attached_threads):
            user32.AttachThreadInput(  # type: ignore[attr-defined]
                current_thread,
                thread_id,
                False,
            )

    # AttachThreadInput is not sufficient when the foreground process has not
    # granted this normal-integrity process foreground rights. A bounded Alt
    # pulse satisfies Windows' foreground-input rule without requiring the app
    # or the game to run elevated. Only use it after the non-synthetic handoff
    # was rejected.
    if int(user32.GetForegroundWindow() or 0) != hwnd:  # type: ignore[attr-defined]
        user32.keybd_event(VK_MENU, 0, 0, 0)  # type: ignore[attr-defined]
        alt_is_down = True
        try:
            user32.keybd_event(  # type: ignore[attr-defined]
                VK_MENU,
                0,
                KEYEVENTF_KEYUP,
                0,
            )
            alt_is_down = False
            user32.BringWindowToTop(hwnd)  # type: ignore[attr-defined]
            user32.SetForegroundWindow(hwnd)  # type: ignore[attr-defined]
        finally:
            if alt_is_down:
                user32.keybd_event(  # type: ignore[attr-defined]
                    VK_MENU,
                    0,
                    KEYEVENTF_KEYUP,
                    0,
                )


def _pack_point(x: int, y: int) -> int:
    """Pack signed client coordinates into a Win32 mouse-message LPARAM."""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def _button_messages(button: str) -> MouseButtonMessages:
    try:
        return _BUTTONS[str(button).lower()]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported Windows mouse button: {button}") from exc


def click_window(
    hwnd: int,
    x: int,
    y: int,
    *,
    button: str = "left",
    click_type: str = "single",
    mode: str = "message",
    hold_duration_ms: int = 0,
    focus_delay_ms: int = 200,
    click_duration_ms: int = 50,
    restore_delay_ms: int = 100,
    backend: WindowsInputBackend | None = None,
) -> None:
    """Click a full-window-relative point using the requested delivery mode.

    ``message`` posts mouse messages directly to the target window or child and
    does not move the real cursor or change foreground focus. ``cursor`` and
    ``input`` are physical fallbacks that temporarily focus the target, then
    restore the previous foreground window and cursor position.
    """
    if backend is None:
        ensure_windows_input_access(hwnd)
        backend = NativeWindowsInputBackend()
    messages = _button_messages(button)
    click_type = str(click_type).lower()
    mode = str(mode).lower()
    hold_duration_ms = max(0, int(hold_duration_ms))
    focus_delay_ms = max(0, int(focus_delay_ms))
    click_duration_ms = max(0, int(click_duration_ms))
    restore_delay_ms = max(0, int(restore_delay_ms))

    if click_type not in {"single", "double"}:
        raise RuntimeError(f"Unsupported Windows click type: {click_type}")

    origin_x, origin_y = backend.window_origin(hwnd)
    screen_x = origin_x + int(x)
    screen_y = origin_y + int(y)

    if mode == "message":
        _message_click(
            backend,
            hwnd,
            screen_x,
            screen_y,
            messages,
            click_type,
            hold_duration_ms,
        )
        return

    if mode not in {"cursor", "input"}:
        raise RuntimeError(f"Unsupported Windows click mode: {mode}")

    _physical_click(
        backend,
        hwnd,
        screen_x,
        screen_y,
        messages,
        click_type,
        hold_duration_ms,
        use_send_input=mode == "input",
        focus_delay_ms=focus_delay_ms,
        click_duration_ms=click_duration_ms,
        restore_delay_ms=restore_delay_ms,
    )


def drag_window(
    hwnd: int,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    *,
    button: str = "left",
    mode: str = "input",
    drag_duration_ms: int = 300,
    steps: int = 12,
    focus_delay_ms: int = 200,
    restore_delay_ms: int = 100,
    backend: WindowsInputBackend | None = None,
) -> None:
    """Drag from one full-window-relative point to another.

    ``message`` posts mouse movement and button messages without changing
    focus. ``cursor`` and ``input`` use physical mouse state and are more
    compatible with games, but briefly focus the target window.
    """
    if backend is None:
        ensure_windows_input_access(hwnd)
        backend = NativeWindowsInputBackend()
    messages = _button_messages(button)
    mode = str(mode).lower()
    drag_duration_ms = max(0, int(drag_duration_ms))
    steps = max(1, int(steps))
    focus_delay_ms = max(0, int(focus_delay_ms))
    restore_delay_ms = max(0, int(restore_delay_ms))

    origin_x, origin_y = backend.window_origin(hwnd)
    start_screen_x = origin_x + int(start_x)
    start_screen_y = origin_y + int(start_y)
    end_screen_x = origin_x + int(end_x)
    end_screen_y = origin_y + int(end_y)

    if mode == "message":
        _message_drag(
            backend,
            hwnd,
            start_screen_x,
            start_screen_y,
            end_screen_x,
            end_screen_y,
            messages,
            drag_duration_ms,
            steps,
        )
        return

    if mode not in {"cursor", "input"}:
        raise RuntimeError(f"Unsupported Windows drag mode: {mode}")

    _physical_drag(
        backend,
        hwnd,
        start_screen_x,
        start_screen_y,
        end_screen_x,
        end_screen_y,
        messages,
        drag_duration_ms,
        steps,
        use_send_input=mode == "input",
        focus_delay_ms=focus_delay_ms,
        restore_delay_ms=restore_delay_ms,
    )


def press_window_keys(
    hwnd: int,
    virtual_keys: list[int],
    *,
    sequence_mode: str = "simultaneous",
    delivery_mode: str = "message",
    pause_ms: int = 0,
    focus_delay_ms: int = 200,
    restore_delay_ms: int = 100,
    backend: WindowsInputBackend | None = None,
) -> None:
    """Press keys using background window messages or focused physical input.

    Message delivery does not change foreground focus. It works with controls
    that consume ordinary window keyboard messages, but some shortcuts and
    applications using Raw Input or DirectInput require ``input`` delivery.
    """
    if backend is None:
        ensure_windows_input_access(hwnd)
        backend = NativeWindowsInputBackend()
    sequence_mode = str(sequence_mode).lower()
    delivery_mode = str(delivery_mode).lower()
    pause_ms = max(0, int(pause_ms))
    focus_delay_ms = max(0, int(focus_delay_ms))
    restore_delay_ms = max(0, int(restore_delay_ms))

    if sequence_mode not in {"simultaneous", "sequential"}:
        raise RuntimeError(f"Unsupported Windows key sequence mode: {sequence_mode}")
    # ``message`` is the legacy name for the original key-only strategy.
    if delivery_mode == "message":
        delivery_mode = "background_key"
    if delivery_mode == "background_text":
        _message_key_text(backend, hwnd, virtual_keys, pause_ms)
        return
    if delivery_mode in {"background_key", "background_combined"}:
        _message_keys(
            backend,
            hwnd,
            virtual_keys,
            sequence_mode,
            pause_ms,
            include_characters=delivery_mode == "background_combined",
        )
        return
    if delivery_mode == "background_activated":
        _activated_message_delivery(
            backend,
            hwnd,
            lambda: _message_keys(
                backend,
                hwnd,
                virtual_keys,
                sequence_mode,
                pause_ms,
                include_characters=True,
            ),
        )
        return
    if delivery_mode != "input":
        raise RuntimeError(f"Unsupported Windows key delivery mode: {delivery_mode}")
    _physical_keys(
        backend,
        hwnd,
        virtual_keys,
        sequence_mode,
        pause_ms,
        focus_delay_ms,
        restore_delay_ms,
    )


def type_window_text(
    hwnd: int,
    text: str,
    *,
    delivery_mode: str = "message",
    min_delay_ms: int = 0,
    max_delay_ms: int = 0,
    focus_delay_ms: int = 200,
    restore_delay_ms: int = 100,
    backend: WindowsInputBackend | None = None,
) -> None:
    """Type Unicode text into a selected window using message or input delivery."""
    if backend is None:
        ensure_windows_input_access(hwnd)
        backend = NativeWindowsInputBackend()
    delivery_mode = str(delivery_mode).lower()
    min_delay_ms = max(0, int(min_delay_ms))
    max_delay_ms = max(0, int(max_delay_ms))
    focus_delay_ms = max(0, int(focus_delay_ms))
    restore_delay_ms = max(0, int(restore_delay_ms))
    if max_delay_ms < min_delay_ms:
        raise RuntimeError("Maximum text delay cannot be less than minimum delay")

    # ``message`` is the legacy name for the original character-only strategy.
    if delivery_mode == "message":
        delivery_mode = "background_text"
    if delivery_mode == "background_text":
        target = backend.resolve_keyboard_target(hwnd)
        _emit_text(
            text,
            min_delay_ms,
            max_delay_ms,
            lambda unit: _post_character(backend, target, unit),
        )
        return
    if delivery_mode in {"background_key", "background_combined"}:
        _message_type_text(
            backend,
            hwnd,
            text,
            min_delay_ms,
            max_delay_ms,
            include_characters=delivery_mode == "background_combined",
        )
        return
    if delivery_mode == "background_activated":
        _activated_message_delivery(
            backend,
            hwnd,
            lambda: _message_type_text(
                backend,
                hwnd,
                text,
                min_delay_ms,
                max_delay_ms,
                include_characters=True,
            ),
        )
        return
    if delivery_mode != "input":
        raise RuntimeError(f"Unsupported Windows text delivery mode: {delivery_mode}")

    previous_foreground = backend.get_foreground_window()
    focus_changed = previous_foreground != hwnd
    try:
        backend.restore_and_focus(hwnd)
        if not _wait_for_foreground(backend, hwnd, timeout_ms=500):
            raise RuntimeError(
                "Windows refused to focus the target for physical text input. "
                "Use message delivery when the target supports background text."
            )
        if focus_changed and focus_delay_ms > 0:
            time.sleep(focus_delay_ms / 1000.0)

        def send(unit: int) -> None:
            backend.send_unicode_unit(unit, key_up=False)
            backend.send_unicode_unit(unit, key_up=True)

        _emit_text(text, min_delay_ms, max_delay_ms, send)
    finally:
        if (
            focus_changed
            and previous_foreground
            and backend.get_foreground_window() == hwnd
        ):
            if restore_delay_ms > 0:
                time.sleep(restore_delay_ms / 1000.0)
            backend.restore_and_focus(previous_foreground)


def scroll_window(
    hwnd: int,
    x: int,
    y: int,
    amount: int,
    *,
    delivery_mode: str = "message",
    focus_delay_ms: int = 200,
    restore_delay_ms: int = 100,
    backend: WindowsInputBackend | None = None,
) -> None:
    """Scroll at a window-relative point without or with physical focus."""
    if backend is None:
        ensure_windows_input_access(hwnd)
        backend = NativeWindowsInputBackend()
    delivery_mode = str(delivery_mode).lower()
    focus_delay_ms = max(0, int(focus_delay_ms))
    restore_delay_ms = max(0, int(restore_delay_ms))
    origin_x, origin_y = backend.window_origin(hwnd)
    screen_x = origin_x + int(x)
    screen_y = origin_y + int(y)
    delta = int(amount) * 120

    if delivery_mode == "message":
        target, _client_x, _client_y = backend.resolve_message_target(
            hwnd, screen_x, screen_y
        )
        wparam = (delta & 0xFFFF) << 16
        if not backend.post_message(
            target,
            WM_MOUSEWHEEL,
            wparam,
            _pack_point(screen_x, screen_y),
        ):
            raise RuntimeError("Windows background scroll message was rejected")
        return
    if delivery_mode != "input":
        raise RuntimeError(f"Unsupported Windows scroll delivery mode: {delivery_mode}")

    previous_foreground = backend.get_foreground_window()
    previous_cursor = backend.get_cursor_pos()
    focus_changed = previous_foreground != hwnd
    try:
        backend.restore_and_focus(hwnd)
        if not _wait_for_foreground(backend, hwnd, timeout_ms=500):
            raise RuntimeError(
                "Windows refused to focus the target for physical scrolling. "
                "Use message delivery when the target supports background scrolling."
            )
        if focus_changed and focus_delay_ms > 0:
            time.sleep(focus_delay_ms / 1000.0)
        backend.set_cursor_pos(screen_x, screen_y)
        time.sleep(0.03)
        backend.send_mouse_wheel(delta)
        if restore_delay_ms > 0:
            time.sleep(restore_delay_ms / 1000.0)
    finally:
        if backend.get_cursor_pos() == (screen_x, screen_y):
            backend.set_cursor_pos(*previous_cursor)
        if (
            focus_changed
            and previous_foreground
            and backend.get_foreground_window() == hwnd
        ):
            backend.restore_and_focus(previous_foreground)


def focus_window(
    hwnd: int,
    *,
    backend: WindowsInputBackend | None = None,
) -> None:
    """Restore and robustly focus one window, failing when Windows refuses."""
    if backend is None:
        ensure_windows_input_access(hwnd)
        backend = NativeWindowsInputBackend()
    backend.restore_and_focus(hwnd)
    if not _wait_for_foreground(backend, hwnd, timeout_ms=500):
        raise RuntimeError("Windows refused to focus the selected application")


def _post_character(
    backend: WindowsInputBackend,
    target: int,
    code_unit: int,
) -> None:
    if not backend.post_message(target, WM_CHAR, code_unit, 1):
        raise RuntimeError("Windows background text message was rejected")


def _emit_text(
    text: str,
    min_delay_ms: int,
    max_delay_ms: int,
    emit_unit: Callable[[int], None],
) -> None:
    for character in text:
        encoded = character.encode("utf-16-le", errors="surrogatepass")
        for index in range(0, len(encoded), 2):
            emit_unit(int.from_bytes(encoded[index:index + 2], "little"))
        delay_ms = (
            random.randint(min_delay_ms, max_delay_ms)
            if max_delay_ms > 0
            else 0
        )
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)


def _message_keys(
    backend: WindowsInputBackend,
    hwnd: int,
    virtual_keys: list[int],
    sequence_mode: str,
    pause_ms: int,
    *,
    include_characters: bool = False,
) -> None:
    target = backend.resolve_keyboard_target(hwnd)
    alt_is_down = False
    shift_is_down = False

    def post(virtual_key: int, *, key_up: bool) -> None:
        nonlocal alt_is_down, shift_is_down
        is_alt = virtual_key == VK_MENU
        is_shift = virtual_key == VK_SHIFT
        system_message = alt_is_down or is_alt
        message = (
            WM_SYSKEYUP if key_up else WM_SYSKEYDOWN
        ) if system_message else (WM_KEYUP if key_up else WM_KEYDOWN)
        lparam = backend.key_message_lparam(virtual_key, key_up=key_up)
        if alt_is_down:
            lparam |= 1 << 29
        if not backend.post_message(target, message, virtual_key, lparam):
            raise RuntimeError(
                f"Windows background key message 0x{message:04X} was rejected"
            )
        if is_shift:
            shift_is_down = not key_up
        if include_characters and not key_up and not alt_is_down:
            character = _virtual_key_character(
                virtual_key,
                shift=shift_is_down,
            )
            if character is not None:
                _post_character(backend, target, ord(character))
        if is_alt:
            alt_is_down = not key_up

    _emit_key_sequence(virtual_keys, sequence_mode, pause_ms, post)


def _message_key_text(
    backend: WindowsInputBackend,
    hwnd: int,
    virtual_keys: list[int],
    pause_ms: int,
) -> None:
    target = backend.resolve_keyboard_target(hwnd)
    emitted = False
    shift_is_down = VK_SHIFT in virtual_keys
    for index, virtual_key in enumerate(virtual_keys):
        character = _virtual_key_character(virtual_key, shift=shift_is_down)
        if character is not None:
            _post_character(backend, target, ord(character))
            emitted = True
        if pause_ms > 0 and index < len(virtual_keys) - 1:
            time.sleep(pause_ms / 1000.0)
    if not emitted:
        raise RuntimeError(
            "Background text delivery cannot represent the selected key(s); "
            "use background key delivery instead"
        )


def _virtual_key_character(virtual_key: int, *, shift: bool = False) -> str | None:
    if ord("A") <= virtual_key <= ord("Z"):
        character = chr(virtual_key)
        return character if shift else character.lower()
    if ord("0") <= virtual_key <= ord("9"):
        return chr(virtual_key)
    return {
        0x08: "\b",
        0x09: "\t",
        0x0D: "\r",
        0x20: " ",
    }.get(virtual_key)


_ASCII_PUNCTUATION_KEYS: dict[str, tuple[int, bool]] = {
    "-": (0xBD, False), "_": (0xBD, True),
    "=": (0xBB, False), "+": (0xBB, True),
    "[": (0xDB, False), "{": (0xDB, True),
    "]": (0xDD, False), "}": (0xDD, True),
    "\\": (0xDC, False), "|": (0xDC, True),
    ";": (0xBA, False), ":": (0xBA, True),
    "'": (0xDE, False), '"': (0xDE, True),
    ",": (0xBC, False), "<": (0xBC, True),
    ".": (0xBE, False), ">": (0xBE, True),
    "/": (0xBF, False), "?": (0xBF, True),
    "`": (0xC0, False), "~": (0xC0, True),
    "!": (ord("1"), True), "@": (ord("2"), True),
    "#": (ord("3"), True), "$": (ord("4"), True),
    "%": (ord("5"), True), "^": (ord("6"), True),
    "&": (ord("7"), True), "*": (ord("8"), True),
    "(": (ord("9"), True), ")": (ord("0"), True),
}


def _character_key(character: str) -> tuple[int, bool] | None:
    if "a" <= character <= "z":
        return ord(character.upper()), False
    if "A" <= character <= "Z":
        return ord(character), True
    if "0" <= character <= "9":
        return ord(character), False
    if character == " ":
        return 0x20, False
    if character in {"\r", "\n"}:
        return 0x0D, False
    if character == "\t":
        return 0x09, False
    if character == "\b":
        return 0x08, False
    return _ASCII_PUNCTUATION_KEYS.get(character)


def _message_type_text(
    backend: WindowsInputBackend,
    hwnd: int,
    text: str,
    min_delay_ms: int,
    max_delay_ms: int,
    *,
    include_characters: bool,
) -> None:
    target = backend.resolve_keyboard_target(hwnd)
    for character in text:
        mapped = _character_key(character)
        if mapped is None:
            if not include_characters:
                raise RuntimeError(
                    f"Background key delivery cannot represent {character!r}; "
                    "use background text or combined delivery instead"
                )
            _emit_text(character, 0, 0, lambda unit: _post_character(backend, target, unit))
        else:
            virtual_key, needs_shift = mapped
            keys = [VK_SHIFT, virtual_key] if needs_shift else [virtual_key]
            for key in keys:
                if not backend.post_message(
                    target,
                    WM_KEYDOWN,
                    key,
                    backend.key_message_lparam(key, key_up=False),
                ):
                    raise RuntimeError("Windows background key message was rejected")
            if include_characters:
                _emit_text(
                    character,
                    0,
                    0,
                    lambda unit: _post_character(backend, target, unit),
                )
            for key in reversed(keys):
                if not backend.post_message(
                    target,
                    WM_KEYUP,
                    key,
                    backend.key_message_lparam(key, key_up=True),
                ):
                    raise RuntimeError("Windows background key message was rejected")
        delay_ms = (
            random.randint(min_delay_ms, max_delay_ms)
            if max_delay_ms > 0
            else 0
        )
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)


def _activated_message_delivery(
    backend: WindowsInputBackend,
    hwnd: int,
    deliver: Callable[[], None],
) -> None:
    target = backend.resolve_keyboard_target(hwnd)
    setup = (
        (hwnd, WM_ACTIVATEAPP, 1, 0),
        (hwnd, WM_NCACTIVATE, 1, 0),
        (hwnd, WM_ACTIVATE, WA_ACTIVE, 0),
        (target, WM_SETFOCUS, 0, 0),
    )
    cleanup = (
        (target, WM_KILLFOCUS, 0, 0),
        (hwnd, WM_ACTIVATE, WA_INACTIVE, 0),
        (hwnd, WM_NCACTIVATE, 0, 0),
        (hwnd, WM_ACTIVATEAPP, 0, 0),
    )
    for target_hwnd, message, wparam, lparam in setup:
        if not backend.post_message(target_hwnd, message, wparam, lparam):
            raise RuntimeError("Windows background activation message was rejected")
    try:
        deliver()
    finally:
        for target_hwnd, message, wparam, lparam in cleanup:
            backend.post_message(target_hwnd, message, wparam, lparam)


def _physical_keys(
    backend: WindowsInputBackend,
    hwnd: int,
    virtual_keys: list[int],
    sequence_mode: str,
    pause_ms: int,
    focus_delay_ms: int,
    restore_delay_ms: int,
) -> None:
    previous_foreground = backend.get_foreground_window()
    focus_changed = previous_foreground != hwnd
    try:
        backend.restore_and_focus(hwnd)
        if not _wait_for_foreground(backend, hwnd, timeout_ms=500):
            raise RuntimeError(
                "Windows refused to focus the target for physical key input. "
                "Use message delivery when the target supports background keys."
            )
        if focus_changed and focus_delay_ms > 0:
            time.sleep(focus_delay_ms / 1000.0)
        _emit_key_sequence(
            virtual_keys,
            sequence_mode,
            pause_ms,
            backend.key_event,
        )
    finally:
        if (
            focus_changed
            and previous_foreground
            and backend.get_foreground_window() == hwnd
        ):
            if restore_delay_ms > 0:
                time.sleep(restore_delay_ms / 1000.0)
            backend.restore_and_focus(previous_foreground)


def _emit_key_sequence(
    virtual_keys: list[int],
    sequence_mode: str,
    pause_ms: int,
    emit: Callable[..., None],
) -> None:
    if sequence_mode == "simultaneous":
        pressed: list[int] = []
        try:
            for virtual_key in virtual_keys:
                emit(virtual_key, key_up=False)
                pressed.append(virtual_key)
        finally:
            for virtual_key in reversed(pressed):
                emit(virtual_key, key_up=True)
        return

    for index, virtual_key in enumerate(virtual_keys):
        pressed = False
        try:
            emit(virtual_key, key_up=False)
            pressed = True
        finally:
            if pressed:
                emit(virtual_key, key_up=True)
        if pause_ms > 0 and index < len(virtual_keys) - 1:
            time.sleep(pause_ms / 1000.0)


def _message_click(
    backend: WindowsInputBackend,
    hwnd: int,
    screen_x: int,
    screen_y: int,
    messages: MouseButtonMessages,
    click_type: str,
    hold_duration_ms: int,
) -> None:
    target, client_x, client_y = backend.resolve_message_target(
        hwnd, screen_x, screen_y
    )
    lparam = _pack_point(client_x, client_y)

    sequence = [
        (WM_MOUSEMOVE, 0),
        (messages.down, messages.key_state),
        (messages.up, 0),
    ]
    if click_type == "double" and hold_duration_ms <= 0:
        sequence.extend(
            [
                (messages.double, messages.key_state),
                (messages.up, 0),
            ]
        )

    press_posted = False
    try:
        for index, (message, key_state) in enumerate(sequence):
            if not backend.post_message(target, message, key_state, lparam):
                raise RuntimeError(
                    f"Windows background click message 0x{message:04X} was rejected"
                )
            if message in {messages.down, messages.double}:
                press_posted = True
            if index == 1 and hold_duration_ms > 0:
                time.sleep(hold_duration_ms / 1000.0)
            elif message in {messages.up, messages.double} and click_type == "double":
                time.sleep(0.05)
    finally:
        if press_posted:
            # Some controls capture the mouse while handling a synthetic button
            # down but do not release it reliably for a posted button-up. That
            # capture can divert the user's real clicks even though message mode
            # never moved the cursor or focused the target. DefWindowProc handles
            # WM_CANCELMODE by releasing capture. Cleanup is best-effort because
            # the click itself may legitimately close or destroy the target.
            backend.post_message(target, WM_CANCELMODE, 0, 0)


def _message_drag(
    backend: WindowsInputBackend,
    hwnd: int,
    start_screen_x: int,
    start_screen_y: int,
    end_screen_x: int,
    end_screen_y: int,
    messages: MouseButtonMessages,
    drag_duration_ms: int,
    steps: int,
) -> None:
    target, start_client_x, start_client_y = backend.resolve_message_target(
        hwnd, start_screen_x, start_screen_y
    )
    delta_x = end_screen_x - start_screen_x
    delta_y = end_screen_y - start_screen_y
    sleep_seconds = (drag_duration_ms / 1000.0) / steps if drag_duration_ms > 0 else 0
    last_lparam = _pack_point(start_client_x, start_client_y)

    sequence = [
        (WM_MOUSEMOVE, 0, last_lparam),
        (messages.down, messages.key_state, last_lparam),
    ]
    for index in range(1, steps + 1):
        client_x = start_client_x + int(delta_x * index / steps)
        client_y = start_client_y + int(delta_y * index / steps)
        last_lparam = _pack_point(client_x, client_y)
        sequence.append((WM_MOUSEMOVE, messages.key_state, last_lparam))
    sequence.append((messages.up, 0, last_lparam))

    press_posted = False
    try:
        for message, key_state, lparam in sequence:
            if not backend.post_message(target, message, key_state, lparam):
                raise RuntimeError(
                    f"Windows background drag message 0x{message:04X} was rejected"
                )
            if message == messages.down:
                press_posted = True
            if message == WM_MOUSEMOVE and key_state == messages.key_state and sleep_seconds:
                time.sleep(sleep_seconds)
    finally:
        if press_posted:
            backend.post_message(target, WM_CANCELMODE, 0, 0)


def _physical_click(
    backend: WindowsInputBackend,
    hwnd: int,
    screen_x: int,
    screen_y: int,
    messages: MouseButtonMessages,
    click_type: str,
    hold_duration_ms: int,
    *,
    use_send_input: bool,
    focus_delay_ms: int,
    click_duration_ms: int,
    restore_delay_ms: int,
) -> None:
    previous_foreground = backend.get_foreground_window()
    previous_cursor = backend.get_cursor_pos()
    emit = backend.send_mouse_input if use_send_input else backend.mouse_event
    focus_changed = previous_foreground != hwnd

    try:
        backend.restore_and_focus(hwnd)
        if not _wait_for_foreground(backend, hwnd, timeout_ms=500):
            raise RuntimeError(
                "Windows refused to focus the target for physical input. "
                "Use message mode for background clicks or add an explicit "
                "Win Focus step before retrying."
            )
        if focus_changed and focus_delay_ms > 0:
            time.sleep(focus_delay_ms / 1000.0)
        backend.set_cursor_pos(screen_x, screen_y)
        time.sleep(0.03)
        emit(messages.physical_down)
        if hold_duration_ms > 0:
            time.sleep(hold_duration_ms / 1000.0)
        elif click_duration_ms > 0:
            time.sleep(click_duration_ms / 1000.0)
        emit(messages.physical_up)

        if click_type == "double" and hold_duration_ms <= 0:
            time.sleep(0.05)
            emit(messages.physical_down)
            if click_duration_ms > 0:
                time.sleep(click_duration_ms / 1000.0)
            emit(messages.physical_up)

        # Games often process input on a later frame. Keep both focus and the
        # cursor stable briefly after release, even when already foreground.
        if restore_delay_ms > 0:
            time.sleep(restore_delay_ms / 1000.0)
    finally:
        if backend.get_cursor_pos() == (screen_x, screen_y):
            backend.set_cursor_pos(*previous_cursor)
        if (
            previous_foreground
            and previous_foreground != hwnd
            and backend.get_foreground_window() == hwnd
        ):
            backend.restore_and_focus(previous_foreground)


def _physical_drag(
    backend: WindowsInputBackend,
    hwnd: int,
    start_screen_x: int,
    start_screen_y: int,
    end_screen_x: int,
    end_screen_y: int,
    messages: MouseButtonMessages,
    drag_duration_ms: int,
    steps: int,
    *,
    use_send_input: bool,
    focus_delay_ms: int,
    restore_delay_ms: int,
) -> None:
    previous_foreground = backend.get_foreground_window()
    previous_cursor = backend.get_cursor_pos()
    emit = backend.send_mouse_input if use_send_input else backend.mouse_event
    focus_changed = previous_foreground != hwnd
    sleep_seconds = (drag_duration_ms / 1000.0) / steps if drag_duration_ms > 0 else 0

    try:
        backend.restore_and_focus(hwnd)
        if not _wait_for_foreground(backend, hwnd, timeout_ms=500):
            raise RuntimeError(
                "Windows refused to focus the target for physical input. "
                "Use message mode for background drags or add an explicit "
                "Win Focus step before retrying."
            )
        if focus_changed and focus_delay_ms > 0:
            time.sleep(focus_delay_ms / 1000.0)

        backend.set_cursor_pos(start_screen_x, start_screen_y)
        time.sleep(0.03)
        emit(messages.physical_down)
        for index in range(1, steps + 1):
            x = start_screen_x + int((end_screen_x - start_screen_x) * index / steps)
            y = start_screen_y + int((end_screen_y - start_screen_y) * index / steps)
            backend.set_cursor_pos(x, y)
            emit(MOUSEEVENTF_MOVE)
            if sleep_seconds:
                time.sleep(sleep_seconds)
        emit(messages.physical_up)

        if restore_delay_ms > 0:
            time.sleep(restore_delay_ms / 1000.0)
    finally:
        if backend.get_cursor_pos() == (end_screen_x, end_screen_y):
            backend.set_cursor_pos(*previous_cursor)
        if (
            previous_foreground
            and previous_foreground != hwnd
            and backend.get_foreground_window() == hwnd
        ):
            backend.restore_and_focus(previous_foreground)


def _wait_for_foreground(
    backend: WindowsInputBackend,
    hwnd: int,
    *,
    timeout_ms: int,
) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if backend.get_foreground_window() == hwnd:
            return True
        time.sleep(0.01)
    return backend.get_foreground_window() == hwnd


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.wintypes.WPARAM),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.wintypes.WPARAM),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ctypes.wintypes.DWORD), ("value", _INPUT_UNION)]


class NativeWindowsInputBackend:
    """Thin testable wrapper around the user32 calls used by click delivery."""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        user32 = self._user32
        user32.GetWindowRect.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.RECT),
        ]
        user32.GetWindowRect.restype = ctypes.wintypes.BOOL
        user32.ScreenToClient.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.POINT),
        ]
        user32.ScreenToClient.restype = ctypes.wintypes.BOOL
        user32.ChildWindowFromPointEx.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.POINT,
            ctypes.wintypes.UINT,
        ]
        user32.ChildWindowFromPointEx.restype = ctypes.wintypes.HWND
        user32.PostMessageW.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = ctypes.wintypes.BOOL
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
        user32.GetGUIThreadInfo.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_GUITHREADINFO),
        ]
        user32.GetGUIThreadInfo.restype = ctypes.wintypes.BOOL
        user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
        user32.GetAncestor.restype = ctypes.wintypes.HWND
        user32.MapVirtualKeyW.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        user32.MapVirtualKeyW.restype = ctypes.wintypes.UINT
        user32.AttachThreadInput.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
        ]
        user32.AttachThreadInput.restype = ctypes.wintypes.BOOL
        user32.BringWindowToTop.argtypes = [ctypes.wintypes.HWND]
        user32.BringWindowToTop.restype = ctypes.wintypes.BOOL
        user32.SetActiveWindow.argtypes = [ctypes.wintypes.HWND]
        user32.SetActiveWindow.restype = ctypes.wintypes.HWND
        user32.SetFocus.argtypes = [ctypes.wintypes.HWND]
        user32.SetFocus.restype = ctypes.wintypes.HWND
        user32.keybd_event.argtypes = [
            ctypes.wintypes.BYTE,
            ctypes.wintypes.BYTE,
            ctypes.wintypes.DWORD,
            ctypes.c_size_t,
        ]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(ctypes.wintypes.POINT)]
        user32.GetCursorPos.restype = ctypes.wintypes.BOOL
        user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
        user32.IsIconic.restype = ctypes.wintypes.BOOL
        user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SendInput.argtypes = [
            ctypes.wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        ]
        user32.SendInput.restype = ctypes.wintypes.UINT
        self._kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD

    def window_origin(self, hwnd: int) -> tuple[int, int]:
        rect = ctypes.wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError("Could not read target window position")
        return int(rect.left), int(rect.top)

    def resolve_message_target(
        self, hwnd: int, screen_x: int, screen_y: int
    ) -> tuple[int, int, int]:
        target = hwnd
        flags = CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT

        while True:
            point = ctypes.wintypes.POINT(screen_x, screen_y)
            self._user32.ScreenToClient(target, ctypes.byref(point))
            child = int(self._user32.ChildWindowFromPointEx(target, point, flags) or 0)
            if not child or child == target:
                return target, int(point.x), int(point.y)
            target = child

    def post_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        return bool(self._user32.PostMessageW(hwnd, message, wparam, lparam))

    def resolve_keyboard_target(self, hwnd: int) -> int:
        thread_id = int(self._user32.GetWindowThreadProcessId(hwnd, None) or 0)
        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        if thread_id and self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            focused = int(info.hwndFocus or 0)
            if focused and int(self._user32.GetAncestor(focused, GA_ROOT) or 0) == hwnd:
                return focused
        return hwnd

    def key_message_lparam(self, virtual_key: int, *, key_up: bool) -> int:
        scan_code = int(self._user32.MapVirtualKeyW(int(virtual_key), 0) or 0)
        value = 1 | (scan_code << 16)
        if virtual_key in _EXTENDED_VIRTUAL_KEYS:
            value |= 1 << 24
        if key_up:
            value |= 1 << 30 | 1 << 31
        return value

    def key_event(self, virtual_key: int, *, key_up: bool) -> None:
        self._user32.keybd_event(
            int(virtual_key),
            0,
            KEYEVENTF_KEYUP if key_up else 0,
            0,
        )

    def send_unicode_unit(self, code_unit: int, *, key_up: bool) -> None:
        value = _INPUT_UNION()
        value.ki = _KEYBDINPUT(
            0,
            int(code_unit),
            KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0),
            0,
            0,
        )
        event = _INPUT(1, value)
        if self._user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise RuntimeError("SendInput failed to deliver Windows text input")

    def send_mouse_wheel(self, delta: int) -> None:
        value = _INPUT_UNION()
        value.mi = _MOUSEINPUT(0, 0, int(delta), MOUSEEVENTF_WHEEL, 0, 0)
        event = _INPUT(0, value)
        if self._user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise RuntimeError("SendInput failed to deliver Windows scroll input")

    def get_foreground_window(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def get_cursor_pos(self) -> tuple[int, int]:
        point = ctypes.wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return 0, 0
        return int(point.x), int(point.y)

    def restore_and_focus(self, hwnd: int) -> None:
        if self._user32.IsIconic(hwnd):
            self._user32.ShowWindow(hwnd, SW_RESTORE)
        _activate_foreground_window(self._user32, self._kernel32, hwnd)

    def set_cursor_pos(self, x: int, y: int) -> None:
        self._user32.SetCursorPos(int(x), int(y))

    def mouse_event(self, flags: int) -> None:
        self._user32.mouse_event(flags, 0, 0, 0, 0)

    def send_mouse_input(self, flags: int) -> None:
        value = _INPUT_UNION()
        value.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0)
        event = _INPUT(0, value)
        if self._user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise RuntimeError("SendInput failed to deliver Windows mouse input")
