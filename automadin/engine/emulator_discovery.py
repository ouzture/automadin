"""Discover running Android emulator instances and their current ADB endpoints."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmulatorAdbEndpoint:
    provider_id: str
    instance_id: str
    display_name: str
    host: str
    port: int

    @property
    def serial(self) -> str:
        return f"{self.host}:{self.port}"


class EmulatorProvider(Protocol):
    provider_id: str
    provider_display_name: str

    def discover(self) -> list[EmulatorAdbEndpoint]: ...


def _running_mumu_process_paths() -> list[Path]:
    if sys.platform != "win32":
        return []

    try:
        import win32api  # type: ignore
        import win32con  # type: ignore
        import win32process  # type: ignore
    except ImportError:
        logger.debug("pywin32 unavailable; skipping running MuMu process lookup")
        return []

    wanted = {"mumunxmain.exe", "mumunxdevice.exe"}
    paths: list[Path] = []
    access = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
    for pid in win32process.EnumProcesses():
        handle = None
        try:
            handle = win32api.OpenProcess(access, False, pid)
            path = Path(win32process.GetModuleFileNameEx(handle, 0))
            if path.name.casefold() in wanted:
                paths.append(path)
        except Exception:
            continue
        finally:
            if handle is not None:
                try:
                    win32api.CloseHandle(handle)
                except Exception:
                    pass
    return paths


def _registered_mumu_install_locations() -> list[Path]:
    """Return MuMu install roots recorded by Windows uninstall entries."""
    if sys.platform != "win32":
        return []

    try:
        import winreg
    except ImportError:
        return []

    uninstall_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    views = (
        (winreg.HKEY_CURRENT_USER, 0),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    )
    locations: list[Path] = []
    for hive, view in views:
        try:
            root = winreg.OpenKey(hive, uninstall_key, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    entry = winreg.OpenKey(root, subkey_name)
                    with entry:
                        display_name = str(
                            winreg.QueryValueEx(entry, "DisplayName")[0]
                        ).strip()
                        location = str(
                            winreg.QueryValueEx(entry, "InstallLocation")[0]
                        ).strip()
                except OSError:
                    continue
                if "mumu" in display_name.casefold() and location:
                    locations.append(Path(location))
    return locations


def mumu_manager_candidates(
    *,
    process_paths: Sequence[Path] | None = None,
    install_locations: Sequence[Path] | None = None,
    environ: Mapping[str, str] | None = None,
    require_existing: bool = True,
) -> list[Path]:
    """Return ordered MuMuManager candidates without scanning drives."""
    env = os.environ if environ is None else environ
    candidates: list[Path] = []

    configured = str(env.get("MUMU_MANAGER_PATH", "")).strip()
    if configured:
        candidates.append(Path(configured))

    paths = _running_mumu_process_paths() if process_paths is None else process_paths
    for process_path in paths:
        name = process_path.name.casefold()
        if name == "mumunxmain.exe":
            candidates.append(process_path.parent / "MuMuManager.exe")
        elif name == "mumunxdevice.exe":
            nx_device = next(
                (parent for parent in process_path.parents if parent.name.casefold() == "nx_device"),
                None,
            )
            if nx_device is not None:
                candidates.append(nx_device.parent / "nx_main" / "MuMuManager.exe")

    locations = (
        _registered_mumu_install_locations()
        if install_locations is None
        else install_locations
    )
    for location in locations:
        root = Path(location)
        candidates.extend(
            [
                root / "MuMuManager.exe",
                root / "nx_main" / "MuMuManager.exe",
                root / "shell" / "MuMuManager.exe",
            ]
        )

    roots = [
        str(env.get("PROGRAMFILES", "")).strip(),
        str(env.get("PROGRAMFILES(X86)", "")).strip(),
        str(env.get("LOCALAPPDATA", "")).strip(),
    ]
    for root_text in roots:
        if not root_text:
            continue
        root = Path(root_text)
        candidates.extend(
            [
                root / "MuMuPlayer" / "nx_main" / "MuMuManager.exe",
                root / "NetEase" / "MuMuPlayer" / "nx_main" / "MuMuManager.exe",
                root / "Netease" / "MuMuPlayerGlobal-12.0" / "shell" / "MuMuManager.exe",
                root / "MuMuPlayerGlobal-12.0" / "shell" / "MuMuManager.exe",
            ]
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen or (require_existing and not candidate.is_file()):
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


class MumuEmulatorProvider:
    provider_id = "mumu"
    provider_display_name = "MuMu"

    def __init__(
        self,
        *,
        manager_candidates: Callable[[], Iterable[Path]] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._manager_candidates = manager_candidates or mumu_manager_candidates
        self._runner = runner

    def discover(self) -> list[EmulatorAdbEndpoint]:
        manager = next(
            (Path(path) for path in self._manager_candidates() if Path(path).is_file()),
            None,
        )
        if manager is None:
            return []

        kwargs: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "timeout": 5,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            result = self._runner(
                [str(manager), "info", "--vmindex", "all"],
                **kwargs,
            )
            if result.returncode != 0:
                logger.warning("MuMu manager discovery failed: %s", result.stderr.strip())
                return []
            payload = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError) as exc:
            logger.warning("MuMu manager discovery failed: %s", exc)
            return []

        if not isinstance(payload, dict):
            return []

        endpoints: list[EmulatorAdbEndpoint] = []
        for key, raw in payload.items():
            if not isinstance(raw, dict) or not raw.get("is_process_started"):
                continue
            host = str(raw.get("adb_host_ip", "")).strip()
            try:
                port = int(raw.get("adb_port", 0))
            except (TypeError, ValueError):
                continue
            if not host or not 1 <= port <= 65535:
                continue

            instance_id = str(raw.get("index", key)).strip()
            if not instance_id:
                continue
            instance_name = str(raw.get("name", "")).strip()
            display_name = (
                f"MuMu: {instance_name}" if instance_name else f"MuMu instance {instance_id}"
            )
            endpoints.append(
                EmulatorAdbEndpoint(
                    provider_id=self.provider_id,
                    instance_id=instance_id,
                    display_name=display_name,
                    host=host,
                    port=port,
                )
            )
        return endpoints


def default_emulator_providers() -> list[EmulatorProvider]:
    return [MumuEmulatorProvider()]
