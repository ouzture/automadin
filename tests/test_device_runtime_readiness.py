from pathlib import Path

import pytest

from automadin import runtime
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    WindowsDevice,
)
from automadin.runtime import RuntimeComponentStatus


def _browser(engine: BrowserEngine) -> BrowserDevice:
    return BrowserDevice(BrowserDeviceConfig(engine=engine))


@pytest.mark.parametrize(
    "engine",
    [BrowserEngine.CHROMIUM, BrowserEngine.FIREFOX, BrowserEngine.WEBKIT],
)
def test_playwright_engines_use_managed_browser_runtime(
    monkeypatch, tmp_path: Path, engine: BrowserEngine
) -> None:
    managed_status = RuntimeComponentStatus(
        source="unavailable",
        path=None,
        usable=False,
        error="managed payload missing",
    )
    monkeypatch.setattr(runtime, "browser_runtime_status", lambda data_dir: managed_status)
    monkeypatch.setattr(
        runtime,
        "system_browser_channel_status",
        lambda channel: pytest.fail(f"system browser lookup called for {channel}"),
    )

    assert runtime.device_runtime_status(_browser(engine), tmp_path) is managed_status


@pytest.mark.parametrize(
    ("engine", "channel"),
    [
        (BrowserEngine.CHROME, "chrome"),
        (BrowserEngine.MSEDGE, "msedge"),
    ],
)
def test_system_channels_do_not_depend_on_managed_chromium(
    monkeypatch, tmp_path: Path, engine: BrowserEngine, channel: str
) -> None:
    executable = tmp_path / f"{channel}.exe"
    system_status = RuntimeComponentStatus(
        source="system",
        path=executable,
        usable=True,
    )
    monkeypatch.setattr(
        runtime,
        "browser_runtime_status",
        lambda data_dir: pytest.fail(f"managed browser lookup called for {data_dir}"),
    )
    monkeypatch.setattr(
        runtime,
        "system_browser_channel_status",
        lambda requested: system_status
        if requested == channel
        else pytest.fail(f"unexpected channel {requested}"),
    )

    assert runtime.device_runtime_status(_browser(engine), tmp_path) is system_status


def test_system_browser_detects_common_program_files_path(
    monkeypatch, tmp_path: Path
) -> None:
    executable = (
        tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(runtime.shutil, "which", lambda _executable: None)
    monkeypatch.setattr(runtime, "_windows_app_path", lambda _executable: None)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    status = runtime.system_browser_channel_status("msedge")

    assert status == RuntimeComponentStatus(
        source="system",
        path=executable,
        usable=True,
    )


def test_android_device_delegates_to_adb_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    adb_status = RuntimeComponentStatus(
        source="managed",
        path=tmp_path / "adb.exe",
        usable=True,
    )
    monkeypatch.setattr(runtime, "adb_runtime_status", lambda data_dir: adb_status)

    status = runtime.device_runtime_status(AndroidDevice("serial"), tmp_path)

    assert status is adb_status


def test_windows_device_is_usable_on_windows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "win32")

    status = runtime.device_runtime_status(WindowsDevice(1, "Window"), tmp_path)

    assert status.usable
    assert status.source == "system"
    assert status.path is None


def test_windows_device_is_actionably_unavailable_elsewhere(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "linux")

    status = runtime.device_runtime_status(WindowsDevice(1, "Window"), tmp_path)

    assert not status.usable
    assert status.source == "unavailable"
    assert status.error is not None
    assert "only available" in status.error
    assert "Windows" in status.error


def test_unsupported_device_returns_unavailable_status(tmp_path: Path) -> None:
    status = runtime.device_runtime_status(object(), tmp_path)

    assert not status.usable
    assert status.source == "unavailable"
    assert status.error == "Unsupported device type: object."
