from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from automadin import runtime, runtime_components
from automadin.runtime import managed_browsers_dir
from automadin.runtime_components import (
    ADB_REQUIRED_FILES,
    application_command,
    browser_health_worker_command,
    install_adb,
    install_playwright_browsers,
    launch_runtime_manager,
    playwright_install_environment,
    playwright_installed,
    runtime_install_lock,
    runtime_manager_command,
)


class _FakeProcess:
    def __init__(self, return_code: int = 0) -> None:
        self.stdout = iter(["downloading\n"])
        self._return_code = return_code

    def wait(self) -> int:
        return self._return_code

    def poll(self) -> int:
        return self._return_code

    def terminate(self) -> None:
        self._return_code = -1


def _make_browser_payload(root: Path) -> None:
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


def test_runtime_install_lock_rejects_concurrent_manager(tmp_path: Path) -> None:
    with runtime_install_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already installing"):
            with runtime_install_lock(tmp_path):
                pass


def test_runtime_manager_command_recognizes_installed_executable(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "Automadin.exe"
    executable.touch()
    monkeypatch.setattr(
        runtime_components.QCoreApplication, "applicationFilePath", lambda: ""
    )
    monkeypatch.setattr(runtime_components.sys, "argv", ["main.py"])
    monkeypatch.setattr(runtime_components.sys, "executable", str(executable))
    monkeypatch.setattr(runtime_components, "is_frozen", lambda: False)

    assert runtime_manager_command(tmp_path / "data") == [
        str(executable),
        "--runtime-manager",
        "--data-dir",
        str(tmp_path / "data"),
    ]


def test_runtime_manager_command_uses_pythonw_for_source(
    monkeypatch, tmp_path: Path
) -> None:
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    project = tmp_path / "project"
    monkeypatch.setattr(
        runtime_components.QCoreApplication, "applicationFilePath", lambda: ""
    )
    monkeypatch.setattr(runtime_components.sys, "argv", ["main.py"])
    monkeypatch.setattr(runtime_components.sys, "executable", str(python))
    monkeypatch.setattr(runtime_components, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime_components, "application_dir", lambda: project)

    assert runtime_manager_command(tmp_path / "data") == [
        str(pythonw),
        str(project / "main.py"),
        "--runtime-manager",
        "--data-dir",
        str(tmp_path / "data"),
    ]


def test_runtime_manager_command_prefers_qt_packaged_executable(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "installed" / "Automadin.exe"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(
        runtime_components.QCoreApplication,
        "applicationFilePath",
        lambda: str(executable),
    )
    monkeypatch.setattr(
        runtime_components.sys, "executable", str(tmp_path / "missing" / "python.exe")
    )
    monkeypatch.setattr(runtime_components.sys, "argv", ["main.py"])
    monkeypatch.setattr(runtime_components, "is_frozen", lambda: False)

    assert runtime_manager_command(tmp_path / "data")[0] == str(executable)


def test_application_command_uses_source_main(monkeypatch, tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    project = tmp_path / "project"
    monkeypatch.setattr(
        runtime_components.QCoreApplication, "applicationFilePath", lambda: ""
    )
    monkeypatch.setattr(runtime_components.sys, "argv", ["main.py"])
    monkeypatch.setattr(runtime_components.sys, "executable", str(python))
    monkeypatch.setattr(runtime_components, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime_components, "application_dir", lambda: project)

    assert application_command("--example", "value") == [
        str(python),
        str(project / "main.py"),
        "--example",
        "value",
    ]


def test_browser_health_worker_command_uses_packaged_executable(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "Automadin.exe"
    executable.touch()
    monkeypatch.setattr(
        runtime_components.QCoreApplication,
        "applicationFilePath",
        lambda: str(executable),
    )

    assert browser_health_worker_command("chromium") == [
        str(executable),
        "--browser-health-worker",
        "chromium",
    ]


def test_browser_health_worker_command_uses_source_python(
    monkeypatch, tmp_path: Path
) -> None:
    python = tmp_path / "python.exe"
    project = tmp_path / "project"
    monkeypatch.setattr(
        runtime_components.QCoreApplication, "applicationFilePath", lambda: ""
    )
    monkeypatch.setattr(runtime_components.sys, "argv", ["main.py"])
    monkeypatch.setattr(runtime_components.sys, "executable", str(python))
    monkeypatch.setattr(runtime_components, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime_components, "application_dir", lambda: project)

    assert browser_health_worker_command("firefox") == [
        str(python),
        str(project / "main.py"),
        "--browser-health-worker",
        "firefox",
    ]


def test_runtime_manager_launch_reports_missing_launcher(monkeypatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing.exe"
    monkeypatch.setattr(
        runtime_components,
        "runtime_manager_command",
        lambda _data_dir: [str(missing), "--runtime-manager"],
    )
    monkeypatch.setattr(
        runtime_components.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError(2, "missing")),
    )

    with pytest.raises(RuntimeError, match=r"missing\.exe"):
        launch_runtime_manager(tmp_path / "data")


def test_runtime_manager_launch_passes_configured_adb_in_copied_environment(
    monkeypatch, tmp_path: Path
) -> None:
    command = [str(tmp_path / "Automadin.exe"), "--runtime-manager"]
    captured: dict[str, object] = {}
    inherited_adb = str(tmp_path / "inherited" / "adb.exe")
    configured_adb = tmp_path / "configured" / "adb.exe"
    monkeypatch.setenv("ADB_PATH", inherited_adb)
    monkeypatch.setenv("AUTOMADIN_TEST_MARKER", "preserved")
    monkeypatch.setattr(
        runtime_components, "runtime_manager_command", lambda _data_dir: command
    )

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(runtime_components.subprocess, "Popen", fake_popen)

    process = launch_runtime_manager(tmp_path / "data", configured_adb)

    assert isinstance(process, _FakeProcess)
    assert captured["args"] == command
    environment = captured["kwargs"]["env"]
    assert environment is not runtime_components.os.environ
    assert environment["ADB_PATH"] == str(configured_adb)
    assert environment["AUTOMADIN_TEST_MARKER"] == "preserved"
    assert runtime_components.os.environ["ADB_PATH"] == inherited_adb


def test_runtime_manager_launch_does_not_add_unconfigured_adb(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("ADB_PATH", raising=False)
    monkeypatch.setattr(
        runtime_components,
        "runtime_manager_command",
        lambda _data_dir: [str(tmp_path / "Automadin.exe"), "--runtime-manager"],
    )

    def fake_popen(_args, **kwargs):
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(runtime_components.subprocess, "Popen", fake_popen)

    launch_runtime_manager(tmp_path / "data")

    assert "ADB_PATH" not in captured["env"]


def test_nuitka_main_module_marks_runtime_as_frozen(monkeypatch) -> None:
    class CompiledMain:
        __compiled__ = object()

    monkeypatch.setitem(runtime.sys.modules, "__main__", CompiledMain())
    monkeypatch.delattr(runtime.sys, "frozen", raising=False)

    assert runtime.is_frozen()

def _make_adb_archive(path: Path, version: str = "37.0.0") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for filename in ADB_REQUIRED_FILES:
            content = b"test"
            if filename == "source.properties":
                content = f"Pkg.UserSrc=false\nPkg.Revision={version}\n".encode()
            archive.writestr(f"platform-tools/{filename}", content)
        archive.writestr("platform-tools/fastboot.exe", b"must not be installed")


def test_adb_install_keeps_only_required_files(tmp_path: Path) -> None:
    archive = tmp_path / "platform-tools.zip"
    _make_adb_archive(archive)
    data_dir = tmp_path / "data"
    progress: list[tuple[int, str]] = []

    adb = install_adb(
        data_dir,
        download_url=archive.as_uri(),
        progress=lambda percent, message: progress.append((percent, message)),
        verify=lambda _path: "Android Debug Bridge version 1.0.41",
    )

    assert adb == data_dir / "runtime" / "platform-tools" / "37.0.0" / "adb.exe"
    assert adb.is_file()
    assert not (adb.parent / "fastboot.exe").exists()
    assert (adb.parent.parent / "current.txt").read_text(encoding="utf-8") == "37.0.0"
    assert (adb.parent / "automadin-install.json").is_file()
    assert progress[-1][0] == 100


def test_cancelled_adb_install_leaves_no_active_runtime(tmp_path: Path) -> None:
    archive = tmp_path / "platform-tools.zip"
    _make_adb_archive(archive)
    data_dir = tmp_path / "data"

    with pytest.raises(RuntimeError, match="cancelled"):
        install_adb(
            data_dir,
            download_url=archive.as_uri(),
            cancelled=lambda: True,
            verify=lambda _path: "unused",
        )

    assert not (data_dir / "runtime" / "platform-tools" / "current.txt").exists()


def test_playwright_status_is_version_scoped(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    browser_dir = managed_browsers_dir(data_dir)
    browser_dir.mkdir(parents=True)
    assert not playwright_installed(data_dir)

    _make_browser_payload(browser_dir)
    (browser_dir / "automadin-install.json").write_text("{}", encoding="utf-8")

    assert playwright_installed(data_dir)
    assert playwright_install_environment(data_dir)["PLAYWRIGHT_BROWSERS_PATH"] == str(
        browser_dir
    )


def test_playwright_install_activates_only_complete_staging(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    def fake_popen(*_args, **kwargs):
        staging = Path(kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"])
        _make_browser_payload(staging)
        return _FakeProcess()

    monkeypatch.setattr(runtime_components.subprocess, "Popen", fake_popen)

    target = install_playwright_browsers(data_dir)

    assert target == managed_browsers_dir(data_dir)
    assert playwright_installed(data_dir)
    assert not any((data_dir / "runtime" / ".staging").iterdir())


def test_failed_playwright_repair_preserves_existing_runtime(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    target = managed_browsers_dir(data_dir)
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("existing", encoding="utf-8")

    monkeypatch.setattr(
        runtime_components.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeProcess(return_code=1),
    )

    with pytest.raises(RuntimeError, match="exited with code 1"):
        install_playwright_browsers(data_dir)

    assert (target / "keep.txt").read_text(encoding="utf-8") == "existing"
