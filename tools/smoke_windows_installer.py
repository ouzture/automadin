"""Exercise isolated Inno Setup install, upgrade, and uninstall behavior.

The smoke installer uses a unique non-production AppId and display name, creates
no shortcuts, never launches Automadin, and installs only tiny synthetic files
under a caller-owned scratch directory.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

try:
    from tools.installer_manifest import (
        MANIFEST_NAME,
        read_installer_manifest,
        write_installer_manifest,
    )
except ModuleNotFoundError:  # Direct execution places tools/ on sys.path.
    from installer_manifest import (  # type: ignore[no-redef]
        MANIFEST_NAME,
        read_installer_manifest,
        write_installer_manifest,
    )
try:
    from tools.smoke_windows_release import managed_process, smoke_main_application
except ModuleNotFoundError:  # Direct execution places tools/ on sys.path.
    from smoke_windows_release import (  # type: ignore[no-redef]
        managed_process,
        smoke_main_application,
    )


DEFAULT_TIMEOUT_SECONDS = 60.0
PRODUCTION_APP_ID = "{14E54D2D-A7E4-4519-859B-AB98812DC5B3}"
UNINSTALL_REGISTRY_ROOT = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
)
_T = TypeVar("_T")


class InstallerSmokeFailure(RuntimeError):
    """The isolated installer lifecycle violated its safety contract."""


@dataclass(frozen=True)
class RegistryEntry:
    """One exact uninstall entry found in a Windows registry view."""

    view: int
    key_name: str
    display_name: str
    display_version: str
    uninstall_string: str


def validate_scratch_parent(path: Path) -> Path:
    """Require a safe existing directory which the smoke must restore exactly."""
    if not path.exists():
        raise InstallerSmokeFailure(f"Scratch parent does not exist: {path}")
    if not path.is_dir():
        raise InstallerSmokeFailure(f"Scratch parent is not a directory: {path}")
    if path.is_symlink():
        raise InstallerSmokeFailure(
            f"Scratch parent must not be a symbolic link: {path}"
        )
    resolved = path.resolve(strict=True)
    if resolved == Path(resolved.anchor):
        raise InstallerSmokeFailure("A filesystem root cannot be the scratch parent")
    if resolved == Path.home().resolve():
        raise InstallerSmokeFailure("The user home directory cannot be the scratch parent")
    return resolved


def snapshot_tree(root: Path) -> tuple[tuple[str, str, int, int], ...]:
    """Capture pre-existing scratch content so the harness can prove restoration."""
    records: list[tuple[str, str, int, int]] = []
    for path in sorted(root.rglob("*"), key=lambda value: str(value).casefold()):
        information = path.lstat()
        kind = "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"
        records.append(
            (
                path.relative_to(root).as_posix(),
                kind,
                information.st_size if kind == "file" else 0,
                information.st_mtime_ns,
            )
        )
    return tuple(records)


def _missing_installed_managed_files(
    install_dir: Path,
    managed_files: tuple[str, ...],
) -> tuple[str, ...]:
    """Return manifest-owned payload entries that are not regular installed files."""
    missing: list[str] = []
    for relative in managed_files:
        installed_path = install_dir / Path(*relative.split("/"))
        if installed_path.is_symlink() or not installed_path.is_file():
            missing.append(relative)
    return tuple(missing)


def _normalize_windows_version(value: str, description: str) -> tuple[int, int, int, int]:
    """Return a one-to-four-component numeric Windows version."""
    components = value.strip().split(".")
    if (
        not 1 <= len(components) <= 4
        or any(not component.isascii() or not component.isdigit() for component in components)
    ):
        raise InstallerSmokeFailure(
            f"{description} is not a numeric Windows version: {value!r}"
        )
    normalized = tuple(int(component) for component in components)
    return (*normalized, *(0 for _ in range(4 - len(normalized))))


def _installed_executable_versions(
    executable: Path,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Read the fixed FileVersion and ProductVersion from a Windows executable."""
    try:
        import pywintypes
        import win32api
    except ImportError as exc:
        raise InstallerSmokeFailure(
            f"Could not load Windows version metadata support: {exc}"
        ) from exc
    try:
        version_info = win32api.GetFileVersionInfo(str(executable), "\\")
        file_version = (
            version_info["FileVersionMS"] >> 16,
            version_info["FileVersionMS"] & 0xFFFF,
            version_info["FileVersionLS"] >> 16,
            version_info["FileVersionLS"] & 0xFFFF,
        )
        product_version = (
            version_info["ProductVersionMS"] >> 16,
            version_info["ProductVersionMS"] & 0xFFFF,
            version_info["ProductVersionLS"] >> 16,
            version_info["ProductVersionLS"] & 0xFFFF,
        )
    except (OSError, pywintypes.error, KeyError, TypeError) as exc:
        raise InstallerSmokeFailure(
            f"Could not read installed Automadin.exe version metadata: {exc}"
        ) from exc
    return file_version, product_version


