import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools import smoke_windows_installer
from tools.installer_manifest import MANIFEST_NAME, read_installer_manifest
from tools.smoke_windows_installer import (
    InstallerSmokeFailure,
    _missing_installed_managed_files,
    _prepare_payload,
    _run_hidden,
    _verify_and_smoke_installed_application,
    isolated_environment,
    RegistryEntry,
    run_built_production_installer_smoke,
    validate_scratch_parent,
)
from automadin.runtime import INSTALLER_APP_MUTEX_NAME


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_installer_script_has_isolated_mode_and_managed_upgrade_contract() -> None:
    script = (PROJECT_ROOT / "packaging" / "Automadin.iss").read_text(
        encoding="utf-8"
    )

    assert "Inno Setup 6.7 or newer is required" in script
    assert "RedirectionGuard=yes" in script
    assert "UsePreviousAppDir=yes" in script
    assert f"AppMutex={INSTALLER_APP_MUTEX_NAME}" in script
    assert "AppMutex=Local\\Automadin.InstallerSmoke.{#MyAppId}" in script
    assert "CloseApplications=yes" in script
    assert "RestartApplications=no" in script
    assert "Check: FileExists(ExpandConstant(" in script
    assert "UninstallLogMode=overwrite" in script
    assert "InstallerTestMode requires an isolated MyAppId" in script
    assert "InstallerTestMode requires an explicit scratch" in script
    assert "#ifndef InstallerTestMode" in script
    assert "CachePreviousInstallManifest" in script
    assert "CompareManagedPathsOrdinal" in script
    assert "CompareText(Previous, Normalized)" not in script
    assert script.count("CompareText(") == 1
    assert "Comparison := CompareText" not in script
    assert "PrepareToInstall" in script
    assert "CurStep = ssPostInstall" in script
    assert "RemoveObsoleteManagedFiles" in script
    assert "preserved without current-version ownership" in script
    assert "InstalledManifestSHA256" in script
    assert "GetSHA256OfFile" in script
    assert "FileAttributeReparsePoint" in script
    assert "PathContainsReparsePoint" in script
    assert "FindPathShapeConflict" in script
    assert "GetPreviousData('InstalledAppVersion'" in script
    assert "ComparePackedVersion" in script
    assert 'Name: "{app}\\*"' not in script
    assert "filesandordirs" not in script


def test_validate_scratch_parent_rejects_missing_and_files(tmp_path: Path) -> None:
    assert validate_scratch_parent(tmp_path) == tmp_path.resolve()
    with pytest.raises(InstallerSmokeFailure, match="does not exist"):
        validate_scratch_parent(tmp_path / "missing")
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(InstallerSmokeFailure, match="not a directory"):
        validate_scratch_parent(file)


def test_isolated_environment_redirects_all_mutable_locations(
    tmp_path: Path,
) -> None:
    environment = isolated_environment(tmp_path)

    for variable in ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
        location = Path(environment[variable])
        assert location.is_dir()
        assert location.resolve().is_relative_to(tmp_path.resolve())


def test_prepare_payload_writes_exact_installer_manifest(tmp_path: Path) -> None:
    payload = _prepare_payload(
        tmp_path / "payload",
        {
            "Automadin.exe": b"fixture",
            "nested/runtime.dll": b"runtime",
        },
    )

    assert read_installer_manifest(payload / MANIFEST_NAME) == (
        "Automadin.exe",
        MANIFEST_NAME,
        "nested/runtime.dll",
    )


def test_missing_installed_managed_files_rejects_absent_and_linked_payload(
    tmp_path: Path,
) -> None:
    (tmp_path / "Automadin.exe").write_bytes(b"fixture")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "present.dll").write_bytes(b"present")
    linked = tmp_path / "nested" / "linked.dll"
    try:
        linked.symlink_to(tmp_path / "Automadin.exe")
    except OSError:
        linked = None

    managed_files = (
        "Automadin.exe",
        "nested/present.dll",
        "nested/missing.dll",
        *(("nested/linked.dll",) if linked is not None else ()),
    )

    expected = (
        "nested/missing.dll",
        *(("nested/linked.dll",) if linked is not None else ()),
    )
    assert _missing_installed_managed_files(tmp_path, managed_files) == expected


