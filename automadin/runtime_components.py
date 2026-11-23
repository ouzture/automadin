"""Install and inspect optional runtimes managed by Automadin."""

from __future__ import annotations

import json
import msvcrt
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from playwright._impl._driver import compute_driver_executable
from PySide6.QtCore import QCoreApplication

from automadin.runtime import (
    BROWSER_ENGINES,
    application_dir,
    inspect_browser_payload,
    is_frozen,
    managed_browsers_dir,
    managed_browsers_ready,
    managed_runtime_dir,
)


ADB_DOWNLOAD_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
ADB_TERMS_URL = "https://developer.android.com/studio/terms"
ADB_REQUIRED_FILES = (
    "adb.exe",
    "AdbWinApi.dll",
    "AdbWinUsbApi.dll",
    "NOTICE.txt",
    "source.properties",
)
MAX_ADB_DOWNLOAD_BYTES = 250 * 1024 * 1024

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]


def _not_cancelled() -> bool:
    return False


def _replace_with_retry(source: Path, destination: Path, attempts: int = 20) -> None:
    """Atomically move a directory despite short-lived Windows scanner locks."""
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25)


@contextmanager
def runtime_install_lock(data_dir: str | Path):
    """Prevent two Runtime Manager processes from modifying runtimes together."""
    lock_path = managed_runtime_dir(data_dir) / ".install.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+b")
    try:
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(
                "Another Automadin Runtime Manager is already installing or repairing tools."
            ) from exc
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        lock_file.close()


def playwright_installed(data_dir: str | Path) -> bool:
    return managed_browsers_ready(data_dir)


def playwright_install_command() -> list[str]:
    node, cli = compute_driver_executable()
    return [str(node), str(cli), "install", *BROWSER_ENGINES]


