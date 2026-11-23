from __future__ import annotations

import base64
import ipaddress
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


_BROWSER_PROFILE_SAVED_ID_PREFIX = "browser|2|"
_BROWSER_CDP_SAVED_ID_PREFIX = "browser-cdp|1|"
_BROWSER_COMPANION_SAVED_ID_PREFIX = "browser-companion|1|"


def canonical_browser_cdp_endpoint(value: object) -> str:
    """Return a canonical loopback HTTP CDP endpoint or raise ``ValueError``."""
    from urllib.parse import urlsplit

    if not isinstance(value, str) or not value.strip():
        raise ValueError("Browser CDP endpoint must be non-empty text")
    endpoint = value.strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Browser CDP endpoint must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Browser CDP endpoint must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Browser CDP endpoint must be an HTTP origin")
    if parsed.hostname is None or parsed.port is None:
        raise ValueError("Browser CDP endpoint must include a host and port")
    host = parsed.hostname.casefold()
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if not is_loopback:
        raise ValueError("Browser CDP endpoint must use a loopback address")
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{rendered_host}:{parsed.port}"


def canonical_browser_profile_id(value: object) -> str:
    """Return a canonical UUID string or raise ``ValueError``.

    Profile IDs are persisted across databases, device references, and eventually
    profile manifests.  Accepting alternate UUID spellings at any of those
    boundaries would make equality and collision checks unreliable.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("Browser profile ID must be a canonical UUID")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Browser profile ID must be a canonical UUID") from exc
    if value != canonical:
        raise ValueError("Browser profile ID must be a canonical UUID")
    return canonical


class DeviceType(Enum):
    BROWSER = "browser"
    ANDROID = "android"
    WINDOWS = "windows"


class BrowserEngine(Enum):
    CHROMIUM = "chromium"
    CHROME = "chrome"
    MSEDGE = "msedge"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


class ScreenMode(Enum):
    DESKTOP = "desktop"
    MOBILE_ANDROID = "mobile_android"
    MOBILE_IOS = "mobile_ios"


@dataclass
class BrowserDeviceConfig:
    engine: BrowserEngine
    headless: bool = False
    headless_uses_headed_user_agent: bool = True
    screen_mode: ScreenMode = ScreenMode.DESKTOP
    profile_name: str | None = None
    profile_id: str | None = None
    cdp_endpoint: str | None = None
    cdp_auto_launch: bool = False
    companion_client_id: str | None = None
    companion_target_id: str | None = None
    companion_title: str | None = None
    companion_url: str | None = None

    def to_id(self) -> str:
        if self.companion_client_id and self.companion_target_id:
            payload = json.dumps(
                {
                    "client": self.companion_client_id,
                    "engine": self.engine.value,
                    "target": self.companion_target_id,
                    "title": self.companion_title or "",
                    "url": self.companion_url or "",
                },
                separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")
            encoded = base64.urlsafe_b64encode(payload).decode("ascii")
            return f"{_BROWSER_COMPANION_SAVED_ID_PREFIX}{encoded}"
        if self.cdp_endpoint:
            endpoint = canonical_browser_cdp_endpoint(self.cdp_endpoint)
            payload = json.dumps(
                {
                    "auto_launch": self.cdp_auto_launch,
                    "engine": self.engine.value,
                    "endpoint": endpoint,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            encoded = base64.urlsafe_b64encode(payload).decode("ascii")
            return f"{_BROWSER_CDP_SAVED_ID_PREFIX}{encoded}"
        if self.profile_id:
            profile_id = canonical_browser_profile_id(self.profile_id)
            return (
                f"{_BROWSER_PROFILE_SAVED_ID_PREFIX}{self.engine.value}|"
                f"{'hl' if self.headless else 'hd'}|{self.screen_mode.value}|"
                f"profile|{profile_id}"
            )
        parts = [self.engine.value, self.screen_mode.value]
        if self.profile_name:
            parts.append(self.profile_name)
        return "-".join(parts)

    def to_display_name(self) -> str:
        engine_name = self.engine.value.capitalize()
        if self.engine == BrowserEngine.MSEDGE:
            engine_name = "MS Edge"

        mode_names = {
            ScreenMode.DESKTOP: "Desktop",
            ScreenMode.MOBILE_ANDROID: "Mobile Android",
            ScreenMode.MOBILE_IOS: "Mobile iOS",
        }
        mode_name = mode_names[self.screen_mode]

        name = f"{engine_name} {mode_name}"
        if self.companion_target_id:
            name = (
                f"Browser Companion · {engine_name} · "
                f"{self.companion_title or 'Granted tab'}"
            )
        elif self.cdp_endpoint:
            endpoint = canonical_browser_cdp_endpoint(self.cdp_endpoint)
            action = "Managed CDP" if self.cdp_auto_launch else "Attached via CDP"
            name = f"{engine_name} · {action} ({endpoint})"
        elif self.profile_name:
            name += f" ({self.profile_name})"
        elif self.profile_id:
            profile_id = canonical_browser_profile_id(self.profile_id)
            name += f" (profile {profile_id[:8]})"
        return name

    @classmethod
    def from_id(cls, id_str: str) -> BrowserDeviceConfig:
        if not isinstance(id_str, str) or not id_str:
            raise ValueError("Saved browser device ID must be non-empty text")
        if id_str.startswith(_BROWSER_COMPANION_SAVED_ID_PREFIX):
            try:
                raw = base64.b64decode(
                    id_str.removeprefix(_BROWSER_COMPANION_SAVED_ID_PREFIX),
                    altchars=b"-_", validate=True,
                )
                payload = json.loads(raw.decode("utf-8"))
                return cls(
                    engine=BrowserEngine(payload["engine"]),
                    companion_client_id=str(payload["client"]),
                    companion_target_id=str(payload["target"]),
                    companion_title=str(payload.get("title") or ""),
                    companion_url=str(payload.get("url") or ""),
                )
            except (
                KeyError, ValueError, TypeError, UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                raise ValueError("Malformed browser companion device ID") from exc
        if id_str.startswith(_BROWSER_CDP_SAVED_ID_PREFIX):
            encoded = id_str.removeprefix(_BROWSER_CDP_SAVED_ID_PREFIX)
            try:
                raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
                payload = json.loads(raw.decode("utf-8"))
                engine = BrowserEngine(payload["engine"])
                endpoint = canonical_browser_cdp_endpoint(payload["endpoint"])
                auto_launch = payload.get("auto_launch", False)
                if type(auto_launch) is not bool:
                    raise ValueError("CDP auto-launch flag must be Boolean")
            except (
                KeyError,
                TypeError,
                ValueError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                raise ValueError("Malformed saved browser CDP device ID") from exc
            if engine not in {
                BrowserEngine.CHROMIUM,
                BrowserEngine.CHROME,
                BrowserEngine.MSEDGE,
            }:
                raise ValueError("Browser CDP attachment requires a Chromium browser")
            return cls(
                engine=engine,
                screen_mode=ScreenMode.DESKTOP,
                cdp_endpoint=endpoint,
                cdp_auto_launch=auto_launch,
            )
        if id_str.startswith(_BROWSER_PROFILE_SAVED_ID_PREFIX):
            parts = id_str.split("|")
            if (
                len(parts) != 7
                or parts[:2] != ["browser", "2"]
                or parts[3] not in {"hd", "hl"}
                or parts[5] != "profile"
            ):
                raise ValueError("Malformed versioned browser profile device ID")
            return cls(
                engine=BrowserEngine(parts[2]),
                headless=parts[3] == "hl",
                screen_mode=ScreenMode(parts[4]),
                profile_id=canonical_browser_profile_id(parts[6]),
            )
        parts = id_str.split("-", 2)
        engine = BrowserEngine(parts[0])
        screen_mode = ScreenMode(parts[1] if len(parts) > 1 else "desktop")
        profile_name = parts[2] if len(parts) > 2 else None
        return cls(
            engine=engine,
            headless=False,
            screen_mode=screen_mode,
            profile_name=profile_name,
        )


@runtime_checkable
class Device(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...


@dataclass
class BrowserDevice:
    config: BrowserDeviceConfig

    @property
    def id(self) -> str:
        return self.config.to_id()

    @property
    def name(self) -> str:
        return self.config.to_display_name()


@dataclass
class AndroidDevice:
    """An Android device connected via ADB."""
    device_id: str
    model: str = ""
    provider_id: str = ""
    provider_instance_id: str = ""
    provider_display_name: str = ""

    @property
    def id(self) -> str:
        if self.provider_id and self.provider_instance_id:
            return make_android_emulator_saved_device_id(
                self.provider_id,
                self.provider_instance_id,
            )
        return self.device_id

    @property
    def name(self) -> str:
        if self.provider_display_name:
            if self.model:
                return f"{self.provider_display_name} ({self.model})"
            return self.provider_display_name
        if self.model:
            return f"{self.model} ({self.device_id})"
        return self.device_id


@dataclass
class WindowsDevice:
    """A Windows application target, optionally stable across process restarts."""
    hwnd: int
    title: str
    executable_path: str = ""
    auto_start: bool = False

    @property
    def id(self) -> str:
        return str(self.hwnd)

    @property
    def name(self) -> str:
        return self.title or self.executable_path or f"Window {self.hwnd}"


_WINDOWS_SAVED_ID_PREFIX = "windows|"
_WINDOWS_APP_SAVED_ID_PREFIX = "windows-app|1|"
_ANDROID_EMULATOR_SAVED_ID_PREFIX = "android-emulator|"
_EMULATOR_PROVIDER_DISPLAY_NAMES = {
    "mumu": "MuMu",
}


def make_android_emulator_saved_device_id(
    provider_id: str,
    instance_id: str,
) -> str:
    provider = str(provider_id).strip()
    instance = str(instance_id).strip()
    if not provider or not instance or "|" in provider or "|" in instance:
        raise ValueError("Android emulator provider and instance IDs must be non-empty")
    return f"{_ANDROID_EMULATOR_SAVED_ID_PREFIX}{provider}|{instance}"


def parse_android_emulator_saved_device_id(
    device_id: object,
) -> tuple[str, str] | None:
    if not isinstance(device_id, str):
        return None
    if not device_id.startswith(_ANDROID_EMULATOR_SAVED_ID_PREFIX):
        return None
    parts = device_id.split("|")
    if len(parts) != 3:
        return None
    provider_id = parts[1].strip()
    instance_id = parts[2].strip()
    if not provider_id or not instance_id:
        return None
    return provider_id, instance_id


def make_windows_saved_device_id(hwnd: int, title: str) -> str:
    """Encode a Windows launcher target with both HWND and title."""
    return f"{_WINDOWS_SAVED_ID_PREFIX}{int(hwnd)}|{title}"


def make_windows_application_saved_device_id(
    hwnd: int,
    title: str,
    executable_path: str,
    *,
    auto_start: bool = False,
) -> str:
    """Encode a restartable Windows application target without delimiter risk."""
    executable = str(executable_path).strip()
    if not executable:
        raise ValueError("Windows application executable path must be non-empty")
    payload = json.dumps(
        {
            "hwnd": int(hwnd),
            "title": str(title),
            "executable_path": executable,
            "auto_start": bool(auto_start),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"{_WINDOWS_APP_SAVED_ID_PREFIX}{encoded}"


def parse_windows_application_saved_device_id(
    device_id: object,
) -> WindowsDevice | None:
    """Decode the versioned restartable Windows application target format."""
    if not isinstance(device_id, str) or not device_id.startswith(
        _WINDOWS_APP_SAVED_ID_PREFIX
    ):
        return None
    encoded = device_id.removeprefix(_WINDOWS_APP_SAVED_ID_PREFIX)
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    hwnd = payload.get("hwnd")
    title = payload.get("title")
    executable = payload.get("executable_path")
    auto_start = payload.get("auto_start", False)
    if (
        not isinstance(hwnd, int)
        or isinstance(hwnd, bool)
        or hwnd < 0
        or not isinstance(title, str)
        or not isinstance(executable, str)
        or not executable.strip()
        or not isinstance(auto_start, bool)
    ):
        return None
    return WindowsDevice(
        hwnd=hwnd,
        title=title,
        executable_path=executable.strip(),
        auto_start=auto_start,
    )


def parse_windows_saved_device_id(device_id: object) -> tuple[int | None, str] | None:
    """Decode a Windows launcher target stored in a run config.

    Returns ``(hwnd, title)`` for the new ``windows|HWND|TITLE`` format, or
    ``None`` when the ID is not a Windows launcher reference.
    """
    if not isinstance(device_id, str) or not device_id.startswith(
        _WINDOWS_SAVED_ID_PREFIX
    ):
        return None

    parts = device_id.split("|", 2)
    if len(parts) < 3:
        return None

    hwnd_text = parts[1].strip()
    title = parts[2]
    try:
        hwnd = int(hwnd_text) if hwnd_text else None
    except ValueError:
        return None
    return hwnd, title


def saved_device_id_for_device(device: Device) -> str:
    """Return the value that launcher run configs should persist."""
    if isinstance(device, WindowsDevice):
        if device.executable_path:
            return make_windows_application_saved_device_id(
                device.hwnd,
                device.title,
                device.executable_path,
                auto_start=device.auto_start,
            )
        return make_windows_saved_device_id(device.hwnd, device.title)
    return device.id


def display_name_for_saved_device_id(device_id: object) -> str:
    """Return a friendly device label for a persisted launcher target."""
    if not isinstance(device_id, str) or not device_id:
        return "Invalid saved target"
    parsed_android = parse_android_emulator_saved_device_id(device_id)
    if parsed_android is not None:
        provider_id, instance_id = parsed_android
        provider_name = _EMULATOR_PROVIDER_DISPLAY_NAMES.get(
            provider_id,
            provider_id.replace("_", " ").title(),
        )
        return f"{provider_name} instance {instance_id}"

    application = parse_windows_application_saved_device_id(device_id)
    if application is not None:
        return application.name
    if device_id.startswith(_WINDOWS_APP_SAVED_ID_PREFIX):
        return "Invalid saved target"
    parsed = parse_windows_saved_device_id(device_id)
    if parsed is not None:
        _hwnd, title = parsed
        return title or device_id
    if device_id.startswith(_WINDOWS_SAVED_ID_PREFIX):
        return "Invalid saved target"
    if device_id.startswith(_ANDROID_EMULATOR_SAVED_ID_PREFIX):
        return "Invalid saved target"
    if device_id.startswith(
        (_BROWSER_PROFILE_SAVED_ID_PREFIX, _BROWSER_COMPANION_SAVED_ID_PREFIX)
    ):
        try:
            return BrowserDeviceConfig.from_id(device_id).to_display_name()
        except (TypeError, ValueError, IndexError):
            return "Invalid saved target"
    if device_id.startswith(_BROWSER_CDP_SAVED_ID_PREFIX):
        try:
            return BrowserDeviceConfig.from_id(device_id).to_display_name()
        except (TypeError, ValueError, IndexError):
            return "Invalid saved target"
    if "|" in device_id:
        try:
            return device_from_saved_id(device_id).name
        except (TypeError, ValueError, IndexError):
            return "Invalid saved target"
    return device_id


def device_from_saved_id(device_id: object) -> Device:
    """Reconstruct a device from a launcher-saved identifier."""
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("Saved device ID must be non-empty text")
    application = parse_windows_application_saved_device_id(device_id)
    if application is not None:
        return application
    if device_id.startswith(_WINDOWS_APP_SAVED_ID_PREFIX):
        raise ValueError("Malformed saved Windows application ID")
    parsed_windows = parse_windows_saved_device_id(device_id)
    if parsed_windows is not None:
        hwnd, title = parsed_windows
        return WindowsDevice(hwnd=hwnd or 0, title=title)
    if device_id.startswith(_WINDOWS_SAVED_ID_PREFIX):
        raise ValueError("Malformed saved Windows device ID")

    parsed_android = parse_android_emulator_saved_device_id(device_id)
    if parsed_android is not None:
        provider_id, instance_id = parsed_android
        return AndroidDevice(
            device_id="",
            provider_id=provider_id,
            provider_instance_id=instance_id,
        )
    if device_id.startswith(_ANDROID_EMULATOR_SAVED_ID_PREFIX):
        raise ValueError("Malformed saved Android emulator device ID")

    if device_id.isdigit():
        return WindowsDevice(hwnd=int(device_id), title="")

    if device_id.startswith(
        (_BROWSER_PROFILE_SAVED_ID_PREFIX, _BROWSER_CDP_SAVED_ID_PREFIX,
         _BROWSER_COMPANION_SAVED_ID_PREFIX)
    ):
        return BrowserDevice(config=BrowserDeviceConfig.from_id(device_id))

    if "|" in device_id:
        parts = device_id.split("|")
        engine = BrowserEngine(parts[0].lower())
        headless = parts[1] == "hl" if len(parts) > 1 else False
        screen_mode = (
            ScreenMode(parts[2].lower()) if len(parts) > 2 else ScreenMode.DESKTOP
        )
        profile_name = (
            parts[4] if len(parts) > 4 and parts[3] == "perm" else None
        )
        return BrowserDevice(
            config=BrowserDeviceConfig(
                engine=engine,
                headless=headless,
                screen_mode=screen_mode,
                profile_name=profile_name,
            )
        )

    try:
        return BrowserDevice(config=BrowserDeviceConfig.from_id(device_id))
    except (ValueError, IndexError):
        return AndroidDevice(device_id=device_id)


@dataclass(frozen=True)
class BrowserProfileReference:
    """Stable or legacy browser-profile identity decoded from a saved device ID."""

    profile_id: str | None = None
    legacy_name: str | None = None


def browser_profile_reference_from_saved_device_id(
    device_id: object,
) -> BrowserProfileReference | None:
    """Safely decode a persistent browser-profile reference.

    Malformed and non-browser IDs are deliberately treated as unreferenced so
    migrations and profile management can preserve corrupt legacy data for
    diagnosis instead of failing the entire database.
    """
    if not isinstance(device_id, str) or not device_id:
        return None
    try:
        device = device_from_saved_id(device_id)
    except (TypeError, ValueError, IndexError):
        return None
    if not isinstance(device, BrowserDevice):
        return None
    if device.config.profile_id:
        try:
            return BrowserProfileReference(
                profile_id=canonical_browser_profile_id(device.config.profile_id)
            )
        except ValueError:
            return None
    if device.config.profile_name:
        return BrowserProfileReference(legacy_name=device.config.profile_name)
    return None


def browser_profile_id_from_saved_device_id(device_id: object) -> str | None:
    """Return the stable profile UUID encoded in a saved browser device ID."""
    reference = browser_profile_reference_from_saved_device_id(device_id)
    return reference.profile_id if reference is not None else None


def browser_profile_name_from_saved_device_id(device_id: object) -> str | None:
    """Safely return the profile identity encoded in a saved browser device ID.

    Keeping saved-ID parsing behind this helper gives profile-reference callers
    one migration point when run setups eventually store stable profile IDs.
    Malformed and non-browser legacy IDs are deliberately treated as unreferenced
    instead of making profile management fail.
    """
    reference = browser_profile_reference_from_saved_device_id(device_id)
    return reference.legacy_name if reference is not None else None


@dataclass
class DeviceSession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    device: Device | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def put_data(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get_data(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)
