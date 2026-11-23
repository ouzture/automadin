"""Windows device engine — discovers visible application windows."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from automadin.engine.windows_input import focus_window
from automadin.models.device import WindowsDevice
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS

logger = logging.getLogger(__name__)

# Windows API constants
_GWL_STYLE = -16
_WS_VISIBLE = 0x10000000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class WindowNotFoundError(RuntimeError):
    """The configured Windows application has no matching live window."""


def _is_windows() -> bool:
    return sys.platform == "win32"


class WindowsDeviceEngine:
    """Discovers visible Windows application windows and manages sessions."""

    def list_devices(self) -> list[WindowsDevice]:
        """Enumerate all visible windows with non-empty titles."""
        if not _is_windows():
            logger.info("Not on Windows — skipping window enumeration")
            return []

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        devices: list[WindowsDevice] = []

        def _enum_callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()

            if not title:
                return True

            # Skip very small / system windows
            style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
            if not (style & _WS_VISIBLE):
                return True

            devices.append(
                WindowsDevice(
                    hwnd=hwnd,
                    title=title,
                    executable_path=self._window_executable_path(hwnd),
                )
            )
            return True

        _WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )
        user32.EnumWindows(_WNDENUMPROC(_enum_callback), 0)

        return devices

    @staticmethod
    def _window_executable_path(hwnd: int) -> str:
        """Best-effort executable identity for one top-level window."""
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPWSTR,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        process_id = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if not process_id.value:
            return ""
        process = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id.value,
        )
        if not process:
            return ""
        try:
            capacity = 32_768
            size = ctypes.wintypes.DWORD(capacity)
            buffer = ctypes.create_unicode_buffer(capacity)
            if not kernel32.QueryFullProcessImageNameW(
                process,
                0,
                buffer,
                ctypes.byref(size),
            ):
                return ""
            return buffer.value.strip()
        finally:
            kernel32.CloseHandle(process)

    def connect(self, device: WindowsDevice) -> dict[str, object]:
        """Validate the target and return session data without stealing focus."""
        if not _is_windows():
            raise RuntimeError("Windows device engine requires Windows OS")

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        try:
            resolved = self._resolve_device(device)
        except WindowNotFoundError:
            if not device.executable_path or not device.auto_start:
                raise
            resolved = self._launch_and_wait(
                device.executable_path,
                (),
                device.title,
                timeout=30.0,
            )
        hwnd = resolved.hwnd

        # Verify the window still exists
        if not user32.IsWindow(hwnd):
            raise RuntimeError(f"Window handle {hwnd} is no longer valid")

        return {
            "hwnd": hwnd,
            "title": resolved.title,
            "executable_path": (
                resolved.executable_path or device.executable_path
            ),
            "auto_start": device.auto_start,
        }

    def resolve_session_data(
        self, session_data: dict[str, object]
    ) -> dict[str, object]:
        """Refresh a Windows session target when its HWND is no longer valid.

        If the stored HWND is dead and the session includes a title, re-discover
        the window by exact title and mutate ``session_data`` in place.
        """
        if not _is_windows():
            raise RuntimeError("Windows device engine requires Windows OS")

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = int(session_data.get("hwnd", 0) or 0)
        title = str(session_data.get("title", "") or "")

        if hwnd and user32.IsWindow(hwnd):
            return session_data

        if not title:
            raise RuntimeError("Window no longer exists")

        executable = str(session_data.get("executable_path", "") or "")
        if executable and bool(session_data.get("auto_start", False)):
            return self.ensure_application(session_data)

        resolved = self._resolve_device(
            WindowsDevice(hwnd=hwnd, title=title)
        )
        self._update_session_target(session_data, resolved)
        return session_data

    def _resolve_device(self, device: WindowsDevice) -> WindowsDevice:
        """Resolve a launcher-saved Windows target to a live window.

        When a title is available, prefer an exact-title lookup so a restarted
        app can still be found even if its HWND changed.
        """
        devices = self.list_devices()
        executable = self._normalized_executable(device.executable_path)
        if executable:
            matches = [
                candidate
                for candidate in devices
                if self._normalized_executable(candidate.executable_path) == executable
            ]
            if len(matches) > 1 and device.title:
                titled = [
                    candidate for candidate in matches
                    if candidate.title == device.title
                ]
                if titled:
                    matches = titled
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RuntimeError(
                    f"Multiple windows found for '{device.executable_path}'. "
                    "Close duplicate windows or use a unique window title."
                )

        if device.title:
            matches = [d for d in devices if d.title == device.title]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RuntimeError(
                    f"Multiple windows found with title '{device.title}'. "
                    "Please make the title unique and try again."
                )

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        if device.hwnd and user32.IsWindow(device.hwnd):
            return device

        if device.title:
            raise WindowNotFoundError(
                f"No visible window found with title '{device.title}'."
            )
        raise WindowNotFoundError(
            f"Window handle {device.hwnd} is no longer valid"
        )

    @staticmethod
    def _normalized_executable(value: object) -> str:
        path = str(value or "").strip()
        if not path:
            return ""
        return os.path.normcase(os.path.normpath(path)).casefold()

    @staticmethod
    def _update_session_target(
        session_data: dict[str, object],
        device: WindowsDevice,
    ) -> None:
        session_data["hwnd"] = device.hwnd
        session_data["title"] = device.title
        if device.executable_path:
            session_data["executable_path"] = device.executable_path

    @staticmethod
    def _validate_launch_arguments(arguments: Sequence[str]) -> list[str]:
        normalized: list[str] = []
        for argument in arguments:
            if not isinstance(argument, str):
                raise ValueError("Windows application arguments must be text")
            if "\x00" in argument:
                raise ValueError("Windows application arguments cannot contain NUL")
            normalized.append(argument)
        return normalized

    def _launch_and_wait(
        self,
        executable_path: str,
        arguments: Sequence[str],
        window_title: str,
        *,
        timeout: float,
    ) -> WindowsDevice:
        executable = Path(executable_path).expanduser()
        if not executable.is_file():
            raise FileNotFoundError(
                f"Windows application does not exist: {executable}"
            )
        executable = executable.resolve()
        command = [str(executable), *self._validate_launch_arguments(arguments)]
        subprocess.Popen(
            command,
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )

        target = WindowsDevice(
            hwnd=0,
            title=window_title,
            executable_path=str(executable),
        )
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            try:
                return self._resolve_device(target)
            except WindowNotFoundError:
                time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))
        raise TimeoutError(
            f"Windows application did not open a matching window within "
            f"{float(timeout):g} seconds"
        )

    def ensure_application(
        self,
        session_data: dict[str, object],
        *,
        executable_path: str = "",
        arguments: Sequence[str] = (),
        window_title: str = "",
        timeout: float = 30.0,
    ) -> bool:
        """Ensure the configured app has a live window and refresh its HWND."""
        executable = executable_path.strip() or str(
            session_data.get("executable_path", "") or ""
        ).strip()
        title = window_title.strip() or str(
            session_data.get("title", "") or ""
        ).strip()
        target = WindowsDevice(
            hwnd=int(session_data.get("hwnd", 0) or 0),
            title=title,
            executable_path=executable,
        )
        try:
            resolved = self._resolve_device(target)
            launched = False
        except WindowNotFoundError:
            if not executable:
                raise RuntimeError(
                    "The window is closed and no executable path is configured"
                )
            resolved = self._launch_and_wait(
                executable,
                arguments,
                title,
                timeout=timeout,
            )
            launched = True
        self._update_session_target(session_data, resolved)
        return launched

    def restore_and_focus(
        self,
        session_data: dict[str, object],
    ) -> dict[str, object]:
        """Restore a minimized app, bring it forward, and return refreshed data."""
        data = self.resolve_session_data(session_data)
        hwnd = int(data["hwnd"])
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        if not user32.IsWindow(hwnd):
            raise WindowNotFoundError("Window no longer exists")
        focus_window(hwnd)
        return data

    def disconnect(self, session_data: dict[str, object]) -> None:
        """No-op — we don't own the window."""
        pass

    def take_screenshot(self, session_data: dict[str, object]) -> bytes | None:
        """Capture a screenshot in a bounded helper process."""
        if not _is_windows():
            return None

        session_data = self.resolve_session_data(session_data)
        hwnd = int(session_data["hwnd"])  # type: ignore
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        if not user32.IsWindow(hwnd):
            raise RuntimeError("Window no longer exists")

        # PrintWindow(PW_RENDERFULLCONTENT) refreshes accelerated windows that
        # WM_PRINT and BitBlt can capture as stale surfaces. Isolating it makes
        # the native call killable if a third-party window stops responding.
        from automadin.services.windows_capture import capture_window

        try:
            return capture_window(hwnd)
        except Exception as exc:
            logger.warning("Windows screenshot failed: %s", exc)
            return None