def _verify_and_smoke_installed_application(
    entry: RegistryEntry,
    executable: Path,
    data_dir: Path,
    environment: dict[str, str],
    timeout: float,
) -> None:
    """Verify the installed identity, then exercise the exact installed executable."""
    registry_version = _normalize_windows_version(
        entry.display_version, "Installed registry DisplayVersion"
    )
    file_version, product_version = _installed_executable_versions(executable)
    for description, executable_version in (
        ("FileVersion", file_version),
        ("ProductVersion", product_version),
    ):
        if executable_version != registry_version:
            raise InstallerSmokeFailure(
                "Installed Automadin.exe "
                f"{description} {'.'.join(map(str, executable_version))} does not "
                "match registry DisplayVersion "
                f"{'.'.join(map(str, registry_version))}"
            )
    smoke_main_application(executable, data_dir, environment, timeout)


def isolated_environment(root: Path) -> dict[str, str]:
    """Redirect setup process environment locations into the smoke session."""
    environment = os.environ.copy()
    for variable, relative in (
        ("LOCALAPPDATA", "local-app-data"),
        ("APPDATA", "roaming-app-data"),
        ("TEMP", "temp"),
        ("TMP", "temp"),
    ):
        location = root / relative
        location.mkdir(parents=True, exist_ok=True)
        environment[variable] = str(location)
    return environment


def _wait_for(
    probe: Callable[[], _T | None],
    *,
    timeout: float,
    description: str,
) -> _T:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except BaseException as exc:
            last_error = exc
        time.sleep(0.1)
    detail = f"; last error: {last_error}" if last_error else ""
    raise InstallerSmokeFailure(f"Timed out waiting for {description}{detail}")


def _run_hidden(
    command: list[str],
    environment: dict[str, str],
    *,
    timeout: float,
    description: str,
) -> int:
    try:
        with managed_process(command, environment) as child:
            assert child.process is not None
            try:
                exit_code = child.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise InstallerSmokeFailure(
                    f"{description} did not exit within {timeout:g}s"
                ) from exc
            _wait_for(
                lambda: True if child.active_processes() == 0 else None,
                timeout=timeout,
                description=f"all contained processes for {description} to exit",
            )
    except OSError as exc:
        raise InstallerSmokeFailure(f"{description} could not complete: {exc}") from exc
    return exit_code


def _registry_views() -> tuple[int, ...]:
    import winreg

    return (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)


def _uninstall_entries(app_id: str) -> tuple[RegistryEntry, ...]:
    import winreg

    matches: list[RegistryEntry] = []
    key_name = f"{app_id}_is1"
    for view in _registry_views():
        try:
            entry = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                f"{UNINSTALL_REGISTRY_ROOT}\\{key_name}",
                0,
                winreg.KEY_READ | view,
            )
        except FileNotFoundError:
            continue
        with entry:
            display_name, _ = winreg.QueryValueEx(entry, "DisplayName")
            display_version, _ = winreg.QueryValueEx(entry, "DisplayVersion")
            uninstall_string, _ = winreg.QueryValueEx(entry, "UninstallString")
        candidate = RegistryEntry(
            view=view,
            key_name=key_name,
            display_name=str(display_name),
            display_version=str(display_version),
            uninstall_string=str(uninstall_string),
        )
        if any(
            prior.key_name == candidate.key_name
            and prior.display_name == candidate.display_name
            and prior.display_version == candidate.display_version
            and prior.uninstall_string == candidate.uninstall_string
            for prior in matches
        ):
            continue
        matches.append(candidate)
    return tuple(matches)


