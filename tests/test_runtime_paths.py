from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from automadin import app
from automadin import runtime
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


def _make_browser_payload(root: Path, *, managed: bool) -> None:
    executable_paths = {
        "chromium": Path("chrome-win64") / "chrome.exe",
        "firefox": Path("firefox") / "firefox.exe",
        "webkit": Path("Playwright.exe"),
    }
    root.mkdir(parents=True, exist_ok=True)
    for engine, revision in runtime.playwright_browser_revisions().items():
        install_dir = root / f"{engine}-{revision}"
        install_dir.mkdir()
        (install_dir / "INSTALLATION_COMPLETE").touch()
        executable = install_dir / executable_paths[engine]
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
    if managed:
        (root / "automadin-install.json").write_text("{}", encoding="utf-8")


def _make_managed_adb(data_dir: Path, version: str = "37.0.0") -> Path:
    adb_dir = data_dir / "runtime" / "platform-tools" / version
    adb_dir.mkdir(parents=True)
    for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "automadin-install.json"):
        (adb_dir / name).touch()
    (adb_dir.parent / "current.txt").write_text(version, encoding="utf-8")
    return adb_dir / "adb.exe"


def _successful_adb_run(*_args, **_kwargs):
    return SimpleNamespace(
        returncode=0,
        stdout="Android Debug Bridge version 1.0.41\nVersion 37.0.0-1234567",
        stderr="",
    )


def test_frozen_default_uses_local_app_data(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("AUTOMADIN_DATA_DIR", raising=False)
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(app, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "application_dir", lambda: tmp_path / "install")
    monkeypatch.setattr(app, "application_dir", lambda: tmp_path / "install")
    monkeypatch.setattr("sys.argv", ["Automadin.exe"])

    assert Path(app._resolve_data_dir()) == tmp_path / "Automadin"


def test_portable_mode_uses_data_beside_executable(monkeypatch, tmp_path: Path) -> None:
    install_dir = tmp_path / "Automadin-Portable"
    install_dir.mkdir()
    monkeypatch.delenv("AUTOMADIN_DATA_DIR", raising=False)
    monkeypatch.setattr(app, "application_dir", lambda: install_dir)
    monkeypatch.setattr("sys.argv", ["Automadin.exe", "--portable"])

    assert Path(app._resolve_data_dir()) == install_dir / "data"


def test_data_dir_override_has_priority(monkeypatch, tmp_path: Path) -> None:
    selected = tmp_path / "explicit"
    monkeypatch.setenv("AUTOMADIN_DATA_DIR", str(tmp_path / "environment"))
    monkeypatch.setattr("sys.argv", ["Automadin.exe", "--data-dir", str(selected)])

    assert Path(app._resolve_data_dir()) == selected


def test_installed_frozen_process_holds_setup_mutex_once(
    monkeypatch, tmp_path: Path
) -> None:
    created: list[str] = []
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.sys, "argv", ["Automadin.exe"])
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "application_dir", lambda: tmp_path / "installed")
    monkeypatch.setattr(
        runtime,
        "_create_windows_mutex",
        lambda name: created.append(name) or 123,
    )
    monkeypatch.setattr(runtime, "_installer_app_mutex_handle", None)

    assert runtime.hold_installer_app_mutex()
    assert runtime.hold_installer_app_mutex()
    assert created == [runtime.INSTALLER_APP_MUTEX_NAME]


def test_portable_and_source_processes_do_not_hold_setup_mutex(
    monkeypatch, tmp_path: Path
) -> None:
    created: list[str] = []
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        runtime,
        "_create_windows_mutex",
        lambda name: created.append(name) or 123,
    )
    monkeypatch.setattr(runtime, "_installer_app_mutex_handle", None)
    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    assert not runtime.hold_installer_app_mutex()

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime.sys, "argv", ["Automadin.exe", "--portable"])
    monkeypatch.setattr(runtime, "application_dir", lambda: tmp_path / "portable")
    assert not runtime.hold_installer_app_mutex()
    assert created == []


def test_bundled_runtime_takes_priority(monkeypatch, tmp_path: Path) -> None:
    install_dir = tmp_path / "app"
    browsers = install_dir / "browsers"
    platform_tools = install_dir / "tools" / "platform-tools"
    _make_browser_payload(browsers, managed=False)
    platform_tools.mkdir(parents=True)
    (platform_tools / "adb.exe").touch()
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("ADB_PATH", raising=False)
    monkeypatch.setattr(runtime, "application_dir", lambda: install_dir)
    monkeypatch.setattr(runtime.subprocess, "run", _successful_adb_run)

    configuration = runtime.configure_runtime(tmp_path / "data")

    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)
    assert os.environ["ADB_PATH"] == str(platform_tools / "adb.exe")
    assert configuration.browsers.source == "bundled"
    assert configuration.adb.source == "bundled"


