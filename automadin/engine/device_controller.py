"""Capability-oriented controllers for device execution operations.

Flow handlers depend on these small contracts instead of knowing how a target
transports input.  A session may provide any subset of the capabilities; this
lets future targets, such as physical iOS devices, expose only the operations
their runtime actually supports.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from automadin.engine.adb_engine import AdbDeviceEngine
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


DEVICE_CONTROLLER_SESSION_KEY = "device_controller"


@runtime_checkable
class ScreenshotCapability(Protocol):
    def take_screenshot(self) -> bytes: ...


@runtime_checkable
class SurfaceSizeCapability(Protocol):
    def get_surface_size(self) -> tuple[int, int]: ...


@runtime_checkable
class TapCapability(Protocol):
    def tap(
        self,
        x: int,
        y: int,
        *,
        click_type: str = "single",
        hold_duration_ms: int = 0,
        double_tap_interval_s: float = 0.1,
    ) -> None: ...


@runtime_checkable
class SwipeCapability(Protocol):
    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        *,
        duration_ms: int,
    ) -> None: ...


@runtime_checkable
class TextInputCapability(Protocol):
    def type_text(self, text: str, *, min_delay_ms: int, max_delay_ms: int) -> None: ...

    def clear_text_field(self) -> None: ...


@runtime_checkable
class KeyInputCapability(Protocol):
    def press_keys(
        self,
        keys: Sequence[str],
        *,
        mode: str,
        pause_ms: int,
    ) -> None: ...


@runtime_checkable
class AndroidAppCapability(Protocol):
    def install_apk(
        self,
        apk_path: str,
        *,
        replace_existing: bool,
        grant_permissions: bool,
        timeout: int,
    ) -> str: ...

    def ensure_package_foreground(self, package: str, *, timeout: float) -> bool: ...

    def force_stop_package(self, package: str) -> None: ...

    def restart_package(self, package: str, *, timeout: float) -> None: ...

    def start_activity(
        self,
        package: str,
        activity: str,
        *,
        timeout: float,
    ) -> None: ...

    def open_deep_link(
        self,
        uri: str,
        *,
        package_name: str,
        timeout: float,
    ) -> None: ...

    def list_installed_packages(self, *, include_system: bool = False) -> list[str]: ...


RunCommand = Callable[..., subprocess.CompletedProcess[Any]]
Sleep = Callable[[float], None]


def _adb_gesture_timeout(duration_ms: int) -> int:
    """Allow an ADB input gesture to finish, plus transport overhead."""
    duration_seconds = (max(0, int(duration_ms)) + 999) // 1000
    return max(10, duration_seconds + 5)


@dataclass(slots=True)
class AndroidDeviceController:
    """ADB-backed capabilities bound to one connected Android session."""

    engine: AdbDeviceEngine
    session_data: dict[str, object]
    run_command: RunCommand = field(default_factory=lambda: subprocess.run)
    sleep: Sleep = field(default_factory=lambda: time.sleep)

    @classmethod
    def from_session_data(
        cls,
        session_data: dict[str, object],
    ) -> AndroidDeviceController:
        adb_path = str(session_data.get("adb_path", "adb"))
        return cls(AdbDeviceEngine(adb_path=adb_path), session_data)

    @property
    def adb_path(self) -> str:
        return str(self.session_data.get("adb_path", self.engine.adb_path))

    @property
    def device_id(self) -> str:
        value = self.session_data.get("device_id")
        if value is None:
            raise RuntimeError("Android ADB session has no device ID")
        return str(value)

    def _shell(self, *args: str, timeout: float = 5) -> subprocess.CompletedProcess[Any]:
        return self.run_command(
            [self.adb_path, "-s", self.device_id, "shell", *args],
            timeout=timeout,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )

    def take_screenshot(self) -> bytes:
        return self.engine.take_screenshot(self.session_data)

    def get_surface_size(self) -> tuple[int, int]:
        result = self.run_command(
            [self.adb_path, "-s", self.device_id, "shell", "wm", "size"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        match = re.search(r"(\d+)x(\d+)", result.stdout)
        if match is None:
            raise RuntimeError(f"Could not parse Android screen size: {result.stdout!r}")
        return int(match.group(1)), int(match.group(2))

    def tap(
        self,
        x: int,
        y: int,
        *,
        click_type: str = "single",
        hold_duration_ms: int = 0,
        double_tap_interval_s: float = 0.1,
    ) -> None:
        hold_duration_ms = max(0, int(hold_duration_ms))
        swipe_duration = max(100, hold_duration_ms) if hold_duration_ms > 0 else 100
        shell_timeout = (
            _adb_gesture_timeout(hold_duration_ms) if hold_duration_ms > 0 else 5
        )
        self._shell(
            "input",
            "swipe",
            str(x),
            str(y),
            str(x),
            str(y),
            str(swipe_duration),
            timeout=shell_timeout,
        )
        if hold_duration_ms <= 0 and click_type == "double":
            self.sleep(max(0.0, float(double_tap_interval_s)))
            self._shell(
                "input",
                "swipe",
                str(x),
                str(y),
                str(x),
                str(y),
                "100",
            )

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        *,
        duration_ms: int,
    ) -> None:
        duration_ms = int(duration_ms)
        self._shell(
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
            timeout=_adb_gesture_timeout(duration_ms),
        )

    def type_text(self, text: str, *, min_delay_ms: int, max_delay_ms: int) -> None:
        del min_delay_ms, max_delay_ms  # ADB sends this operation as one command.
        escaped = text.replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
        self._shell("input", "text", escaped, timeout=10)

    def clear_text_field(self) -> None:
        self._shell("input", "keyevent", "KEYCODE_MOVE_HOME")
        self._shell("input", "keyevent", "--longpress", "KEYCODE_DEL")

    def press_keys(
        self,
        keys: Sequence[str],
        *,
        mode: str,
        pause_ms: int,
    ) -> None:
        for key in keys:
            self._shell("input", "keyevent", str(key))
            if pause_ms > 0 and mode == "sequential":
                self.sleep(pause_ms / 1000.0)

    def install_apk(
        self,
        apk_path: str,
        *,
        replace_existing: bool,
        grant_permissions: bool,
        timeout: int,
    ) -> str:
        return self.engine.install_apk(
            self.session_data,
            apk_path,
            replace_existing=replace_existing,
            grant_permissions=grant_permissions,
            timeout=timeout,
        )

    def ensure_package_foreground(self, package: str, *, timeout: float) -> bool:
        return self.engine.ensure_package_foreground(
            self.session_data,
            package,
            timeout=timeout,
        )

    def force_stop_package(self, package: str) -> None:
        self.engine.force_stop_package(self.session_data, package)

    def restart_package(self, package: str, *, timeout: float) -> None:
        self.engine.restart_package(self.session_data, package, timeout=timeout)

    def start_activity(
        self,
        package: str,
        activity: str,
        *,
        timeout: float,
    ) -> None:
        self.engine.start_activity(
            self.session_data,
            package,
            activity,
            timeout=timeout,
        )

    def open_deep_link(
        self,
        uri: str,
        *,
        package_name: str,
        timeout: float,
    ) -> None:
        self.engine.open_deep_link(
            self.session_data,
            uri,
            package_name=package_name,
            timeout=timeout,
        )

    def list_installed_packages(self, *, include_system: bool = False) -> list[str]:
        return self.engine.list_installed_packages(
            self.session_data,
            include_system=include_system,
        )


CapabilityT = TypeVar("CapabilityT")


def put_device_controller(session: DeviceSession, controller: object) -> None:
    session.put_data(DEVICE_CONTROLLER_SESSION_KEY, controller)


def get_device_controller(session: DeviceSession) -> object | None:
    """Return the session controller, adapting legacy Android session data.

    Lazy adaptation keeps existing saved sessions and lightweight test/session
    construction compatible while all newly connected sessions install their
    controller eagerly.
    """
    controller = session.get_data(DEVICE_CONTROLLER_SESSION_KEY)
    if controller is not None:
        return controller
    if not isinstance(session.device, AndroidDevice):
        return None
    adb_data = session.get_data("adb_data")
    if not isinstance(adb_data, dict):
        return None
    controller = AndroidDeviceController.from_session_data(adb_data)
    put_device_controller(session, controller)
    return controller


def get_device_capability(
    session: DeviceSession,
    capability: type[CapabilityT],
) -> CapabilityT | None:
    controller = get_device_controller(session)
    return controller if isinstance(controller, capability) else None