def _snapshot_production_uninstall_entries() -> tuple[tuple[int, str, object, int], ...]:
    """Capture all values from only the production AppId registry entry."""
    import winreg

    key_name = f"{PRODUCTION_APP_ID}_is1"
    records: list[tuple[int, str, object, int]] = []
    for view in _registry_views():
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                f"{UNINSTALL_REGISTRY_ROOT}\\{key_name}",
                0,
                winreg.KEY_READ | view,
            )
        except FileNotFoundError:
            continue
        with key:
            index = 0
            while True:
                try:
                    name, value, value_type = winreg.EnumValue(key, index)
                except OSError:
                    break
                records.append((view, name, value, value_type))
                index += 1
    return tuple(sorted(records, key=lambda record: (record[0], record[1].casefold())))


def _delete_exact_test_entries(app_id: str) -> None:
    """Remove only smoke-created uninstall keys during failure recovery."""
    import winreg

    key_path = f"{UNINSTALL_REGISTRY_ROOT}\\{app_id}_is1"
    for view in _registry_views():
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, key_path, view, 0)
        except FileNotFoundError:
            pass


def _prepare_payload(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=False)
    for relative, content in files.items():
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    write_installer_manifest(root)
    return root


def _tamper_manifest_with_path(manifest: Path, relative: str) -> None:
    entries = [*read_installer_manifest(manifest), relative]
    entries.sort(key=lambda value: (value.lower(), value))
    content = "\r\n".join(
        ("AUTOMADIN-INSTALL-MANIFEST-V1", *entries, "")
    )
    manifest.write_text(content, encoding="utf-8-sig", newline="")


def _create_directory_junction(
    link: Path,
    target: Path,
    environment: dict[str, str],
    timeout: float,
) -> None:
    exit_code = _run_hidden(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        environment,
        timeout=timeout,
        description="test directory-junction creation",
    )
    if exit_code != 0 or not link.is_dir():
        raise InstallerSmokeFailure(
            f"Could not create the test directory junction {link} -> {target}"
        )


def _compile_test_installer(
    compiler: Path,
    script: Path,
    source: Path,
    output_dir: Path,
    *,
    version: str,
    app_id: str,
    app_name: str,
    output_name: str,
    environment: dict[str, str],
    timeout: float,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=False)
    command = [
        str(compiler),
        f"/DMyAppVersion={version}",
        f"/DSourceDir={source}",
        f"/DMyAppId={app_id}",
        f"/DMyAppName={app_name}",
        "/DInstallerTestMode=1",
        f"/DInstallerTestDefaultDirName={source.parent / 'installed-app'}",
        f"/DInstallerOutputDir={output_dir}",
        f"/DInstallerOutputBaseFilename={output_name}",
        str(script),
    ]
    exit_code = _run_hidden(
        command,
        environment,
        timeout=timeout,
        description=f"Inno Setup compilation for {version}",
    )
    if exit_code != 0:
        raise InstallerSmokeFailure(
            f"Inno Setup compilation for {version} exited with code {exit_code}"
        )
    installer = output_dir / f"{output_name}.exe"
    if not installer.is_file():
        raise InstallerSmokeFailure(f"Compiler did not produce {installer}")
    return installer


def _run_setup(
    installer: Path,
    install_dir: Path,
    log_path: Path,
    environment: dict[str, str],
    timeout: float,
    *,
    expect_success: bool = True,
) -> int:
    exit_code = _run_hidden(
        [
            str(installer),
            "/CURRENTUSER",
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            f"/DIR={install_dir}",
            f"/LOG={log_path}",
        ],
        environment,
        timeout=timeout,
        description=f"installer {installer.name}",
    )
    if expect_success and exit_code != 0:
        raise InstallerSmokeFailure(
            f"installer {installer.name} exited with code {exit_code}"
        )
    if not expect_success and exit_code == 0:
        raise InstallerSmokeFailure(
            f"installer {installer.name} unexpectedly accepted a downgrade"
        )
    return exit_code


def _find_uninstaller(install_dir: Path) -> Path | None:
    candidates = tuple(sorted(install_dir.glob("unins*.exe")))
    if len(candidates) > 1:
        raise InstallerSmokeFailure(
            f"Expected one uninstaller, found {len(candidates)} in {install_dir}"
        )
    return candidates[0] if candidates else None


