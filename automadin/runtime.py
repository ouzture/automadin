"""Runtime paths shared by development and packaged Automadin builds."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS

if TYPE_CHECKING:
    from automadin.models.device import Device


APP_NAME = "Automadin"
PORTABLE_MARKER = "portable.flag"
INSTALLER_APP_MUTEX_NAME = (
    r"Local\Automadin.Application.14E54D2D-A7E4-4519-859B-AB98812DC5B3"
)
BROWSER_ENGINES = ("chromium", "firefox", "webkit")
BROWSER_EXECUTABLE_PATTERNS = {
    "chromium": ("chrome-win*/chrome.exe",),
    "firefox": ("firefox/firefox.exe",),
    "webkit": ("Playwright.exe",),
}
SYSTEM_BROWSER_CHANNELS = {
    "chrome": {
        "display_name": "Google Chrome",
        "executable": "chrome.exe",
        "relative_paths": (
            Path("Google") / "Chrome" / "Application" / "chrome.exe",
        ),
    },
    "msedge": {
        "display_name": "Microsoft Edge",
        "executable": "msedge.exe",
        "relative_paths": (
            Path("Microsoft") / "Edge" / "Application" / "msedge.exe",
        ),
    },
}

_installer_app_mutex_handle: int | None = None


@dataclass(frozen=True)
class BrowserPayloadStatus:
    root: Path
    manifest_present: bool
    markers_complete: bool
    usable: bool
    executable_paths: tuple[tuple[str, Path], ...]
    missing_engines: tuple[str, ...]

    @property
    def managed_package_installed(self) -> bool:
        return self.manifest_present and self.markers_complete


@dataclass(frozen=True)
class RuntimeComponentStatus:
    source: str
    path: Path | None
    usable: bool
    version: str | None = None
    error: str | None = None
    ignored_override: Path | None = None


@dataclass(frozen=True)
class RuntimeConfiguration:
    browsers: RuntimeComponentStatus
    adb: RuntimeComponentStatus


def is_frozen() -> bool:
    """Return whether Automadin is running from a frozen/compiled executable."""
    main_module = sys.modules.get("__main__")
    main_is_compiled = bool(
        main_module is not None and hasattr(main_module, "__compiled__")
    )
    return bool(
        getattr(sys, "frozen", False)
        or "__compiled__" in globals()
        or main_is_compiled
    )


def application_dir() -> Path:
    """Return the directory containing the installed executable or source tree."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _create_windows_mutex(name: str) -> int:
    """Create and retain one named Windows mutex handle."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    handle = create_mutex(None, False, name)
    if not handle:
        raise ctypes.WinError()
    return int(handle)


def hold_installer_app_mutex() -> bool:
    """Advertise an installed process so Setup cannot replace its executable.

    Inno Setup's ``AppMutex`` check uses this process-lifetime handle to stop an
    upgrade before files are touched. Portable and source processes deliberately
    do not claim the installed application's mutex.
    """
    global _installer_app_mutex_handle

    if _installer_app_mutex_handle is not None:
        return True
    if sys.platform != "win32" or not is_frozen():
        return False
    app_dir = application_dir()
    if "--portable" in sys.argv or (app_dir / PORTABLE_MARKER).is_file():
        return False
    _installer_app_mutex_handle = _create_windows_mutex(
        INSTALLER_APP_MUTEX_NAME
    )
    return True


def bundled_browsers_dir() -> Path:
    return application_dir() / "browsers"


def bundled_platform_tools_dir() -> Path:
    return application_dir() / "tools" / "platform-tools"


def managed_runtime_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "runtime"


def managed_browsers_dir(data_dir: str | Path) -> Path:
    """Return the browser path for the exact packaged Playwright version."""
    from playwright._repo_version import version as playwright_version

    return managed_runtime_dir(data_dir) / "browsers" / f"playwright-{playwright_version}"


def playwright_browser_revisions() -> dict[str, str]:
    """Read the browser revisions expected by the packaged Playwright driver."""
    try:
        from playwright._impl._driver import compute_driver_executable

        _node, cli = compute_driver_executable()
        registry_path = Path(cli).parent / "browsers.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}

    revisions: dict[str, str] = {}
    for entry in registry.get("browsers", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        revision = entry.get("revision")
        if name in BROWSER_ENGINES and isinstance(revision, str) and revision:
            revisions[name] = revision
    return revisions


def playwright_browser_versions() -> dict[str, str]:
    """Return browser product versions declared by this Playwright package."""
    try:
        from playwright._impl._driver import compute_driver_executable

        _node, cli = compute_driver_executable()
        registry_path = Path(cli).parent / "browsers.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}

    versions: dict[str, str] = {}
    for entry in registry.get("browsers", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        version = entry.get("browserVersion")
        if name in BROWSER_ENGINES and isinstance(version, str) and version:
            versions[name] = version
    return versions


def inspect_browser_payload(root: str | Path) -> BrowserPayloadStatus:
    """Inspect exact Playwright revisions and executable payloads without launching them."""
    browser_root = Path(root)
    revisions = playwright_browser_revisions()
    markers_complete = len(revisions) == len(BROWSER_ENGINES)
    executable_paths: list[tuple[str, Path]] = []
    missing_engines: list[str] = []

    for engine in BROWSER_ENGINES:
        revision = revisions.get(engine)
        install_dir = browser_root / f"{engine}-{revision}" if revision else None
        marker_ready = bool(
            install_dir is not None
            and install_dir.is_dir()
            and (install_dir / "INSTALLATION_COMPLETE").is_file()
        )
        markers_complete = markers_complete and marker_ready

        executable = None
        if marker_ready and install_dir is not None:
            for pattern in BROWSER_EXECUTABLE_PATTERNS[engine]:
                executable = next(
                    (candidate for candidate in install_dir.glob(pattern) if candidate.is_file()),
                    None,
                )
                if executable is not None:
                    break
        if executable is None:
            missing_engines.append(engine)
        else:
            executable_paths.append((engine, executable))

    return BrowserPayloadStatus(
        root=browser_root,
        manifest_present=(browser_root / "automadin-install.json").is_file(),
        markers_complete=markers_complete,
        usable=markers_complete and not missing_engines,
        executable_paths=tuple(executable_paths),
        missing_engines=tuple(missing_engines),
    )


def managed_browsers_package_installed(data_dir: str | Path) -> bool:
    return inspect_browser_payload(managed_browsers_dir(data_dir)).managed_package_installed


def managed_browsers_ready(data_dir: str | Path) -> bool:
    status = inspect_browser_payload(managed_browsers_dir(data_dir))
    return status.managed_package_installed and status.usable


def managed_adb_path(data_dir: str | Path) -> Path | None:
    """Return the currently selected managed ADB executable, if valid."""
    platform_root = managed_runtime_dir(data_dir) / "platform-tools"
    current_file = platform_root / "current.txt"
    if not current_file.is_file():
        return None
    try:
        version_dir = current_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not version_dir or Path(version_dir).name != version_dir:
        return None
    candidate = platform_root / version_dir / "adb.exe"
    required = (
        candidate,
        candidate.parent / "AdbWinApi.dll",
        candidate.parent / "AdbWinUsbApi.dll",
        candidate.parent / "automadin-install.json",
    )
    return candidate if all(path.is_file() for path in required) else None


def default_installed_data_dir() -> Path:
    """Return the per-user writable data location for an installed build."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def _environment_path(variable: str) -> Path | None:
    value = os.environ.get(variable, "").strip()
    return Path(os.path.expandvars(value)).expanduser() if value else None


