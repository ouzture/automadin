from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from automadin.runtime import is_frozen

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_PREFIX = "AutomadinScheduler"


def _value_name(data_dir: str | Path) -> str:
    canonical = str(Path(data_dir).expanduser().resolve()).casefold()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{_VALUE_PREFIX}-{digest}"


def scheduler_startup_command(
    data_dir: str | Path,
    *,
    executable: str | Path | None = None,
    frozen: bool | None = None,
) -> str:
    launcher = str(executable or sys.executable)
    packaged = is_frozen() if frozen is None else frozen
    arguments = [launcher]
    if not packaged:
        arguments.extend(("-m", "automadin.app"))
    arguments.extend(("--scheduler-host", "--data-dir", str(Path(data_dir).resolve())))
    return subprocess.list2cmdline(arguments)


def scheduler_startup_enabled(data_dir: str | Path) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, _value_name(data_dir))
    except FileNotFoundError:
        return False
    return bool(str(value).strip())


def set_scheduler_startup_enabled(
    data_dir: str | Path,
    enabled: bool,
) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Scheduler sign-in launch is available only on Windows")
    import winreg

    name = _value_name(data_dir)
    if enabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(
                key,
                name,
                0,
                winreg.REG_SZ,
                scheduler_startup_command(data_dir),
            )
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        return