def _assert_single_uninstaller_generation(install_dir: Path) -> None:
    artifacts = tuple(
        path
        for path in install_dir.glob("unins*.*")
        if path.suffix.lower() in {".exe", ".dat", ".msg"}
    )
    executable_stems = {
        path.stem.lower() for path in artifacts if path.suffix.lower() == ".exe"
    }
    artifact_stems = {path.stem.lower() for path in artifacts}
    if len(executable_stems) != 1 or artifact_stems != executable_stems:
        raise InstallerSmokeFailure(
            "Upgrade left multiple or mismatched uninstaller generations: "
            + ", ".join(path.name for path in artifacts)
        )


def _run_uninstaller(
    uninstaller: Path,
    environment: dict[str, str],
    timeout: float,
) -> None:
    exit_code = _run_hidden(
        [
            str(uninstaller),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
        ],
        environment,
        timeout=timeout,
        description="test uninstaller",
    )
    if exit_code != 0:
        raise InstallerSmokeFailure(
            f"test uninstaller exited with code {exit_code}"
        )


def _remove_tree(path: Path) -> None:
    def retry_readonly(function: Callable[..., object], target: str, _: object) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    deadline = time.monotonic() + 10.0
    while True:
        try:
            shutil.rmtree(path, onerror=retry_readonly)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def run_installer_smoke(
    inno_compiler: Path,
    installer_script: Path,
    scratch_parent: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Run a synthetic two-version lifecycle without production app identity."""
    if sys.platform != "win32":
        raise InstallerSmokeFailure("The installer smoke must run on Windows")
    if timeout <= 0:
        raise InstallerSmokeFailure("Timeout must be greater than zero")

    compiler = inno_compiler.resolve(strict=True)
    script = installer_script.resolve(strict=True)
    if not compiler.is_file():
        raise InstallerSmokeFailure(f"Inno Setup compiler is not a file: {compiler}")
    if not script.is_file():
        raise InstallerSmokeFailure(f"Installer script is not a file: {script}")
    scratch = validate_scratch_parent(scratch_parent)
    before = snapshot_tree(scratch)
    production_registry_before = _snapshot_production_uninstall_entries()

    token = uuid.uuid4().hex
    app_id = f"Automadin.InstallerSmoke.{token}"
    app_name = f"Automadin Installer Smoke {token}"
    session = Path(tempfile.mkdtemp(prefix="automadin-installer-smoke-", dir=scratch))
    environment = isolated_environment(session / "environment")
    install_dir = session / "installed-app"
    external_data = session / "user-data"
    external_data.mkdir()
    external_sentinel = external_data / "keep.txt"
    external_sentinel.write_bytes(b"external user data")
    installed = False
    uninstalled = False
    failure: BaseException | None = None
    try:
        source_v1 = _prepare_payload(
            session / "source-v1",
            {
                "Automadin.exe": b"synthetic Automadin v1",
                "_asyncio.pyd": b"underscore runtime v1",
                "runtime-code/common.dll": b"common v1",
                "obsolete/legacy.dll": b"remove on upgrade",
                "junction-obsolete/legacy.dll": b"must not escape app root",
            },
        )
        source_v2 = _prepare_payload(
            session / "source-v2",
            {
                "Automadin.exe": b"synthetic Automadin v2",
                "_asyncio.pyd": b"underscore runtime v2",
                "runtime-code/common.dll": b"common v2",
                "runtime-code/current.dll": b"new in v2",
            },
        )
        source_v3 = _prepare_payload(
            session / "source-v3",
            {
                "Automadin.exe": b"synthetic Automadin v3",
                "_asyncio.pyd": b"underscore runtime v3",
                "runtime-code/common.dll": b"common v3",
                "runtime-code/current.dll": b"current v3",
            },
        )
        source_shape_conflict = _prepare_payload(
            session / "source-shape-conflict",
            {
                "Automadin.exe": b"synthetic Automadin shape conflict",
                "obsolete/legacy.dll/child.dll": b"file-directory conflict",
            },
        )
        installer_v1 = _compile_test_installer(
            compiler,
            script,
            source_v1,
            session / "installer-v1",
            version="1.0.0",
            app_id=app_id,
            app_name=app_name,
            output_name="Automadin-Smoke-v1",
            environment=environment,
            timeout=timeout,
        )
        installer_v2 = _compile_test_installer(
            compiler,
            script,
            source_v2,
            session / "installer-v2",
            version="2.0.0",
            app_id=app_id,
            app_name=app_name,
            output_name="Automadin-Smoke-v2",
            environment=environment,
            timeout=timeout,
        )
        installer_v3 = _compile_test_installer(
            compiler,
            script,
            source_v3,
            session / "installer-v3",
            version="3.0.0",
            app_id=app_id,
            app_name=app_name,
            output_name="Automadin-Smoke-v3",
            environment=environment,
            timeout=timeout,
        )
        installer_shape_conflict = _compile_test_installer(
            compiler,
            script,
            source_shape_conflict,
            session / "installer-shape-conflict",
            version="1.5.0",
            app_id=app_id,
            app_name=app_name,
            output_name="Automadin-Smoke-shape-conflict",
            environment=environment,
            timeout=timeout,
        )

        _run_setup(
            installer_v1,
            install_dir,
            session / "install-v1.log",
            environment,
            timeout,
        )
        installed = True
        entries = _wait_for(
            lambda: _uninstall_entries(app_id) or None,
            timeout=timeout,
            description="the isolated v1 uninstall registration",
        )
        if len(entries) != 1:
            raise InstallerSmokeFailure(
                f"Expected one isolated uninstall registration, found {len(entries)}"
            )
        if entries[0].key_name != f"{app_id}_is1":
            raise InstallerSmokeFailure("Installer did not use the isolated test AppId")
        if (
            not entries[0].display_name.startswith(app_name)
            or entries[0].display_version != "1.0.0"
        ):
            raise InstallerSmokeFailure(
                f"The v1 uninstall metadata is incorrect: {entries[0]!r}"
            )
        if str(install_dir).casefold() not in entries[0].uninstall_string.casefold():
            raise InstallerSmokeFailure("The v1 uninstaller is outside the scratch install")
        if (install_dir / "obsolete" / "legacy.dll").read_bytes() != b"remove on upgrade":
            raise InstallerSmokeFailure("The v1 obsolete-file fixture was not installed")

        protected_files = {
            install_dir / "data" / "keep.txt": b"app-local data",
            install_dir / "profiles" / "keep.txt": b"app-local profile",
            install_dir / "runtime" / "keep.txt": b"app-local runtime",
            install_dir / "automadin.db": b"app-local database",
            install_dir / "user-note.txt": b"unknown user file",
        }
        for path, content in protected_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        junction_external = external_data / "junction-target"
        junction_external.mkdir()
        junction_external_file = junction_external / "legacy.dll"
        junction_external_file.write_bytes(b"external junction data")
        junction_parent = install_dir / "junction-obsolete"
        (junction_parent / "legacy.dll").unlink()
        junction_parent.rmdir()
        _create_directory_junction(
            junction_parent, junction_external, environment, timeout
        )

        before_shape_rejection = {
            path: path.read_bytes()
            for path in (
                install_dir / "Automadin.exe",
                install_dir / "obsolete" / "legacy.dll",
                install_dir / MANIFEST_NAME,
                *protected_files,
            )
        }
        _run_setup(
            installer_shape_conflict,
            install_dir,
            session / "rejected-shape-conflict.log",
            environment,
            timeout,
            expect_success=False,
        )
        for path, content in before_shape_rejection.items():
            if path.read_bytes() != content:
                raise InstallerSmokeFailure(
                    f"Rejected file-directory transition changed state: {path}"
                )

        _run_setup(
            installer_v2,
            install_dir,
            session / "install-v2.log",
            environment,
            timeout,
        )
        if (install_dir / "obsolete" / "legacy.dll").exists():
            raise InstallerSmokeFailure("Upgrade retained an obsolete managed file")
        if (install_dir / "obsolete").exists():
            raise InstallerSmokeFailure("Upgrade retained an empty obsolete directory")
        if (install_dir / "runtime-code" / "common.dll").read_bytes() != b"common v2":
            raise InstallerSmokeFailure("Upgrade did not replace the common payload")
        if (install_dir / "_asyncio.pyd").read_bytes() != b"underscore runtime v2":
            raise InstallerSmokeFailure(
                "Upgrade cleanup removed or retained a stale underscore-prefixed payload"
            )
        if (install_dir / "runtime-code" / "current.dll").read_bytes() != b"new in v2":
            raise InstallerSmokeFailure("Upgrade did not add the current payload")
        if (install_dir / "install-files.next").exists():
            raise InstallerSmokeFailure("Incoming managed-file manifest was not activated")
        if tuple(read_installer_manifest(install_dir / MANIFEST_NAME)) != tuple(
            read_installer_manifest(source_v2 / MANIFEST_NAME)
        ):
            raise InstallerSmokeFailure("Installed managed-file manifest is not v2")
        for path, content in protected_files.items():
            if path.read_bytes() != content:
                raise InstallerSmokeFailure(f"Upgrade changed protected data: {path}")
        if external_sentinel.read_bytes() != b"external user data":
            raise InstallerSmokeFailure("Upgrade changed external user data")
        if junction_external_file.read_bytes() != b"external junction data":
            raise InstallerSmokeFailure(
                "Upgrade followed a reparse-point parent outside the app root"
            )
        if not junction_parent.exists():
            raise InstallerSmokeFailure(
                "Upgrade removed the reparse-point fixture instead of preserving it"
            )
        upgraded_entries = _uninstall_entries(app_id)
        if len(upgraded_entries) != 1:
            raise InstallerSmokeFailure("Upgrade did not retain exactly one test identity")
        if upgraded_entries[0].display_version != "2.0.0":
            raise InstallerSmokeFailure("Upgrade did not publish the v2 display version")
        if str(install_dir).casefold() not in upgraded_entries[0].uninstall_string.casefold():
            raise InstallerSmokeFailure("The upgraded uninstaller is outside scratch")
        _assert_single_uninstaller_generation(install_dir)

        before_downgrade = {
            path: path.read_bytes()
            for path in (
                install_dir / "Automadin.exe",
                install_dir / "runtime-code" / "common.dll",
                install_dir / "runtime-code" / "current.dll",
                install_dir / MANIFEST_NAME,
                *protected_files,
            )
        }
        _run_setup(
            installer_v1,
            install_dir,
            session / "rejected-downgrade.log",
            environment,
            timeout,
            expect_success=False,
        )
        for path, content in before_downgrade.items():
            if path.read_bytes() != content:
                raise InstallerSmokeFailure(
                    f"Rejected downgrade changed installed state: {path}"
                )
        rejected_entries = _uninstall_entries(app_id)
        if len(rejected_entries) != 1 or rejected_entries[0].display_version != "2.0.0":
            raise InstallerSmokeFailure(
                "Rejected downgrade changed the uninstall registration"
            )

        os.rmdir(junction_parent)
        recreated_obsolete = install_dir / "obsolete" / "legacy.dll"
        recreated_obsolete.parent.mkdir()
        recreated_obsolete.write_bytes(b"user recreated obsolete path")
        _tamper_manifest_with_path(
            install_dir / MANIFEST_NAME, "user-note.txt"
        )
        _run_setup(
            installer_v3,
            install_dir,
            session / "install-v3-after-manifest-tamper.log",
            environment,
            timeout,
        )
        if recreated_obsolete.read_bytes() != b"user recreated obsolete path":
            raise InstallerSmokeFailure(
                "Manifest integrity mismatch did not fail preserve"
            )
        if protected_files[install_dir / "user-note.txt"] != (
            install_dir / "user-note.txt"
        ).read_bytes():
            raise InstallerSmokeFailure(
                "Tampered manifest authorized deletion of an unknown user file"
            )
        if tuple(read_installer_manifest(install_dir / MANIFEST_NAME)) != tuple(
            read_installer_manifest(source_v3 / MANIFEST_NAME)
        ):
            raise InstallerSmokeFailure("Installed managed-file manifest is not v3")
        _assert_single_uninstaller_generation(install_dir)

        uninstaller = _find_uninstaller(install_dir)
        if uninstaller is None:
            raise InstallerSmokeFailure("Upgrade did not leave an uninstaller")
        _run_uninstaller(uninstaller, environment, timeout)
        _wait_for(
            lambda: True if not _uninstall_entries(app_id) else None,
            timeout=timeout,
            description="the isolated uninstall registration to disappear",
        )
        _wait_for(
            lambda: True if _find_uninstaller(install_dir) is None else None,
            timeout=timeout,
            description="the isolated uninstaller clone to finish cleanup",
        )
        uninstalled = True
        if tuple(install_dir.glob("unins*.*")):
            raise InstallerSmokeFailure("Uninstall retained an uninstaller artifact")
        for relative in (
            "Automadin.exe",
            "runtime-code/common.dll",
            "runtime-code/current.dll",
            MANIFEST_NAME,
            "install-files.next",
        ):
            if (install_dir / relative).exists():
                raise InstallerSmokeFailure(
                    f"Uninstall retained installer-managed payload: {relative}"
                )
        for path, content in protected_files.items():
            if path.read_bytes() != content:
                raise InstallerSmokeFailure(f"Uninstall changed protected data: {path}")
        if external_sentinel.read_bytes() != b"external user data":
            raise InstallerSmokeFailure("Uninstall changed external user data")
        if recreated_obsolete.read_bytes() != b"user recreated obsolete path":
            raise InstallerSmokeFailure(
                "Current-version uninstall ownership removed a recreated obsolete path"
            )
        if junction_external_file.read_bytes() != b"external junction data":
            raise InstallerSmokeFailure("Uninstall changed external junction data")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        cleanup_errors: list[str] = []
        try:
            if installed and not uninstalled:
                uninstaller = _find_uninstaller(install_dir)
                if uninstaller is not None:
                    _run_uninstaller(uninstaller, environment, min(timeout, 30.0))
        except BaseException as exc:
            cleanup_errors.append(f"test uninstaller cleanup failed: {exc}")
        try:
            _delete_exact_test_entries(app_id)
        except BaseException as exc:
            cleanup_errors.append(f"test registry cleanup failed: {exc}")
        try:
            if _uninstall_entries(app_id):
                cleanup_errors.append("test uninstall registry identity remains")
        except BaseException as exc:
            cleanup_errors.append(f"test registry verification failed: {exc}")
        if _snapshot_production_uninstall_entries() != production_registry_before:
            cleanup_errors.append("production Automadin uninstall identity changed")
        try:
            _remove_tree(session)
            if snapshot_tree(scratch) != before:
                cleanup_errors.append("scratch parent was not restored exactly")
        except BaseException as exc:
            cleanup_errors.append(f"scratch cleanup failed: {exc}")
        if cleanup_errors:
            joined = "; ".join(cleanup_errors)
            if failure is None:
                raise InstallerSmokeFailure(joined)
            raise InstallerSmokeFailure(
                f"Installer smoke failed ({failure}); cleanup also failed: {joined}"
            ) from failure


def run_built_production_installer_smoke(
    installer: Path,
    scratch_parent: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Install the exact production artifact only on an empty GitHub Actions runner."""
    if sys.platform != "win32":
        raise InstallerSmokeFailure("The production installer smoke must run on Windows")
    if os.environ.get("CI", "").lower() != "true" or os.environ.get(
        "GITHUB_ACTIONS", ""
    ).lower() != "true":
        raise InstallerSmokeFailure(
            "The production installer smoke is restricted to GitHub Actions CI"
        )
    if timeout <= 0:
        raise InstallerSmokeFailure("Timeout must be greater than zero")
    exact_installer = installer.resolve(strict=True)
    if not exact_installer.is_file():
        raise InstallerSmokeFailure(
            f"Production installer is not a file: {exact_installer}"
        )
    if _uninstall_entries(PRODUCTION_APP_ID):
        raise InstallerSmokeFailure(
            "Refusing production installer smoke because the production uninstall "
            "identity already exists"
        )

    scratch = validate_scratch_parent(scratch_parent)
    before = snapshot_tree(scratch)
    session = Path(
        tempfile.mkdtemp(prefix="automadin-production-installer-smoke-", dir=scratch)
    )
    environment = isolated_environment(session / "environment")
    install_dir = session / "installed-app"
    uninstalled = False
    failure: BaseException | None = None
    try:
        _run_setup(
            exact_installer,
            install_dir,
            session / "production-install.log",
            environment,
            timeout,
        )
        entries = _wait_for(
            lambda: _uninstall_entries(PRODUCTION_APP_ID) or None,
            timeout=timeout,
            description="the production uninstall registration",
        )
        if len(entries) != 1:
            raise InstallerSmokeFailure(
                f"Expected one production uninstall registration, found {len(entries)}"
            )
        if str(install_dir).casefold() not in entries[0].uninstall_string.casefold():
            raise InstallerSmokeFailure(
                "Production uninstaller was registered outside the isolated install"
            )
        managed_files = read_installer_manifest(install_dir / MANIFEST_NAME)
        if "Automadin.exe" not in managed_files:
            raise InstallerSmokeFailure(
                "Production installer manifest does not own Automadin.exe"
            )
        missing_managed_files = _missing_installed_managed_files(
            install_dir,
            managed_files,
        )
        if missing_managed_files:
            raise InstallerSmokeFailure(
                "Production installer omitted managed files: "
                + ", ".join(missing_managed_files)
            )
        _verify_and_smoke_installed_application(
            entries[0],
            install_dir / "Automadin.exe",
            session / "installed-app-data",
            environment,
            timeout,
        )
        sentinel = install_dir / "user-note.txt"
        sentinel.write_bytes(b"production smoke user file")
        uninstaller = _find_uninstaller(install_dir)
        if uninstaller is None:
            raise InstallerSmokeFailure("Production installer left no uninstaller")
        _run_uninstaller(uninstaller, environment, timeout)
        _wait_for(
            lambda: True if not _uninstall_entries(PRODUCTION_APP_ID) else None,
            timeout=timeout,
            description="the production uninstall registration to disappear",
        )
        _wait_for(
            lambda: True if _find_uninstaller(install_dir) is None else None,
            timeout=timeout,
            description="the production uninstaller clone to finish cleanup",
        )
        uninstalled = True
        retained = [
            relative
            for relative in managed_files
            if (install_dir / Path(*relative.split("/"))).exists()
        ]
        if retained:
            raise InstallerSmokeFailure(
                f"Production uninstall retained managed files: {', '.join(retained)}"
            )
        if sentinel.read_bytes() != b"production smoke user file":
            raise InstallerSmokeFailure(
                "Production uninstall changed an unknown app-local user file"
            )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        cleanup_errors: list[str] = []
        try:
            if not uninstalled:
                entries = _uninstall_entries(PRODUCTION_APP_ID)
                if entries and all(
                    str(install_dir).casefold()
                    in entry.uninstall_string.casefold()
                    for entry in entries
                ):
                    uninstaller = _find_uninstaller(install_dir)
                    if uninstaller is not None:
                        _run_uninstaller(
                            uninstaller, environment, min(timeout, 30.0)
                        )
        except BaseException as exc:
            cleanup_errors.append(f"production uninstaller cleanup failed: {exc}")
        try:
            if _uninstall_entries(PRODUCTION_APP_ID):
                cleanup_errors.append(
                    "production uninstall registry identity remains; no raw registry "
                    "cleanup was attempted"
                )
        except BaseException as exc:
            cleanup_errors.append(
                f"production registry verification failed: {exc}"
            )
        if not cleanup_errors:
            try:
                _remove_tree(session)
                if snapshot_tree(scratch) != before:
                    cleanup_errors.append("scratch parent was not restored exactly")
            except BaseException as exc:
                cleanup_errors.append(f"scratch cleanup failed: {exc}")
        if cleanup_errors:
            joined = "; ".join(cleanup_errors)
            if failure is None:
                raise InstallerSmokeFailure(joined)
            raise InstallerSmokeFailure(
                f"Production installer smoke failed ({failure}); cleanup also failed: "
                f"{joined}"
            ) from failure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inno_compiler", type=Path, nargs="?")
    parser.add_argument("installer_script", type=Path, nargs="?")
    parser.add_argument("scratch_parent", type=Path, nargs="?")
    parser.add_argument("--built-production-installer", type=Path)
    parser.add_argument("--built-production-scratch", type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.built_production_installer is not None:
            if (
                args.built_production_scratch is None
                or args.inno_compiler is not None
                or args.installer_script is not None
                or args.scratch_parent is not None
            ):
                parser.error(
                    "--built-production-installer requires "
                    "--built-production-scratch and no positional arguments"
                )
            run_built_production_installer_smoke(
                args.built_production_installer,
                args.built_production_scratch,
                timeout=args.timeout,
            )
        else:
            if (
                args.inno_compiler is None
                or args.installer_script is None
                or args.scratch_parent is None
                or args.built_production_scratch is not None
            ):
                parser.error(
                    "isolated mode requires inno_compiler, installer_script, and "
                    "scratch_parent"
                )
            run_installer_smoke(
                args.inno_compiler,
                args.installer_script,
                args.scratch_parent,
                timeout=args.timeout,
            )
    except (OSError, InstallerSmokeFailure) as exc:
        print(f"WINDOWS INSTALLER SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    if args.built_production_installer is not None:
        print("Exact production installer lifecycle smoke passed.")
    else:
        print("Isolated Windows install, upgrade, and uninstall smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