def test_managed_runtime_sets_browser_and_adb_paths(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    browsers = runtime.managed_browsers_dir(data_dir)
    _make_browser_payload(browsers, managed=True)
    adb = _make_managed_adb(data_dir)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("ADB_PATH", raising=False)
    monkeypatch.setattr(runtime, "application_dir", lambda: tmp_path / "install")
    monkeypatch.setattr(runtime.subprocess, "run", _successful_adb_run)

    configuration = runtime.configure_runtime(data_dir)

    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)
    assert os.environ["ADB_PATH"] == str(adb)
    assert configuration.browsers.source == "managed"
    assert configuration.adb.source == "managed"


def test_managed_adb_rejects_unsafe_current_pointer(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    platform_root = data_dir / "runtime" / "platform-tools"
    platform_root.mkdir(parents=True)
    (platform_root / "current.txt").write_text("../outside", encoding="utf-8")

    assert runtime.managed_adb_path(data_dir) is None


def test_stale_environment_paths_are_replaced_by_healthy_managed_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    browsers = runtime.managed_browsers_dir(data_dir)
    _make_browser_payload(browsers, managed=True)
    adb = _make_managed_adb(data_dir)
    stale_browsers = tmp_path / "missing-browsers"
    stale_adb = tmp_path / "missing-adb.exe"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(stale_browsers))
    monkeypatch.setenv("ADB_PATH", str(stale_adb))
    monkeypatch.setattr(runtime, "application_dir", lambda: tmp_path / "install")
    monkeypatch.setattr(runtime.subprocess, "run", _successful_adb_run)

    configuration = runtime.configure_runtime(data_dir)

    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)
    assert os.environ["ADB_PATH"] == str(adb)
    assert configuration.browsers.ignored_override == stale_browsers
    assert configuration.adb.ignored_override == stale_adb


def test_valid_external_overrides_are_retained(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _make_browser_payload(runtime.managed_browsers_dir(data_dir), managed=True)
    _make_managed_adb(data_dir)
    external_browsers = tmp_path / "external-browsers"
    _make_browser_payload(external_browsers, managed=False)
    external_adb = tmp_path / "external" / "adb.exe"
    external_adb.parent.mkdir()
    external_adb.touch()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(external_browsers))
    monkeypatch.setenv("ADB_PATH", str(external_adb))
    monkeypatch.setattr(runtime, "application_dir", lambda: tmp_path / "install")
    monkeypatch.setattr(runtime.subprocess, "run", _successful_adb_run)

    configuration = runtime.configure_runtime(data_dir)

    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(external_browsers)
    assert os.environ["ADB_PATH"] == str(external_adb)
    assert configuration.browsers.source == "external"
    assert configuration.adb.source == "external"


def test_managed_browser_markers_without_executable_need_repair(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    browsers = runtime.managed_browsers_dir(data_dir)
    _make_browser_payload(browsers, managed=True)
    firefox = dict(runtime.inspect_browser_payload(browsers).executable_paths)["firefox"]
    firefox.unlink()

    status = runtime.inspect_browser_payload(browsers)

    assert status.managed_package_installed
    assert not status.usable
    assert status.missing_engines == ("firefox",)
    assert not runtime.managed_browsers_ready(data_dir)


def test_external_adb_readiness_reports_source_path_version_and_hidden_process(
    monkeypatch, tmp_path: Path
) -> None:
    external_adb = tmp_path / "external" / "adb.exe"
    external_adb.parent.mkdir()
    external_adb.touch()
    calls: list[tuple[tuple, dict]] = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return _successful_adb_run()

    monkeypatch.setenv("ADB_PATH", str(external_adb))
    monkeypatch.setattr(runtime, "application_dir", lambda: tmp_path / "install")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    status = runtime.adb_runtime_status(tmp_path / "data")

    assert status.usable
    assert status.source == "external"
    assert status.path == external_adb
    assert status.version == "Android Debug Bridge version 1.0.41; Version 37.0.0-1234567"
    assert calls[0][1]["creationflags"] == HIDDEN_PROCESS_CREATION_FLAGS
    assert calls[0][1]["timeout"] == 1.5


def test_zero_exit_non_adb_program_is_not_usable(monkeypatch, tmp_path: Path) -> None:
    fake_adb = tmp_path / "adb.exe"
    fake_adb.touch()
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="not actually adb", stderr=""
        ),
    )

    status = runtime.probe_adb(fake_adb)

    assert not status.usable
    assert "did not identify Android Debug Bridge" in (status.error or "")


def test_adb_probe_timeout_is_actionable_and_bounded(monkeypatch, tmp_path: Path) -> None:
    fake_adb = tmp_path / "adb.exe"
    fake_adb.touch()
    observed_timeout: list[float] = []

    def timeout_run(*args, **kwargs):
        observed_timeout.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(runtime.subprocess, "run", timeout_run)

    status = runtime.probe_adb(fake_adb)

    assert not status.usable
    assert observed_timeout == [1.5]
    assert "timed out" in (status.error or "").lower()
