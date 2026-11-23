"""ADB device engine — discovers and connects to Android devices via ADB."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from math import ceil
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from automadin.engine.emulator_discovery import (
    EmulatorAdbEndpoint,
    EmulatorProvider,
    default_emulator_providers,
)
from automadin.models.android_file import (
    AndroidFileEntry,
    AndroidRootState,
    PulledAndroidFile,
)
from automadin.models.device import AndroidDevice
from automadin.models.managed_emulator import ManagedEmulatorTargetResolution
from automadin.runtime import application_dir
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS

logger = logging.getLogger(__name__)

ADB_PATH_SETTING_KEY = "adb_path"
_ANDROID_PACKAGE_NAME = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_ANDROID_ACTIVITY_NAME = re.compile(
    r"^\.?[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*$"
)
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


class AdbDiscoveryDiagnosticKind(Enum):
    """Machine-readable outcomes from an ``adb devices`` probe."""

    SUCCESS = "success"
    MISSING_EXECUTABLE = "missing_executable"
    TIMEOUT = "timeout"
    COMMAND_FAILED = "command_failed"
    UNAUTHORIZED = "unauthorized"
    OFFLINE = "offline"


class AndroidTargetReadinessKind(Enum):
    """Stable reasons an Android run can be rejected before execution."""

    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    TARGET_UNAVAILABLE = "target_unavailable"
    RECONNECT_FAILED = "reconnect_failed"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"


class AndroidTargetReadinessError(RuntimeError):
    """An actionable, safe-to-display Android startup failure."""

    def __init__(
        self,
        kind: AndroidTargetReadinessKind,
        target_id: str,
        message: str,
        action: str,
    ) -> None:
        self.kind = kind
        self.target_id = target_id
        self.action = action
        self.public_message = f"{message} {action}".strip()
        super().__init__(self.public_message)


class AdbRuntimeUnavailableError(RuntimeError):
    """ADB could not be launched, as distinct from a target connection failure."""


@dataclass(frozen=True)
class AdbDiscoveryDiagnostic:
    kind: AdbDiscoveryDiagnosticKind
    message: str
    action: str = ""
    detail: str = ""
    device_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdbDiscoveryResult:
    devices: tuple[AndroidDevice, ...]
    diagnostics: tuple[AdbDiscoveryDiagnostic, ...]

    @property
    def command_succeeded(self) -> bool:
        return any(
            diagnostic.kind == AdbDiscoveryDiagnosticKind.SUCCESS
            for diagnostic in self.diagnostics
        )


class AdbDiscoveredDevices(list[AndroidDevice]):
    """List-compatible discovery output carrying its structured diagnostics."""

    def __init__(self, result: AdbDiscoveryResult) -> None:
        super().__init__(result.devices)
        self.discovery_result = result


def _bundled_adb_candidates() -> list[str]:
    """Return project-local ADB locations, if platform-tools is bundled later."""
    base_dirs = [
        application_dir(),
        Path.cwd(),
        Path(__file__).resolve().parents[2],
    ]
    candidates: list[str] = []
    for base_dir in base_dirs:
        candidates.append(str(base_dir / "tools" / "platform-tools" / "adb.exe"))
        candidates.append(str(base_dir / "platform-tools" / "adb.exe"))
    return candidates


def find_adb(configured_path: str | None = None) -> str:
    """Auto-detect ADB executable path.

    Search order:
    1. Stored app setting, when provided
    2. ADB_PATH environment variable
    3. Project-local platform-tools folder
    4. Android SDK platform-tools from ANDROID_HOME or ANDROID_SDK_ROOT
    5. Android SDK platform-tools under LOCALAPPDATA
    6. shutil.which("adb") for PATH lookup
    7. Fall back to "adb"
    """
    if configured_path:
        path = Path(configured_path)
        if path.is_file():
            logger.info("ADB found via app setting: %s", configured_path)
            return str(path)
        logger.warning("Configured ADB path does not exist: %s", configured_path)

    # Environment variable
    env_path = os.environ.get("ADB_PATH")
    if env_path and Path(env_path).is_file():
        logger.info("ADB found via ADB_PATH env: %s", env_path)
        return env_path

    # Standard Android SDK and application-managed locations.
    common_paths = _bundled_adb_candidates()
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_root = os.environ.get(variable)
        if sdk_root:
            common_paths.append(
                os.path.join(sdk_root, "platform-tools", "adb.exe")
            )
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        common_paths.append(
            os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe")
        )

    for candidate in common_paths:
        if Path(candidate).is_file():
            logger.info("ADB found at common path: %s", candidate)
            return candidate

    # PATH lookup
    which_result = shutil.which("adb")
    if which_result:
        logger.info("ADB found on PATH: %s", which_result)
        return which_result

    logger.warning("ADB not found in any known location, falling back to 'adb'")
    return "adb"


def adb_path_from_db(db: Any | None) -> str | None:
    if db is None or not hasattr(db, "get_app_setting"):
        return None
    value = db.get_app_setting(ADB_PATH_SETTING_KEY, None)
    return str(value).strip() if value else None


class AdbDeviceEngine:
    """Discovers Android devices connected via ADB and manages sessions."""

    def __init__(
        self,
        adb_path: str | None = None,
        db: Any | None = None,
        emulator_providers: Sequence[EmulatorProvider] | None = None,
    ) -> None:
        if adb_path:
            self._adb_path = adb_path
        else:
            self._adb_path = find_adb(adb_path_from_db(db))
        self._emulator_providers = list(
            default_emulator_providers()
            if emulator_providers is None
            else emulator_providers
        )

    @property
    def adb_path(self) -> str:
        return self._adb_path

    @staticmethod
    def parse_root_state(output: str) -> AndroidRootState:
        match = re.search(r"uid=(\d+)\(([^)]*)\)", output.strip())
        if not match:
            return AndroidRootState(uid=None, username="", raw=output.strip())
        return AndroidRootState(
            uid=int(match.group(1)),
            username=match.group(2),
            raw=output.strip(),
        )

    @staticmethod
    def _join_device_path(parent: str, name: str) -> str:
        parent = parent.rstrip("/") or "/"
        if parent == "/":
            return f"/{name}"
        return f"{parent}/{name}"

    @staticmethod
    def parse_ls_long_line(line: str, parent_path: str) -> AndroidFileEntry | None:
        line = line.strip()
        if not line or line.startswith("total "):
            return None
        parts = line.split(maxsplit=7)
        if len(parts) < 8:
            return None
        mode, _links, owner, group, size_text, date_text, time_text, name = parts
        if mode.startswith("l") and " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in {".", ".."}:
            return None
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        return AndroidFileEntry(
            path=AdbDeviceEngine._join_device_path(parent_path, name),
            name=name,
            is_dir=mode.startswith("d"),
            size=size,
            mode=mode,
            owner=owner,
            group=group,
            modified=f"{date_text} {time_text}",
        )

    @staticmethod
    def quote_shell_path(path: str) -> str:
        return "'" + path.replace("'", "'\\''") + "'"

    def shell_text(
        self,
        device_id: str,
        *args: str,
        timeout: int = 15,
        check: bool = True,
    ) -> str:
        result = subprocess.run(
            [self._adb_path, "-s", device_id, "shell", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        ).strip()
        if check and result.returncode != 0:
            raise RuntimeError(output or f"adb shell {' '.join(args)} failed")
        return output

    def root_state(self, session_data: dict[str, object]) -> AndroidRootState:
        device_id = str(session_data["device_id"])
        return self.parse_root_state(self.shell_text(device_id, "id", timeout=5))

    def foreground_package(self, session_data: dict[str, object]) -> str | None:
        device_id = str(session_data["device_id"])
        output = self.shell_text(
            device_id,
            "dumpsys activity activities",
            timeout=5,
            check=False,
        )
        for line in output.splitlines():
            if not any(
                token in line
                for token in (
                    "topResumedActivity",
                    "mResumedActivity",
                    "ResumedActivity",
                    "mFocusedApp",
                    "mCurrentFocus",
                )
            ):
                continue
            match = re.search(r"\s([A-Za-z0-9._]+)/[A-Za-z0-9._$]+", line)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def validate_package_name(package_name: object) -> str:
        """Return a normalized Android package name or reject unsafe input."""
        if not isinstance(package_name, str):
            raise ValueError("Android package name must be text")
        normalized = package_name.strip()
        if not _ANDROID_PACKAGE_NAME.fullmatch(normalized):
            raise ValueError(
                "Android package name must use dot-separated identifier segments"
            )
        return normalized

    @staticmethod
    def validate_activity_name(activity_name: object) -> str:
        """Return a normalized explicit activity class name."""
        if not isinstance(activity_name, str):
            raise ValueError("Android activity name must be text")
        normalized = activity_name.strip()
        if not _ANDROID_ACTIVITY_NAME.fullmatch(normalized):
            raise ValueError(
                "Android activity name must be a class name such as "
                "'.MainActivity' or 'com.example.MainActivity'"
            )
        return normalized

    @staticmethod
    def validate_deep_link(uri: object) -> str:
        """Return a normalized URI suitable for an Android VIEW intent."""
        if not isinstance(uri, str):
            raise ValueError("Android deep link must be text")
        normalized = uri.strip()
        if not normalized or len(normalized) > 4096:
            raise ValueError("Android deep link must contain 1 to 4096 characters")
        if any(character.isspace() or ord(character) < 32 for character in normalized):
            raise ValueError("Android deep link cannot contain whitespace or controls")
        scheme = urlsplit(normalized).scheme
        if not scheme or not _URI_SCHEME.fullmatch(scheme):
            raise ValueError("Android deep link must include a valid URI scheme")
        return normalized

    @staticmethod
    def parse_installed_packages(output: str) -> list[str]:
        """Parse stable package identifiers from ``pm list packages`` output."""
        packages: set[str] = set()
        for line in output.splitlines():
            value = line.strip()
            if value.startswith("package:"):
                value = value.removeprefix("package:").strip()
            if _ANDROID_PACKAGE_NAME.fullmatch(value):
                packages.add(value)
        return sorted(packages, key=str.casefold)

    @staticmethod
    def _raise_for_activity_manager_error(output: str) -> None:
        lowered = output.casefold()
        if any(
            marker in lowered
            for marker in (
                "error:",
                "exception occurred",
                "unable to resolve intent",
                "activity class",
            )
        ):
            raise RuntimeError(output or "Android Activity Manager command failed")

    def list_installed_packages(
        self,
        session_data: dict[str, object],
        *,
        include_system: bool = False,
    ) -> list[str]:
        """List installed package names, defaulting to user-installed apps."""
        args = ["pm", "list", "packages"]
        if not include_system:
            args.append("-3")
        output = self.shell_text(
            str(session_data["device_id"]),
            *args,
            timeout=15,
        )
        return self.parse_installed_packages(output)

    def start_activity(
        self,
        session_data: dict[str, object],
        package_name: str,
        activity_name: str,
        *,
        timeout: float = 15.0,
    ) -> str:
        """Start one explicit activity and confirm its package reaches foreground."""
        package = self.validate_package_name(package_name)
        activity = self.validate_activity_name(activity_name)
        component = f"{package}/{activity}"
        output = self.shell_text(
            str(session_data["device_id"]),
            "am",
            "start",
            "-W",
            "-n",
            component,
            timeout=max(1, ceil(timeout)),
        )
        self._raise_for_activity_manager_error(output)
        self.wait_for_foreground_package(session_data, package, timeout=timeout)
        return output

    def open_deep_link(
        self,
        session_data: dict[str, object],
        uri: str,
        *,
        package_name: str = "",
        timeout: float = 15.0,
    ) -> str:
        """Open a VIEW intent, optionally constrained to a package."""
        link = self.validate_deep_link(uri)
        package = (
            self.validate_package_name(package_name) if package_name.strip() else ""
        )
        args = [
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            link,
        ]
        if package:
            args.extend(("-p", package))
        output = self.shell_text(
            str(session_data["device_id"]),
            *args,
            timeout=max(1, ceil(timeout)),
        )
        self._raise_for_activity_manager_error(output)
        if package:
            self.wait_for_foreground_package(session_data, package, timeout=timeout)
        return output

    def install_apk(
        self,
        session_data: dict[str, object],
        apk_path: str | os.PathLike[str],
        *,
        replace_existing: bool = True,
        grant_permissions: bool = False,
        timeout: int = 120,
    ) -> str:
        """Install one local APK on the selected device."""
        source = Path(apk_path).expanduser()
        if source.suffix.casefold() != ".apk":
            raise ValueError("Android install source must be an .apk file")
        if not source.is_file():
            raise FileNotFoundError(f"APK file does not exist: {source}")

        device_id = str(session_data["device_id"])
        command = [self._adb_path, "-s", device_id, "install"]
        if replace_existing:
            command.append("-r")
        if grant_permissions:
            command.append("-g")
        command.append(str(source.resolve()))
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        ).strip()
        if result.returncode != 0 or "success" not in output.casefold():
            raise RuntimeError(output or f"adb install failed for {source.name}")
        return output

    def force_stop_package(
        self,
        session_data: dict[str, object],
        package_name: str,
    ) -> None:
        """Stop an Android package without clearing its application data."""
        package = self.validate_package_name(package_name)
        self.shell_text(
            str(session_data["device_id"]),
            "am",
            "force-stop",
            package,
            timeout=10,
        )

    def wait_for_foreground_package(
        self,
        session_data: dict[str, object],
        package_name: str,
        *,
        timeout: float = 15.0,
        poll_interval: float = 0.25,
    ) -> None:
        """Wait until the requested package owns the foreground activity."""
        package = self.validate_package_name(package_name)
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if self.foreground_package(session_data) == package:
                return
            time.sleep(max(0.01, min(float(poll_interval), deadline - time.monotonic())))
        current = self.foreground_package(session_data)
        raise TimeoutError(
            f"Android package '{package}' did not reach the foreground"
            + (f"; current foreground package is '{current}'" if current else "")
        )

    def ensure_package_foreground(
        self,
        session_data: dict[str, object],
        package_name: str,
        *,
        timeout: float = 15.0,
    ) -> bool:
        """Launch a package only when needed and confirm it reaches foreground."""
        package = self.validate_package_name(package_name)
        if self.foreground_package(session_data) == package:
            return False
        self.launch_package(session_data, package)
        self.wait_for_foreground_package(session_data, package, timeout=timeout)
        return True

    def restart_package(
        self,
        session_data: dict[str, object],
        package_name: str,
        *,
        timeout: float = 15.0,
    ) -> None:
        """Force-stop, relaunch, and verify an Android package."""
        package = self.validate_package_name(package_name)
        self.force_stop_package(session_data, package)
        self.launch_package(session_data, package)
        self.wait_for_foreground_package(session_data, package, timeout=timeout)

    def send_home(self, session_data: dict[str, object]) -> None:
        device_id = str(session_data["device_id"])
        result = subprocess.run(
            [self._adb_path, "-s", device_id, "shell", "input", "keyevent", "3"],
            capture_output=True,
            timeout=5,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or "adb shell input keyevent 3 failed")

    def launch_package(self, session_data: dict[str, object], package_name: str) -> None:
        package_name = self.validate_package_name(package_name)
        device_id = str(session_data["device_id"])
        result = subprocess.run(
            [
                self._adb_path,
                "-s",
                device_id,
                "shell",
                "monkey",
                "-p",
                package_name,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        ).strip()
        if result.returncode != 0 or "Events injected: 1" not in output:
            raise RuntimeError(output or f"adb monkey launch {package_name} failed")

    def restart_adbd_as_root(self, session_data: dict[str, object]) -> str:
        device_id = str(session_data["device_id"])
        result = subprocess.run(
            [self._adb_path, "-s", device_id, "root"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        ).strip()
        if result.returncode != 0:
            raise RuntimeError(output or "adb root failed")
        subprocess.run(
            [self._adb_path, "-s", device_id, "wait-for-device"],
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        return output

    def list_directory(
        self,
        session_data: dict[str, object],
        path: str,
    ) -> list[AndroidFileEntry]:
        device_id = str(session_data["device_id"])
        quoted = self.quote_shell_path(path)
        output = self.shell_text(device_id, f"ls -la {quoted}", timeout=15)
        entries = [
            entry
            for line in output.splitlines()
            if (entry := self.parse_ls_long_line(line, path)) is not None
        ]
        return sorted(entries, key=lambda item: (not item.is_dir, item.name.lower()))

    def read_file_text(
        self,
        session_data: dict[str, object],
        path: str,
        max_bytes: int = 262144,
    ) -> tuple[str, bool]:
        device_id = str(session_data["device_id"])
        quoted = self.quote_shell_path(path)
        byte_limit = int(max_bytes)
        result = subprocess.run(
            [
                self._adb_path,
                "-s",
                device_id,
                "shell",
                f"head -c {byte_limit + 1} {quoted}",
            ],
            capture_output=True,
            timeout=15,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or f"adb shell head -c {byte_limit + 1} failed")
        output_bytes = result.stdout
        truncated = len(output_bytes) > byte_limit
        if truncated:
            output_bytes = output_bytes[:byte_limit]
        return output_bytes.decode("utf-8", errors="replace"), truncated

    def pull_file_to_temp(
        self,
        session_data: dict[str, object],
        path: str,
        temp_dir: str | None = None,
    ) -> PulledAndroidFile:
        device_id = str(session_data["device_id"])
        base_dir = temp_dir or tempfile.mkdtemp(prefix="automadin-android-file-")
        os.makedirs(base_dir, exist_ok=True)
        local_path = os.path.join(
            base_dir, os.path.basename(path.rstrip("/")) or "pulled-file"
        )
        result = subprocess.run(
            [self._adb_path, "-s", device_id, "pull", path, local_path],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        ).strip()
        if result.returncode != 0:
            raise RuntimeError(output or f"adb pull {path} failed")
        return PulledAndroidFile(
            device_path=path,
            local_path=local_path,
            size=os.path.getsize(local_path),
        )

    def _query_adb_devices(self) -> AdbDiscoveryResult:
        """Run ``adb devices`` and retain actionable states for every target."""
        try:
            result = subprocess.run(
                [self._adb_path, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
            )
        except FileNotFoundError as exc:
            message = f"ADB executable was not found at '{self._adb_path}'."
            logger.warning(message)
            return AdbDiscoveryResult(
                devices=(),
                diagnostics=(
                    AdbDiscoveryDiagnostic(
                        kind=AdbDiscoveryDiagnosticKind.MISSING_EXECUTABLE,
                        message=message,
                        action=(
                            "Install ADB from Tools > Runtime Components or choose "
                            "adb.exe from Tools > ADB Location."
                        ),
                        detail=str(exc),
                    ),
                ),
            )
        except subprocess.TimeoutExpired as exc:
            message = "ADB device discovery timed out."
            logger.warning(message)
            return AdbDiscoveryResult(
                devices=(),
                diagnostics=(
                    AdbDiscoveryDiagnostic(
                        kind=AdbDiscoveryDiagnosticKind.TIMEOUT,
                        message=message,
                        action=(
                            "Reconnect the device and retry. If this continues, restart "
                            "ADB or repair it from Tools > Runtime Components."
                        ),
                        detail=str(exc),
                    ),
                ),
            )
        except OSError as exc:
            message = f"ADB could not be started from '{self._adb_path}'."
            logger.warning("%s %s", message, exc)
            return AdbDiscoveryResult(
                devices=(),
                diagnostics=(
                    AdbDiscoveryDiagnostic(
                        kind=AdbDiscoveryDiagnosticKind.COMMAND_FAILED,
                        message=message,
                        action=(
                            "Check file permissions or choose a working adb.exe from "
                            "Tools > ADB Location."
                        ),
                        detail=str(exc),
                    ),
                ),
            )

        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        ).strip()
        if result.returncode != 0:
            message = f"ADB device discovery failed with exit code {result.returncode}."
            logger.warning("%s %s", message, output)
            return AdbDiscoveryResult(
                devices=(),
                diagnostics=(
                    AdbDiscoveryDiagnostic(
                        kind=AdbDiscoveryDiagnosticKind.COMMAND_FAILED,
                        message=message,
                        action=(
                            "Test the selected executable in Tools > ADB Location or "
                            "repair ADB from Tools > Runtime Components."
                        ),
                        detail=output,
                    ),
                ),
            )

        devices: list[AndroidDevice] = []
        unauthorized_ids: list[str] = []
        offline_ids: list[str] = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line or line.casefold().startswith("list of devices attached"):
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            device_id = parts[0]
            status = parts[1]
            if status == "unauthorized":
                unauthorized_ids.append(device_id)
                continue
            if status == "offline":
                offline_ids.append(device_id)
                continue
            if status != "device":
                continue

            # Try to extract model from the -l output (model:XXX)
            model = ""
            for part in parts[2:]:
                if part.startswith("model:"):
                    model = part.split(":", 1)[1].replace("_", " ")
                    break

            devices.append(AndroidDevice(device_id=device_id, model=model))

        devices = self._deduplicate_devices(devices)
        diagnostics: list[AdbDiscoveryDiagnostic] = []
        if unauthorized_ids:
            diagnostics.append(
                AdbDiscoveryDiagnostic(
                    kind=AdbDiscoveryDiagnosticKind.UNAUTHORIZED,
                    message=(
                        f"{len(unauthorized_ids)} Android device"
                        f"{' is' if len(unauthorized_ids) == 1 else 's are'} waiting "
                        "for USB debugging authorization."
                    ),
                    action=(
                        "Unlock each device, accept the USB debugging prompt, then "
                        "click Refresh."
                    ),
                    device_ids=tuple(unauthorized_ids),
                )
            )
        if offline_ids:
            diagnostics.append(
                AdbDiscoveryDiagnostic(
                    kind=AdbDiscoveryDiagnosticKind.OFFLINE,
                    message=(
                        f"{len(offline_ids)} Android device"
                        f"{' is' if len(offline_ids) == 1 else 's are'} offline."
                    ),
                    action=(
                        "Reconnect or restart each device, then click Refresh. "
                        "Restart ADB if it remains offline."
                    ),
                    device_ids=tuple(offline_ids),
                )
            )
        ready_count = len(devices)
        diagnostics.append(
            AdbDiscoveryDiagnostic(
                kind=AdbDiscoveryDiagnosticKind.SUCCESS,
                message=(
                    f"ADB is ready; {ready_count} Android device"
                    f"{' is' if ready_count == 1 else 's are'} available."
                    if ready_count
                    else "ADB is ready; no connected Android devices were reported."
                ),
                action=(
                    "Connect a device, enable USB debugging, and click Refresh."
                    if not ready_count and not unauthorized_ids and not offline_ids
                    else ""
                ),
            )
        )
        return AdbDiscoveryResult(
            devices=tuple(devices),
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _deduplicate_devices(devices: list[AndroidDevice]) -> list[AndroidDevice]:
        unique: list[AndroidDevice] = []
        seen: set[str] = set()
        for device in devices:
            if device.device_id in seen:
                continue
            seen.add(device.device_id)
            unique.append(device)
        return unique

    def _discover_emulator_endpoints(self) -> list[EmulatorAdbEndpoint]:
        endpoints: list[EmulatorAdbEndpoint] = []
        seen: set[tuple[str, str, str]] = set()
        for provider in self._emulator_providers:
            try:
                discovered = provider.discover()
            except Exception as exc:
                logger.warning(
                    "%s emulator discovery failed: %s",
                    getattr(provider, "provider_display_name", provider.provider_id),
                    exc,
                )
                continue
            for endpoint in discovered:
                key = (endpoint.provider_id, endpoint.instance_id, endpoint.serial)
                if key in seen:
                    continue
                seen.add(key)
                endpoints.append(endpoint)
        return endpoints

    def discover_managed_emulator_target(
        self,
        provider_id: str,
        instance_id: str,
    ) -> ManagedEmulatorTargetResolution | None:
        """Resolve one provider instance to its current ready ADB endpoint.

        Generic device discovery intentionally returns only connected ADB
        devices.  That loses the provider identity when an emulator is
        transitioning between endpoints or when ``adb connect`` races the
        provider becoming ready.  Managed acquisition needs the provider
        identity first, then must verify that the exact endpoint is online;
        it must never infer an instance from a model or from another endpoint.
        """
        normalized_provider = str(provider_id).casefold()
        normalized_instance = str(instance_id)
        endpoint = next(
            (
                candidate
                for candidate in self._discover_emulator_endpoints()
                if candidate.provider_id.casefold() == normalized_provider
                and candidate.instance_id == normalized_instance
            ),
            None,
        )
        if endpoint is None:
            return None

        def ready_device() -> AndroidDevice | None:
            discovery = self._query_adb_devices()
            if not discovery.command_succeeded:
                return None
            for device in discovery.devices:
                if device.device_id.casefold() != endpoint.serial.casefold():
                    continue
                return replace(
                    device,
                    provider_id=endpoint.provider_id,
                    provider_instance_id=endpoint.instance_id,
                    provider_display_name=endpoint.display_name,
                )
            return None

        existing = ready_device()
        if existing is not None:
            return ManagedEmulatorTargetResolution(running=True, device=existing)

        try:
            self.connect_tcp(endpoint.host, endpoint.port)
        except (AdbRuntimeUnavailableError, OSError) as exc:
            logger.warning(
                "Could not reconnect managed %s instance %s at %s: %s",
                endpoint.provider_id,
                endpoint.instance_id,
                endpoint.serial,
                exc,
            )
        except RuntimeError as exc:
            logger.info(
                "Managed %s instance %s is not ready at %s: %s",
                endpoint.provider_id,
                endpoint.instance_id,
                endpoint.serial,
                exc,
            )
        return ManagedEmulatorTargetResolution(running=True, device=ready_device())

    def discover_devices(self) -> AdbDiscoveryResult:
        """Discover ADB devices and return structured command/device diagnostics."""
        discovery = self._query_adb_devices()
        if not discovery.command_succeeded:
            return discovery

        devices = self._deduplicate_devices(list(discovery.devices))
        endpoints = self._discover_emulator_endpoints()
        connected_serials = {device.device_id for device in devices}
        attempted_connect = False

        for endpoint in endpoints:
            if endpoint.serial in connected_serials:
                continue
            attempted_connect = True
            try:
                self.connect_tcp(endpoint.host, endpoint.port)
            except RuntimeError as exc:
                logger.warning(
                    "Could not connect %s instance %s at %s: %s",
                    endpoint.provider_id,
                    endpoint.instance_id,
                    endpoint.serial,
                    exc,
                )

        if attempted_connect:
            discovery = self._query_adb_devices()
            if not discovery.command_succeeded:
                return discovery
            devices = self._deduplicate_devices(list(discovery.devices))

        endpoint_by_serial = {endpoint.serial: endpoint for endpoint in endpoints}
        annotated: list[AndroidDevice] = []
        for device in devices:
            endpoint = endpoint_by_serial.get(device.device_id)
            if endpoint is None:
                annotated.append(device)
                continue
            annotated.append(
                replace(
                    device,
                    provider_id=endpoint.provider_id,
                    provider_instance_id=endpoint.instance_id,
                    provider_display_name=endpoint.display_name,
                )
            )
        return AdbDiscoveryResult(
            devices=tuple(annotated),
            diagnostics=discovery.diagnostics,
        )

    def list_devices(self) -> list[AndroidDevice]:
        """Return list-compatible devices while retaining discovery diagnostics."""
        return AdbDiscoveredDevices(self.discover_devices())

    def connect_tcp(self, host: str, port: int) -> str:
        """Run ``adb connect host:port`` and return the command output."""
        target = f"{host}:{int(port)}"
        try:
            result = subprocess.run(
                [self._adb_path, "connect", target],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
            )
        except FileNotFoundError as exc:
            raise AdbRuntimeUnavailableError(
                f"ADB not found at '{self._adb_path}'"
            ) from exc
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ADB timed out connecting to '{target}'")
        except OSError as exc:
            raise AdbRuntimeUnavailableError(
                f"ADB could not be started from '{self._adb_path}'"
            ) from exc

        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        ).strip()
        output_lower = output.casefold()
        failed = (
            result.returncode != 0
            or "failed to connect" in output_lower
            or "unable to connect" in output_lower
            or "cannot connect" in output_lower
        )
        if failed:
            raise RuntimeError(output or f"adb connect {target} failed")
        return output or f"connected to {target}"

    def _resolve_emulator_device(self, device: AndroidDevice) -> str:
        provider = next(
            (
                candidate
                for candidate in self._emulator_providers
                if candidate.provider_id == device.provider_id
            ),
            None,
        )
        provider_name = (
            getattr(provider, "provider_display_name", device.provider_id)
            if provider is not None
            else device.provider_id.replace("_", " ").title()
        )
        if provider is not None:
            try:
                endpoint = next(
                    (
                        candidate
                        for candidate in provider.discover()
                        if candidate.instance_id == device.provider_instance_id
                    ),
                    None,
                )
            except Exception as exc:
                logger.warning(
                    "%s emulator resolution failed: %s",
                    provider_name,
                    exc,
                )
                endpoint = None
        else:
            endpoint = None

        if endpoint is None:
            raise RuntimeError(
                f"{provider_name} instance {device.provider_instance_id} "
                "is not running or discoverable"
            )

        self.connect_tcp(endpoint.host, endpoint.port)
        return endpoint.serial

    @staticmethod
    def _tcp_target(device_id: str) -> tuple[str, int] | None:
        """Return an exact ADB TCP endpoint without guessing for USB serials."""
        host, separator, raw_port = device_id.rpartition(":")
        if not separator or not host or not raw_port.isdecimal():
            return None
        port = int(raw_port)
        if not 1 <= port <= 65535:
            return None
        return host, port

    @staticmethod
    def _readiness_error_for_state(
        device_id: str,
        output: str,
    ) -> AndroidTargetReadinessError:
        normalized = output.casefold()
        if "unauthorized" in normalized:
            return AndroidTargetReadinessError(
                AndroidTargetReadinessKind.UNAUTHORIZED,
                device_id,
                f"Android target '{device_id}' is waiting for USB debugging authorization.",
                "Unlock the device, accept the USB debugging prompt, then run it again.",
            )
        if "offline" in normalized:
            return AndroidTargetReadinessError(
                AndroidTargetReadinessKind.OFFLINE,
                device_id,
                f"Android target '{device_id}' is offline.",
                "Reconnect or restart the device or emulator, then run it again. "
                "Restart ADB if it remains offline.",
            )
        return AndroidTargetReadinessError(
            AndroidTargetReadinessKind.TARGET_UNAVAILABLE,
            device_id,
            f"Android target '{device_id}' is not connected or ready.",
            "Connect the device or start the emulator, then run it again.",
        )

    def connect(self, device: AndroidDevice) -> dict[str, object]:
        """Initialize ADB session data for the given device.

        Returns a dict to be stored in DeviceSession.data.
        For ADB, session initialization is lightweight — we just verify
        the device is reachable.
        """
        device_id = device.device_id
        provider_target = bool(device.provider_id and device.provider_instance_id)
        if device.provider_id and device.provider_instance_id:
            try:
                device_id = self._resolve_emulator_device(device)
            except (AdbRuntimeUnavailableError, OSError) as exc:
                logger.warning("ADB could not resolve the Android provider target: %s", exc)
                raise AndroidTargetReadinessError(
                    AndroidTargetReadinessKind.RUNTIME_UNAVAILABLE,
                    device.id,
                    "ADB could not be started while resolving the Android target.",
                    "Check the configured ADB executable or repair it in "
                    "Tools > Runtime Components, then run it again.",
                ) from exc
            except RuntimeError as exc:
                provider = next(
                    (
                        candidate
                        for candidate in self._emulator_providers
                        if candidate.provider_id == device.provider_id
                    ),
                    None,
                )
                provider_name = (
                    getattr(provider, "provider_display_name", "")
                    or device.provider_display_name
                    or device.provider_id.replace("_", " ").title()
                )
                target_label = (
                    f"{provider_name} "
                    f"instance {device.provider_instance_id}"
                )
                logger.warning("Android provider target is unavailable: %s", exc)
                raise AndroidTargetReadinessError(
                    AndroidTargetReadinessKind.TARGET_UNAVAILABLE,
                    device.id,
                    f"Android target {target_label} is not running or discoverable.",
                    "Start that emulator instance or repair its managed profile, "
                    "then run it again.",
                ) from exc

        tcp_target = self._tcp_target(device_id)
        if tcp_target is not None and not provider_target:
            try:
                self.connect_tcp(*tcp_target)
            except (AdbRuntimeUnavailableError, OSError) as exc:
                logger.warning("ADB could not start for TCP reconnection: %s", exc)
                raise AndroidTargetReadinessError(
                    AndroidTargetReadinessKind.RUNTIME_UNAVAILABLE,
                    device_id,
                    "ADB could not be started while reconnecting the Android target.",
                    "Check the configured ADB executable or repair it in "
                    "Tools > Runtime Components, then run it again.",
                ) from exc
            except RuntimeError as exc:
                logger.warning("Could not reconnect Android target %s: %s", device_id, exc)
                raise AndroidTargetReadinessError(
                    AndroidTargetReadinessKind.RECONNECT_FAILED,
                    device_id,
                    f"Automadin could not reconnect Android target '{device_id}'.",
                    "Start the emulator or check its ADB host and port, then run it again.",
                ) from exc

        # Verify device is online
        try:
            result = subprocess.run(
                [self._adb_path, "-s", device_id, "get-state"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
            )
            state = result.stdout.strip()
            detail = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            if result.returncode != 0 or state != "device":
                logger.warning(
                    "Android readiness probe failed for %s (exit=%s): %s",
                    device_id,
                    result.returncode,
                    detail,
                )
                raise self._readiness_error_for_state(device_id, detail or state)
        except FileNotFoundError as exc:
            raise AndroidTargetReadinessError(
                AndroidTargetReadinessKind.RUNTIME_UNAVAILABLE,
                device_id,
                f"ADB was not found at '{self._adb_path}'.",
                "Repair ADB in Tools > Runtime Components, then run it again.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AndroidTargetReadinessError(
                AndroidTargetReadinessKind.TARGET_UNAVAILABLE,
                device_id,
                f"Android target '{device_id}' did not respond before the readiness timeout.",
                "Reconnect or restart the device or emulator, then run it again.",
            ) from exc
        except OSError as exc:
            logger.warning("ADB readiness command could not start: %s", exc)
            raise AndroidTargetReadinessError(
                AndroidTargetReadinessKind.RUNTIME_UNAVAILABLE,
                device_id,
                "ADB could not be started for Android run readiness.",
                "Check the configured ADB executable or repair it in "
                "Tools > Runtime Components, then run it again.",
            ) from exc

        return {
            "adb_path": self._adb_path,
            "device_id": device_id,
        }

    def disconnect(self, session_data: dict[str, object]) -> None:
        """No-op for ADB — nothing to close."""
        pass

    def take_screenshot(self, session_data: dict[str, object]) -> bytes:
        """Capture a screenshot via ADB screencap."""
        device_id = str(session_data["device_id"])
        try:
            result = subprocess.run(
                [self._adb_path, "-s", device_id, "exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=10,
                creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
            )
            if result.returncode != 0:
                raise RuntimeError(f"screencap failed: {result.stderr.decode()}")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError("ADB screenshot timed out")