def playwright_install_environment(data_dir: str | Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(managed_browsers_dir(data_dir))
    return env


def _install_playwright_browsers_unlocked(
    data_dir: str | Path,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback = _not_cancelled,
    process_started: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    """Install all supported browsers in staging and atomically activate them."""
    report = progress or (lambda _percent, _message: None)
    runtime_root = managed_runtime_dir(data_dir)
    staging = runtime_root / ".staging" / f"browsers-{uuid.uuid4().hex}"
    target = managed_browsers_dir(data_dir)
    backup = target.with_name(f"{target.name}.old-{uuid.uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(staging)
    process: subprocess.Popen | None = None
    target_moved = False
    try:
        report(0, "Starting Playwright browser installer...")
        process = subprocess.Popen(
            playwright_install_command(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process_started is not None:
            process_started(process)
        assert process.stdout is not None
        for line in process.stdout:
            if cancelled():
                process.terminate()
                raise RuntimeError("Browser installation cancelled.")
            message = line.strip()
            if message:
                report(0, message)
        return_code = process.wait()
        if cancelled():
            raise RuntimeError("Browser installation cancelled.")
        if return_code != 0:
            raise RuntimeError(f"Playwright installer exited with code {return_code}.")
        payload = inspect_browser_payload(staging)
        if not payload.usable:
            missing = ", ".join(payload.missing_engines) or "unknown payload"
            raise RuntimeError(
                "Playwright finished but one or more browser executables are missing: "
                f"{missing}."
            )

        manifest = {
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "engines": list(BROWSER_ENGINES),
        }
        (staging / "automadin-install.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _replace_with_retry(target, backup)
            target_moved = True
        _replace_with_retry(staging, target)
        shutil.rmtree(backup, ignore_errors=True)
        report(100, f"Browsers installed at {target}")
        return target
    except Exception:
        if target_moved and backup.exists() and not target.exists():
            _replace_with_retry(backup, target)
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
        shutil.rmtree(staging, ignore_errors=True)


def install_playwright_browsers(
    data_dir: str | Path,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback = _not_cancelled,
    process_started: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    with runtime_install_lock(data_dir):
        return _install_playwright_browsers_unlocked(
            data_dir,
            progress=progress,
            cancelled=cancelled,
            process_started=process_started,
        )


def _adb_version_from_properties(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^Pkg\.Revision=(.+)$", text, re.MULTILINE)
    if not match:
        raise RuntimeError("Downloaded Platform Tools package has no version metadata.")
    version = match.group(1).strip()
    if not re.fullmatch(r"[0-9A-Za-z._-]+", version):
        raise RuntimeError("Downloaded Platform Tools version is invalid.")
    return version


def _default_adb_verifier(adb_path: Path) -> str:
    result = subprocess.run(
        [str(adb_path), "version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip()


def _install_adb_unlocked(
    data_dir: str | Path,
    *,
    download_url: str = ADB_DOWNLOAD_URL,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback = _not_cancelled,
    verify: Callable[[Path], str] = _default_adb_verifier,
) -> Path:
    """Download ADB directly from Google and atomically activate it."""
    report = progress or (lambda _percent, _message: None)
    runtime_root = managed_runtime_dir(data_dir)
    staging_root = runtime_root / ".staging" / f"adb-{uuid.uuid4().hex}"
    archive_path = staging_root / "platform-tools.zip"
    extracted = staging_root / "extracted"
    platform_root = runtime_root / "platform-tools"
    backup: Path | None = None
    target_moved = False
    staging_root.mkdir(parents=True, exist_ok=False)

    try:
        report(0, "Connecting to Google...")
        request = urllib.request.Request(download_url, headers={"User-Agent": "Automadin/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response, archive_path.open("wb") as output:
            total = int(response.headers.get("Content-Length", "0") or 0)
            if total > MAX_ADB_DOWNLOAD_BYTES:
                raise RuntimeError("ADB download is unexpectedly large.")
            received = 0
            while True:
                if cancelled():
                    raise RuntimeError("ADB installation cancelled.")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                if received > MAX_ADB_DOWNLOAD_BYTES:
                    raise RuntimeError("ADB download exceeded the safety limit.")
                percent = min(85, int(received * 85 / total)) if total else 0
                report(percent, f"Downloading ADB ({received / 1024 / 1024:.1f} MiB)...")

        report(88, "Validating downloaded archive...")
        extracted.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            for filename in ADB_REQUIRED_FILES:
                member = f"platform-tools/{filename}"
                if member not in names:
                    raise RuntimeError(f"Downloaded ADB package is missing {filename}.")
                destination = extracted / filename
                with archive.open(member) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)

        version = _adb_version_from_properties(extracted / "source.properties")
        report(94, f"Verifying ADB {version}...")
        version_output = verify(extracted / "adb.exe")

        target = platform_root / version
        manifest = {
            "version": version,
            "source": download_url,
            "terms": ADB_TERMS_URL,
            "terms_accepted": True,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "verification": version_output,
        }
        # Write metadata while staged, then swap the verified directory into place.
        (extracted / "automadin-install.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        platform_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = target.with_name(f"{target.name}.old-{uuid.uuid4().hex}")
            _replace_with_retry(target, backup)
            target_moved = True
        try:
            _replace_with_retry(extracted, target)
        except Exception:
            if target_moved and backup is not None and backup.exists() and not target.exists():
                _replace_with_retry(backup, target)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        current_temp = platform_root / "current.txt.tmp"
        current_temp.write_text(version, encoding="utf-8")
        os.replace(current_temp, platform_root / "current.txt")
        report(100, f"ADB {version} installed.")
        return target / "adb.exe"
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def install_adb(
    data_dir: str | Path,
    *,
    download_url: str = ADB_DOWNLOAD_URL,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback = _not_cancelled,
    verify: Callable[[Path], str] = _default_adb_verifier,
) -> Path:
    with runtime_install_lock(data_dir):
        return _install_adb_unlocked(
            data_dir,
            download_url=download_url,
            progress=progress,
            cancelled=cancelled,
            verify=verify,
        )


def _packaged_application_executable() -> Path | None:
    """Return the installed/portable Automadin executable when available.

    Qt owns the application process and provides the most reliable executable
    path in compiled builds.  Nuitka can expose a Python-oriented
    ``sys.executable`` value in some launch contexts, so keep it as the last
    candidate rather than treating it as authoritative.
    """
    candidates = (
        QCoreApplication.applicationFilePath(),
        sys.argv[0] if sys.argv else "",
        str(application_dir() / "Automadin.exe"),
        sys.executable,
    )
    for value in candidates:
        if not value:
            continue
        candidate = Path(value).resolve()
        if candidate.name.casefold() == "automadin.exe" and candidate.is_file():
            return candidate
    return None


def application_command(
    *arguments: str,
    source_executable: str | Path | None = None,
) -> list[str]:
    """Build an Automadin helper command for packaged and source modes."""
    packaged_executable = _packaged_application_executable()
    if packaged_executable is not None:
        return [str(packaged_executable), *arguments]
    if is_frozen():
        raise RuntimeError(
            "Automadin could not locate its installed executable. "
            "Please repair or reinstall the application."
        )

    executable = Path(source_executable or sys.executable).resolve()
    return [
        str(executable),
        str(application_dir() / "main.py"),
        *arguments,
    ]


def runtime_manager_command(data_dir: str | Path) -> list[str]:
    """Build the Runtime Manager command without changing its launch behavior."""
    executable = Path(sys.executable).resolve()
    pythonw = executable.with_name("pythonw.exe")
    return application_command(
        "--runtime-manager",
        "--data-dir",
        str(data_dir),
        source_executable=pythonw if pythonw.is_file() else executable,
    )


def browser_health_worker_command(target: str) -> list[str]:
    """Build the isolated browser-health worker command."""
    return application_command("--browser-health-worker", target)


def launch_runtime_manager(
    data_dir: str | Path, adb_path: str | Path | None = None
) -> subprocess.Popen:
    """Launch the Runtime Manager as a separate process."""
    command = runtime_manager_command(data_dir)
    environment = os.environ.copy()
    if adb_path is not None and (configured_adb := str(adb_path).strip()):
        environment["ADB_PATH"] = configured_adb
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(
            command,
            close_fds=True,
            creationflags=flags,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Runtime Manager could not start because its launcher was not found:\n"
            f"{command[0]}"
        ) from exc