def _paths_equal(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _runtime_source(path: Path, managed: Path | None, bundled: Path) -> str:
    if managed is not None and _paths_equal(path, managed):
        return "managed"
    if _paths_equal(path, bundled):
        return "bundled"
    return "external"


def browser_runtime_status(data_dir: str | Path) -> RuntimeComponentStatus:
    """Select a complete browser payload and report where it came from."""
    managed = managed_browsers_dir(data_dir)
    bundled = bundled_browsers_dir()
    override = _environment_path("PLAYWRIGHT_BROWSERS_PATH")
    if override is not None and inspect_browser_payload(override).usable:
        return RuntimeComponentStatus(
            source=_runtime_source(override, managed, bundled),
            path=override,
            usable=True,
        )

    for source, candidate in (("bundled", bundled), ("managed", managed)):
        payload = inspect_browser_payload(candidate)
        if payload.usable and (source != "managed" or payload.managed_package_installed):
            return RuntimeComponentStatus(
                source=source,
                path=candidate,
                usable=True,
                ignored_override=override,
            )
    return RuntimeComponentStatus(
        source="unavailable",
        path=None,
        usable=False,
        error="No complete Playwright browser payload was detected.",
        ignored_override=override,
    )


def _windows_app_path(executable: str) -> Path | None:
    """Read a Windows App Paths registration without launching the executable."""
    try:
        import winreg
    except ImportError:
        return None

    key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    access_modes = (
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for access in access_modes:
            try:
                with winreg.OpenKey(hive, key_name, 0, access) as key:
                    value, _value_type = winreg.QueryValueEx(key, None)
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                candidate = Path(
                    os.path.expandvars(value.strip().strip('"'))
                ).expanduser()
                if candidate.is_file():
                    return candidate
    return None


def _system_browser_common_paths(channel: str) -> tuple[Path, ...]:
    details = SYSTEM_BROWSER_CHANNELS[channel]
    roots = (
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    )
    return tuple(
        Path(root) / relative_path
        for root in roots
        if root
        for relative_path in details["relative_paths"]
    )


def system_browser_channel_status(channel: str) -> RuntimeComponentStatus:
    """Report a system Chrome or Edge installation without launching it."""
    normalized_channel = str(channel).strip().casefold()
    details = SYSTEM_BROWSER_CHANNELS.get(normalized_channel)
    if details is None:
        return RuntimeComponentStatus(
            source="unavailable",
            path=None,
            usable=False,
            error=f"Unsupported system browser channel: {channel!r}.",
        )

    executable = str(details["executable"])
    candidates: list[Path] = []
    path_match = shutil.which(executable)
    if path_match:
        candidates.append(Path(path_match))
    app_path = _windows_app_path(executable)
    if app_path is not None:
        candidates.append(app_path)
    candidates.extend(_system_browser_common_paths(normalized_channel))

    checked: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in checked:
            continue
        checked.add(key)
        if candidate.is_file():
            return RuntimeComponentStatus(
                source="system",
                path=candidate,
                usable=True,
            )

    display_name = str(details["display_name"])
    return RuntimeComponentStatus(
        source="unavailable",
        path=None,
        usable=False,
        error=(
            f"{display_name} was not detected. Install {display_name} or make "
            f"{executable} available on PATH."
        ),
    )


def device_runtime_status(
    device: Device | object,
    data_dir: str | Path,
) -> RuntimeComponentStatus:
    """Report whether the selected device's runtime is available, without mutation."""
    from automadin.models.device import (
        AndroidDevice,
        BrowserDevice,
        BrowserEngine,
        WindowsDevice,
    )

    if isinstance(device, BrowserDevice):
        engine = device.config.engine
        if engine in {
            BrowserEngine.CHROMIUM,
            BrowserEngine.FIREFOX,
            BrowserEngine.WEBKIT,
        }:
            return browser_runtime_status(data_dir)
        if engine == BrowserEngine.CHROME:
            return system_browser_channel_status("chrome")
        if engine == BrowserEngine.MSEDGE:
            return system_browser_channel_status("msedge")
    elif isinstance(device, AndroidDevice):
        return adb_runtime_status(data_dir)
    elif isinstance(device, WindowsDevice):
        if sys.platform == "win32":
            return RuntimeComponentStatus(source="system", path=None, usable=True)
        return RuntimeComponentStatus(
            source="unavailable",
            path=None,
            usable=False,
            error="Windows automation is only available when Automadin runs on Windows.",
        )

    return RuntimeComponentStatus(
        source="unavailable",
        path=None,
        usable=False,
        error=f"Unsupported device type: {type(device).__name__}.",
    )


def probe_adb(adb_path: str | Path, *, timeout: float = 1.5) -> RuntimeComponentStatus:
    """Run a bounded, non-interactive ``adb version`` capability probe."""
    candidate = Path(adb_path)
    if not candidate.is_file():
        return RuntimeComponentStatus(
            source="unavailable",
            path=candidate,
            usable=False,
            error="ADB executable does not exist.",
        )
    try:
        result = subprocess.run(
            [str(candidate), "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RuntimeComponentStatus(
            source="unavailable",
            path=candidate,
            usable=False,
            error=str(exc),
        )
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    )
    if result.returncode != 0:
        return RuntimeComponentStatus(
            source="unavailable",
            path=candidate,
            usable=False,
            error=output or f"adb version exited with code {result.returncode}.",
        )
    if "android debug bridge" not in output.casefold():
        return RuntimeComponentStatus(
            source="unavailable",
            path=candidate,
            usable=False,
            error="Version command did not identify Android Debug Bridge.",
        )
    version_lines = [line.strip() for line in output.splitlines() if line.strip()]
    return RuntimeComponentStatus(
        source="unavailable",
        path=candidate,
        usable=True,
        version="; ".join(version_lines[:2]) or "Version command succeeded",
    )


def adb_runtime_status(data_dir: str | Path) -> RuntimeComponentStatus:
    """Select and capability-test external, bundled, then managed ADB."""
    managed = managed_adb_path(data_dir)
    bundled = bundled_platform_tools_dir() / "adb.exe"
    override = _environment_path("ADB_PATH")
    candidates: list[tuple[str, Path]] = []
    if override is not None:
        candidates.append((_runtime_source(override, managed, bundled), override))
    candidates.append(("bundled", bundled))
    if managed is not None:
        candidates.append(("managed", managed))

    attempted: set[str] = set()
    last_error: str | None = None
    for source, candidate in candidates:
        key = os.path.normcase(str(candidate.resolve()))
        if key in attempted:
            continue
        attempted.add(key)
        probe = probe_adb(candidate)
        if probe.usable:
            return RuntimeComponentStatus(
                source=source,
                path=candidate,
                usable=True,
                version=probe.version,
                ignored_override=override if override is not None and candidate != override else None,
            )
        if candidate.is_file():
            last_error = probe.error

    return RuntimeComponentStatus(
        source="unavailable",
        path=None,
        usable=False,
        error=last_error or "No usable ADB executable was detected.",
        ignored_override=override,
    )


def configure_runtime(data_dir: str | Path) -> RuntimeConfiguration:
    """Select validated runtime components and replace stale environment overrides."""
    browsers = browser_runtime_status(data_dir)
    adb = adb_runtime_status(data_dir)

    if browsers.path is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers.path)
    else:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    if adb.path is not None:
        os.environ["ADB_PATH"] = str(adb.path)
    else:
        os.environ.pop("ADB_PATH", None)
    return RuntimeConfiguration(browsers=browsers, adb=adb)