def test_hidden_command_runs_inside_managed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Process:
        @staticmethod
        def wait(timeout: float) -> int:
            events.append(f"wait:{timeout:g}")
            return 0

    class _Managed:
        process = _Process()

        @staticmethod
        def active_processes() -> int:
            events.append("active")
            return 0

    @contextmanager
    def fake_managed(command: list[str], environment: dict[str, str]):
        events.append(f"start:{command[0]}:{environment['TEST']}")
        try:
            yield _Managed()
        finally:
            events.append("cleanup")

    monkeypatch.setattr(smoke_windows_installer, "managed_process", fake_managed)

    assert _run_hidden(
        ["ISCC.exe"],
        {"TEST": "isolated"},
        timeout=3,
        description="compiler",
    ) == 0
    assert events == [
        "start:ISCC.exe:isolated",
        "wait:3",
        "active",
        "cleanup",
    ]


def test_hidden_command_timeout_still_closes_managed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Process:
        @staticmethod
        def wait(timeout: float) -> int:
            raise subprocess.TimeoutExpired("setup.exe", timeout)

    class _Managed:
        process = _Process()

    @contextmanager
    def fake_managed(_command: list[str], _environment: dict[str, str]):
        try:
            yield _Managed()
        finally:
            events.append("cleanup")

    monkeypatch.setattr(smoke_windows_installer, "managed_process", fake_managed)

    with pytest.raises(InstallerSmokeFailure, match="did not exit"):
        _run_hidden(
            ["setup.exe"],
            {},
            timeout=1,
            description="setup",
        )
    assert events == ["cleanup"]


def test_production_installer_mode_is_restricted_to_github_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smoke_windows_installer.sys, "platform", "win32")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    with pytest.raises(InstallerSmokeFailure, match="restricted to GitHub Actions"):
        run_built_production_installer_smoke(
            tmp_path / "installer.exe", tmp_path
        )


def test_production_installer_mode_refuses_existing_production_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"fixture")
    monkeypatch.setattr(smoke_windows_installer.sys, "platform", "win32")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(
        smoke_windows_installer,
        "_uninstall_entries",
        lambda _app_id: (object(),),
    )

    with pytest.raises(InstallerSmokeFailure, match="already exists"):
        run_built_production_installer_smoke(installer, tmp_path)


def test_installed_application_smoke_rejects_registry_executable_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = RegistryEntry(0, "key", "Automadin", "0.2.5", "uninstall.exe")
    launched: list[object] = []
    monkeypatch.setattr(
        smoke_windows_installer,
        "_installed_executable_versions",
        lambda _executable: ((0, 2, 4, 0), (0, 2, 5, 0)),
    )
    monkeypatch.setattr(
        smoke_windows_installer,
        "smoke_main_application",
        lambda *args: launched.append(args),
    )

    with pytest.raises(InstallerSmokeFailure, match="FileVersion.*does not match"):
        _verify_and_smoke_installed_application(
            entry,
            tmp_path / "Automadin.exe",
            tmp_path / "data",
            {"TEST": "isolated"},
            12,
        )
    assert launched == []


def test_installed_application_smoke_launches_exact_verified_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "installed" / "Automadin.exe"
    data_dir = tmp_path / "isolated-data"
    environment = {"TEST": "isolated"}
    entry = RegistryEntry(0, "key", "Automadin", "0.2.5", "uninstall.exe")
    launched: list[tuple[Path, Path, dict[str, str], float]] = []
    monkeypatch.setattr(
        smoke_windows_installer,
        "_installed_executable_versions",
        lambda _executable: ((0, 2, 5, 0), (0, 2, 5, 0)),
    )
    monkeypatch.setattr(
        smoke_windows_installer,
        "smoke_main_application",
        lambda *args: launched.append(args),
    )

    _verify_and_smoke_installed_application(
        entry,
        executable,
        data_dir,
        environment,
        12,
    )

    assert launched == [(executable, data_dir, environment, 12)]
