"""Device session management — supports Browser, Android, and Windows devices."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from automadin.engine.adb_engine import AdbDeviceEngine
from automadin.engine.browser_engine import BrowserDeviceEngine, BrowserSessionData
from automadin.engine.browser_session import BrowserSessionHandle, BrowserSessionPort
from automadin.engine.device_controller import (
    DEVICE_CONTROLLER_SESSION_KEY,
    AndroidAppCapability,
    AndroidDeviceController,
    ScreenshotCapability,
    get_device_capability,
    put_device_controller,
)
from automadin.engine.windows_engine import WindowsDeviceEngine
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    Device,
    DeviceSession,
    DeviceType,
    WindowsDevice,
)
from automadin.services.browser_profile_metadata import BrowserProfileTarget
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    ProfileUse,
    browser_profile_leases,
)
from automadin.models.managed_emulator import ManagedEmulatorTargetResolution
from automadin.services.ocr import ScreenTextOcrEngine

logger = logging.getLogger(__name__)


class DeviceDiscoveryService:
    """Discovers available devices across all device types."""

    def __init__(
        self,
        browser_engine: BrowserDeviceEngine | None = None,
        adb_engine: AdbDeviceEngine | None = None,
        windows_engine: WindowsDeviceEngine | None = None,
    ) -> None:
        self._browser_engine = browser_engine or BrowserDeviceEngine()
        self._adb_engine = adb_engine or AdbDeviceEngine()
        self._windows_engine = windows_engine or WindowsDeviceEngine()

    def set_adb_engine(self, adb_engine: AdbDeviceEngine) -> None:
        self._adb_engine = adb_engine

    def discover_managed_emulator_target(
        self,
        provider_id: str,
        instance_id: str,
    ) -> ManagedEmulatorTargetResolution | None:
        """Resolve one managed provider instance without broadening its identity."""
        return self._adb_engine.discover_managed_emulator_target(
            provider_id,
            instance_id,
        )

    def list_devices(
        self,
        device_type: DeviceType,
        profiles: list[BrowserProfileTarget] | None = None,
    ) -> list[Device]:
        """List available devices of the given type."""
        if device_type == DeviceType.BROWSER:
            return self._browser_engine.generate_devices(profiles)  # type: ignore
        elif device_type == DeviceType.ANDROID:
            return self._adb_engine.list_devices()  # type: ignore
        elif device_type == DeviceType.WINDOWS:
            return self._windows_engine.list_devices()  # type: ignore
        return []


class DeviceSessionService:
    """Manages active device sessions across all device types."""

    def __init__(
        self,
        browser_engine: BrowserDeviceEngine | None = None,
        adb_engine: AdbDeviceEngine | None = None,
        windows_engine: WindowsDeviceEngine | None = None,
        profile_lease_registry: BrowserProfileLeaseRegistry | None = None,
        screen_text_ocr_engine: ScreenTextOcrEngine | None = None,
    ) -> None:
        self._sessions: dict[str, DeviceSession] = {}
        self._browser_engine = browser_engine or BrowserDeviceEngine()
        self._adb_engine = adb_engine or AdbDeviceEngine()
        self._windows_engine = windows_engine or WindowsDeviceEngine()
        self._profile_lease_registry = (
            profile_lease_registry or browser_profile_leases
        )
        self._screen_text_ocr_engine = screen_text_ocr_engine
        self.discovery = DeviceDiscoveryService(
            self._browser_engine, self._adb_engine, self._windows_engine
        )

    def set_adb_engine(self, adb_engine: AdbDeviceEngine) -> None:
        """Use a new ADB engine for future discovery and Android sessions."""
        self._adb_engine = adb_engine
        self.discovery.set_adb_engine(adb_engine)

    @property
    def browser_profile_lease_registry(self) -> BrowserProfileLeaseRegistry:
        """Return the registry shared by interactive and background sessions."""
        return self._profile_lease_registry

    def create_session(self, device: Device) -> DeviceSession:
        """Connect to a device and create a new session.

        Raises ValueError if a session for this device already exists.
        """
        existing = self.get_session_by_device(device.id)
        if existing is not None:
            raise ValueError(f"Session already exists for device '{device.id}'")

        data: dict[str, Any] = {}

        if isinstance(device, BrowserDevice):
            profile_lease = None
            if device.config.profile_id:
                profile_lease = self._profile_lease_registry.acquire_owner(
                    device.config.profile_id,
                    ProfileUse(
                        kind="interactive-browser",
                        label=device.name,
                        operation_id=str(uuid4()),
                    ),
                )
            data["browser_data"] = BrowserSessionHandle(
                device,
                self._browser_engine,
                profile_lease=profile_lease,
            )
        elif isinstance(device, AndroidDevice):
            adb_data = self._adb_engine.connect(device)
            data["adb_data"] = adb_data
            data[DEVICE_CONTROLLER_SESSION_KEY] = AndroidDeviceController(
                self._adb_engine,
                adb_data,
            )
        elif isinstance(device, WindowsDevice):
            win_data = self._windows_engine.connect(device)
            data["windows_data"] = win_data
            device = WindowsDevice(
                hwnd=int(win_data["hwnd"]),
                title=str(win_data.get("title", device.title)),
                executable_path=str(
                    win_data.get("executable_path", device.executable_path)
                ),
                auto_start=bool(win_data.get("auto_start", device.auto_start)),
            )
        else:
            raise TypeError(f"Unknown device type: {type(device)}")

        if self._screen_text_ocr_engine is not None:
            data["screen_text_ocr_engine"] = self._screen_text_ocr_engine

        session = DeviceSession(device=device, data=data)
        self._sessions[session.session_id] = session
        return session

    def ensure_android_session(self, device: AndroidDevice) -> DeviceSession:
        """Reconnect and verify an Android target before a flow can execute.

        Existing sessions are deliberately not trusted: their lightweight ADB
        data can outlive a device or emulator connection.  The cached session is
        updated only after the exact saved target passes a fresh readiness probe.
        """
        adb_data = self._adb_engine.connect(device)
        existing = self.get_session_by_device(device.id)
        if existing is not None:
            existing.device = device
            existing.put_data("adb_data", adb_data)
            put_device_controller(
                existing,
                AndroidDeviceController(self._adb_engine, adb_data),
            )
            if self._screen_text_ocr_engine is not None:
                existing.put_data(
                    "screen_text_ocr_engine",
                    self._screen_text_ocr_engine,
                )
            return existing

        data: dict[str, Any] = {
            "adb_data": adb_data,
            DEVICE_CONTROLLER_SESSION_KEY: AndroidDeviceController(
                self._adb_engine,
                adb_data,
            ),
        }
        if self._screen_text_ocr_engine is not None:
            data["screen_text_ocr_engine"] = self._screen_text_ocr_engine
        session = DeviceSession(device=device, data=data)
        self._sessions[session.session_id] = session
        return session

    def close_session(self, session_id: str) -> None:
        """Close and remove a session by its ID.

        A failed close retains a still-live session so callers can retry and
        Automadin does not lose the only handle to a browser/profile lock.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return

        device = session.device
        browser_data: BrowserSessionData | BrowserSessionHandle | None = None

        try:
            if isinstance(device, BrowserDevice):
                browser_data = (
                    session.get_data("browser_data")
                )
                if isinstance(browser_data, BrowserSessionHandle):
                    browser_data.close()
                elif browser_data is not None:
                    self._browser_engine.disconnect(browser_data)
            elif isinstance(device, AndroidDevice):
                adb_data = session.get_data("adb_data")
                if adb_data is not None:
                    self._adb_engine.disconnect(adb_data)
            elif isinstance(device, WindowsDevice):
                win_data = session.get_data("windows_data")
                if win_data is not None:
                    self._windows_engine.disconnect(win_data)
        except Exception as exc:
            logger.exception("Error disconnecting device")
            # If teardown finished but reported an error, there is nothing left
            # to retry.  Remove that closed handle, while still surfacing the
            # failure to the caller.
            if isinstance(browser_data, BrowserSessionHandle) and browser_data.is_closed:
                self._sessions.pop(session_id, None)
            raise RuntimeError(
                f"Could not close session for '{getattr(device, 'name', session_id)}': "
                f"{exc}"
            ) from exc

        self._sessions.pop(session_id, None)

    def take_screenshot(self, session: DeviceSession) -> bytes:
        """Capture a connected target without exposing engine-owned resources."""
        device = session.device
        if isinstance(device, BrowserDevice):
            browser_data: BrowserSessionPort | None = session.get_data("browser_data")
            if browser_data is None:
                raise RuntimeError("No browser session data")
            return browser_data.screenshot()
        if isinstance(device, AndroidDevice):
            capability = get_device_capability(session, ScreenshotCapability)
            if capability is not None:
                return capability.take_screenshot()
            # Preserve the session-service engine contract for injected engines
            # whose opaque test or adapter session data predates controllers.
            adb_data = session.get_data("adb_data")
            if adb_data is None:
                raise RuntimeError("No ADB session data")
            return self._adb_engine.take_screenshot(adb_data)
        if isinstance(device, WindowsDevice):
            windows_data = session.get_data("windows_data")
            if windows_data is None:
                raise RuntimeError("No Windows session data")
            screenshot = self._windows_engine.take_screenshot(windows_data)
            if screenshot is None:
                raise RuntimeError(
                    "Windows screenshot failed (pywin32/Pillow not installed?)"
                )
            return screenshot
        raise TypeError(f"Unknown device type: {type(device)}")

    def list_android_packages(
        self,
        session: DeviceSession,
        *,
        include_system: bool = False,
    ) -> list[str]:
        """List packages through the engine that owns the connected session."""
        if not isinstance(session.device, AndroidDevice):
            raise RuntimeError("Connect an Android device to browse packages")
        capability = get_device_capability(session, AndroidAppCapability)
        if capability is None:
            raise RuntimeError("Android application control is not connected")
        return capability.list_installed_packages(include_system=include_system)

    def get_session(self, session_id: str) -> DeviceSession | None:
        return self._sessions.get(session_id)

    def get_session_by_device(self, device_id: str) -> DeviceSession | None:
        for session in self._sessions.values():
            if session.device is not None and session.device.id == device_id:
                return session
        return None

    def list_sessions(self) -> list[DeviceSession]:
        return list(self._sessions.values())

    def close_all(self) -> None:
        """Close all active sessions. Safe to call during shutdown."""
        failures: list[str] = []
        for session_id in list(self._sessions.keys()):
            try:
                self.close_session(session_id)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError(
                "Some device sessions are still open:\n" + "\n".join(failures)
            )
