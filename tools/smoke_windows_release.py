"""Behavioral smoke tests for frozen Windows release artifacts.

This gate intentionally runs only against disposable caller-owned storage.  It
never launches Automadin with the user's real LocalAppData environment, and it
places each process in a kill-on-close Job Object so cleanup is fail closed.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

EXECUTABLE_NAME = "Automadin.exe"
PORTABLE_MARKER = "portable.flag"
DATABASE_NAME = "automadin.db"
MAIN_LOG = Path("logs") / "automadin.log"
RUNTIME_MANAGER_LOG = Path("logs") / "runtime-manager.log"
DEFAULT_TIMEOUT_SECONDS = 30.0
CREATE_SUSPENDED = 0x00000004
DATABASE_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
INHERITED_RUNTIME_OVERRIDES = {
    "ADB_PATH",
    "AUTOMADIN_BROWSER_HEALTH_CONTAINED",
    "AUTOMADIN_BROWSER_HEALTH_LAUNCH_TIMEOUT_MS",
    "AUTOMADIN_BROWSER_HEALTH_RESULT",
    "AUTOMADIN_DATA_DIR",
    "AUTOMADIN_PROFILES_DIR",
    "PLAYWRIGHT_BROWSERS_PATH",
}

_T = TypeVar("_T")


class SmokeFailure(RuntimeError):
    """A release artifact failed a safety or behavioral smoke assertion."""


def _resume_suspended_process(process: subprocess.Popen[bytes]) -> None:
    """Resume every thread in a process created with CREATE_SUSPENDED."""

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    resume_process = ntdll.NtResumeProcess
    resume_process.argtypes = [ctypes.wintypes.HANDLE]
    resume_process.restype = ctypes.c_long
    status = int(resume_process(int(process._handle)))
    if status < 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08X}")


def _close_process_handle(process: subprocess.Popen[bytes]) -> None:
    """Close Popen's native handle once no further polling can occur."""

    handle = getattr(process, "_handle", None)
    if handle is not None:
        handle.Close()
        process._handle = None


@dataclass(frozen=True)
class PortableArchive:
    """Validated portable archive layout."""

    root_name: str
    members: tuple[zipfile.ZipInfo, ...]


def validate_scratch_parent(path: Path) -> Path:
    """Return a safe existing scratch parent without creating or changing it."""
    if not path.exists():
        raise SmokeFailure(f"Scratch parent does not exist: {path}")
    if not path.is_dir():
        raise SmokeFailure(f"Scratch parent is not a directory: {path}")
    if path.is_symlink():
        raise SmokeFailure(f"Scratch parent must not be a symbolic link: {path}")

    resolved = path.resolve(strict=True)
    anchor = Path(resolved.anchor)
    if resolved == anchor:
        raise SmokeFailure("A filesystem root cannot be used as the scratch parent")
    if resolved == Path.home().resolve():
        raise SmokeFailure("The user home directory cannot be used as the scratch parent")
    return resolved


def snapshot_tree(root: Path) -> tuple[tuple[str, str, int, int], ...]:
    """Capture enough state to prove pre-existing scratch content was unchanged."""
    records: list[tuple[str, str, int, int]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        kind = "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"
        size = info.st_size if kind == "file" else 0
        records.append((relative, kind, size, info.st_mtime_ns))
    return tuple(records)


def validate_release_dir(path: Path) -> Path:
    """Validate the clean standalone directory used by the smoke run."""
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise SmokeFailure(f"Standalone release is not a directory: {resolved}")
    executable = resolved / EXECUTABLE_NAME
    if not executable.is_file():
        raise SmokeFailure(f"Standalone executable is missing: {executable}")
    if (resolved / PORTABLE_MARKER).exists():
        raise SmokeFailure("Standalone release unexpectedly contains portable.flag")
    if (resolved / "data").exists() or (resolved / DATABASE_NAME).exists():
        raise SmokeFailure("Standalone release contains bundled user data")
    return resolved


def _archive_member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename
    if not name or "\\" in name or "\0" in name:
        raise SmokeFailure(f"Unsafe ZIP member name: {name!r}")
    path = PurePosixPath(name.rstrip("/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SmokeFailure(f"Unsafe ZIP member path: {name!r}")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == stat.S_IFLNK:
        raise SmokeFailure(f"Portable ZIP contains a symbolic link: {name}")
    if info.flag_bits & 0x1:
        raise SmokeFailure(f"Portable ZIP contains an encrypted member: {name}")
    return path


def inspect_portable_archive(path: Path) -> PortableArchive:
    """Validate a clean, single-root portable archive without extracting it."""
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise SmokeFailure(f"Portable archive is not a file: {resolved}")

    with zipfile.ZipFile(resolved) as archive:
        members = tuple(archive.infolist())
        if not members:
            raise SmokeFailure("Portable ZIP is empty")

        roots: set[str] = set()
        seen: set[str] = set()
        marker_count = 0
        executable_count = 0
        root_marker_count = 0
        root_executable_count = 0
        for info in members:
            member = _archive_member_path(info)
            roots.add(member.parts[0])
            normalized = "/".join(member.parts).casefold()
            if normalized in seen:
                raise SmokeFailure(f"Portable ZIP contains a Windows path collision: {info.filename}")
            seen.add(normalized)

            relative = tuple(part.casefold() for part in member.parts[1:])
            if member.name.casefold() == PORTABLE_MARKER:
                marker_count += 1
            if member.name.casefold() == EXECUTABLE_NAME.casefold():
                executable_count += 1
            if relative == (PORTABLE_MARKER,):
                root_marker_count += 1
            if relative == (EXECUTABLE_NAME.casefold(),):
                root_executable_count += 1
            if relative and relative[0] == "data":
                raise SmokeFailure(f"Portable ZIP contains bundled data: {info.filename}")
            lower_name = member.name.casefold()
            if any(lower_name.endswith(suffix) for suffix in DATABASE_SUFFIXES):
                raise SmokeFailure(f"Portable ZIP contains a database artifact: {info.filename}")

        if len(roots) != 1:
            raise SmokeFailure("Portable ZIP must contain exactly one top-level directory")
        if marker_count != 1 or root_marker_count != 1:
            raise SmokeFailure("Portable ZIP must contain exactly one root portable.flag")
        if executable_count != 1 or root_executable_count != 1:
            raise SmokeFailure("Portable ZIP must contain exactly one root Automadin.exe")
        return PortableArchive(next(iter(roots)), members)


def extract_portable_archive(archive_path: Path, destination: Path) -> Path:
    """Extract a previously validated archive without zipfile path traversal."""
    layout = inspect_portable_archive(archive_path)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        for info in layout.members:
            member = _archive_member_path(info)
            target = destination.joinpath(*member.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    return destination / layout.root_name


def isolated_environment(root: Path) -> dict[str, str]:
    """Build a process environment that cannot select the user's normal data path."""
    environment = os.environ.copy()
    for variable in INHERITED_RUNTIME_OVERRIDES:
        environment.pop(variable, None)
    for variable, child in (
        ("LOCALAPPDATA", "local-app-data"),
        ("APPDATA", "roaming-app-data"),
        ("TEMP", "temp"),
        ("TMP", "temp"),
    ):
        location = root / child
        location.mkdir(parents=True, exist_ok=True)
        environment[variable] = str(location)
    return environment


def sqlite_quick_check(path: Path) -> None:
    """Require a readable SQLite database whose full quick check is clean."""
    if not path.is_file():
        raise SmokeFailure(f"Database was not created: {path}")
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(
            sqlite3.connect(uri, uri=True, timeout=2.0)
        ) as connection:
            rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        raise SmokeFailure(f"Could not inspect database {path}: {exc}") from exc
    if rows != [("ok",)]:
        raise SmokeFailure(f"SQLite quick_check failed for {path}: {rows!r}")


def _visible_windows_for_pid(pid: int) -> tuple[int, ...]:
    import win32gui
    import win32process

    handles: list[int] = []

    def collect(hwnd: int, _context: object) -> bool:
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid == pid and win32gui.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    win32gui.EnumWindows(collect, None)
    return tuple(handles)


def _wait_for(
    probe: Callable[[], _T | None],
    *,
    timeout: float,
    description: str,
) -> _T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    detail = f"; last error: {last_error}" if last_error is not None else ""
    raise SmokeFailure(f"Timed out waiting for {description}{detail}")


class ManagedProcess:
    """A hidden subprocess contained by a kill-on-close Windows Job Object."""

    def __init__(self, command: list[str], environment: dict[str, str]) -> None:
        import win32job

        self._win32job = win32job
        self._closed = False
        self.process: subprocess.Popen[bytes] | None = None
        self._job = win32job.CreateJobObject(None, "")
        assigned = False
        try:
            limits = win32job.QueryInformationJobObject(
                self._job,
                win32job.JobObjectExtendedLimitInformation,
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self._job,
                win32job.JobObjectExtendedLimitInformation,
                limits,
            )
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED
                ),
                close_fds=True,
            )
            win32job.AssignProcessToJobObject(self._job, self.process._handle)
            assigned = True
            _resume_suspended_process(self.process)
        except Exception:
            try:
                if self.process is not None:
                    terminated = False
                    if assigned:
                        try:
                            win32job.TerminateJobObject(self._job, 91)
                            terminated = True
                        except Exception:
                            pass
                    if not terminated:
                        try:
                            self.process.kill()
                        except Exception:
                            pass
                    try:
                        self.process.wait(timeout=10)
                    except Exception:
                        pass
            finally:
                self._job.Close()
                if self.process is not None:
                    _close_process_handle(self.process)
            raise

    @property
    def pid(self) -> int:
        assert self.process is not None
        return self.process.pid

    def active_processes(self) -> int:
        details = self._win32job.QueryInformationJobObject(
            self._job,
            self._win32job.JobObjectBasicAccountingInformation,
        )
        return int(details["ActiveProcesses"])

    def require_visible_window(self, timeout: float) -> tuple[int, ...]:
        return _wait_for(
            lambda: _visible_windows_for_pid(self.pid) or None,
            timeout=timeout,
            description=f"a visible top-level window from PID {self.pid}",
        )

    def require_clean_exit(self, timeout: float) -> None:
        assert self.process is not None
        try:
            exit_code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise SmokeFailure(f"PID {self.pid} did not exit within {timeout:g}s") from exc
        if exit_code != 0:
            raise SmokeFailure(f"PID {self.pid} exited with code {exit_code}")
        _wait_for(
            lambda: self.active_processes() == 0,
            timeout=timeout,
            description=f"all processes in the Job Object for PID {self.pid} to exit",
        )

    def close_gracefully(self, timeout: float) -> None:
        import win32con
        import win32gui

        assert self.process is not None
        handles = _visible_windows_for_pid(self.pid)
        if not handles:
            raise SmokeFailure(f"PID {self.pid} has no visible window to close")
        deadline = time.monotonic() + timeout
        while self.process.poll() is None:
            for hwnd in _visible_windows_for_pid(self.pid):
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(
                    f"PID {self.pid} did not exit within {timeout:g}s"
                )
            try:
                exit_code = self.process.wait(timeout=min(1.0, remaining))
            except subprocess.TimeoutExpired:
                continue
            if exit_code != 0:
                raise SmokeFailure(f"PID {self.pid} exited with code {exit_code}")

        exit_code = self.process.poll()
        if exit_code != 0:
            raise SmokeFailure(f"PID {self.pid} exited with code {exit_code}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SmokeFailure(
                f"PID {self.pid} descendants did not exit within {timeout:g}s"
            )
        _wait_for(
            lambda: self.active_processes() == 0,
            timeout=remaining,
            description=f"all processes in the Job Object for PID {self.pid} to exit",
        )

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.active_processes():
                self._win32job.TerminateJobObject(self._job, 91)
                try:
                    assert self.process is not None
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                _wait_for(
                    lambda: self.active_processes() == 0,
                    timeout=10,
                    description="Job Object cleanup",
                )
        finally:
            self._job.Close()
            if self.process is not None:
                if self.process.returncode is None:
                    self.process.poll()
                _close_process_handle(self.process)


@contextmanager
def managed_process(
    command: list[str],
    environment: dict[str, str],
) -> Iterator[ManagedProcess]:
    process = ManagedProcess(command, environment)
    try:
        yield process
    finally:
        process.cleanup()


def _require_created_file(path: Path, timeout: float, description: str) -> Path:
    return _wait_for(
        lambda: path if path.is_file() and path.stat().st_size > 0 else None,
        timeout=timeout,
        description=description,
    )


def smoke_main_application(
    executable: Path,
    data_dir: Path,
    environment: dict[str, str],
    timeout: float,
) -> None:
    data_dir.mkdir(parents=True, exist_ok=False)
    command = [str(executable), "--data-dir", str(data_dir), "--debug"]
    with managed_process(command, environment) as primary:
        print("Smoke: standalone primary window", flush=True)
        primary.require_visible_window(timeout)
        _require_created_file(data_dir / DATABASE_NAME, timeout, "the standalone database")
        _require_created_file(data_dir / MAIN_LOG, timeout, "the standalone diagnostics log")

        print("Smoke: standalone activation handoff", flush=True)
        with managed_process(command, environment) as secondary:
            secondary.require_clean_exit(timeout)
        if primary.process.poll() is not None:
            raise SmokeFailure("Primary instance exited after the activation handoff")
        if not _visible_windows_for_pid(primary.pid):
            raise SmokeFailure("Primary instance lost its visible window after activation")
        print("Smoke: standalone graceful close", flush=True)
        primary.close_gracefully(timeout)
    sqlite_quick_check(data_dir / DATABASE_NAME)


def smoke_runtime_manager(
    executable: Path,
    data_dir: Path,
    environment: dict[str, str],
    timeout: float,
) -> None:
    data_dir.mkdir(parents=True, exist_ok=False)
    command = [
        str(executable),
        "--runtime-manager",
        "--data-dir",
        str(data_dir),
        "--debug",
    ]
    with managed_process(command, environment) as manager:
        manager.require_visible_window(timeout)
        _require_created_file(
            data_dir / RUNTIME_MANAGER_LOG,
            timeout,
            "the Runtime Manager diagnostics log",
        )
        manager.close_gracefully(timeout)


def _remove_tree(path: Path) -> None:
    def retry_readonly(function: Callable[..., object], target: str, _: object) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    deadline = time.monotonic() + 5.0
    while True:
        try:
            shutil.rmtree(path, onerror=retry_readonly)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def run_smoke(
    release_dir: Path,
    portable_zip: Path,
    scratch_parent: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Run all release behaviors and restore the scratch parent exactly."""
    if sys.platform != "win32":
        raise SmokeFailure("The Windows release smoke gate must run on Windows")
    if timeout <= 0:
        raise SmokeFailure("Timeout must be greater than zero")

    standalone = validate_release_dir(release_dir)
    archive = portable_zip.resolve(strict=True)
    inspect_portable_archive(archive)
    scratch = validate_scratch_parent(scratch_parent)
    before = snapshot_tree(scratch)
    session = Path(tempfile.mkdtemp(prefix="automadin-release-smoke-", dir=scratch))
    failure: BaseException | None = None
    try:
        environment = isolated_environment(session / "environment")
        print("Smoke: standalone application", flush=True)
        smoke_main_application(
            standalone / EXECUTABLE_NAME,
            session / "standalone-data",
            environment,
            timeout,
        )
        print("Smoke: Runtime Manager", flush=True)
        smoke_runtime_manager(
            standalone / EXECUTABLE_NAME,
            session / "runtime-manager-data",
            environment,
            timeout,
        )

        print("Smoke: portable application", flush=True)
        portable_root = extract_portable_archive(archive, session / "portable-extract")
        if not (portable_root / PORTABLE_MARKER).is_file():
            raise SmokeFailure("Extracted portable marker is missing")
        if (portable_root / "data").exists():
            raise SmokeFailure("Portable data directory existed before first launch")
        with managed_process(
            [str(portable_root / EXECUTABLE_NAME), "--debug"],
            environment,
        ) as portable:
            portable.require_visible_window(timeout)
            portable_data = portable_root / "data"
            _require_created_file(
                portable_data / DATABASE_NAME,
                timeout,
                "the portable database beside the executable",
            )
            _require_created_file(
                portable_data / MAIN_LOG,
                timeout,
                "the portable diagnostics log beside the executable",
            )
            portable.close_gracefully(timeout)
        sqlite_quick_check(portable_root / "data" / DATABASE_NAME)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            _remove_tree(session)
            after = snapshot_tree(scratch)
            if after != before:
                raise SmokeFailure(
                    "Scratch parent was not restored exactly after the smoke run"
                )
        except BaseException as cleanup_error:
            if failure is None:
                raise
            raise SmokeFailure(
                f"Release smoke failed ({failure}); cleanup also failed: {cleanup_error}"
            ) from cleanup_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("portable_zip", type=Path)
    parser.add_argument("scratch_parent", type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        run_smoke(
            args.release_dir,
            args.portable_zip,
            args.scratch_parent,
            timeout=args.timeout,
        )
    except (OSError, SmokeFailure, zipfile.BadZipFile) as exc:
        print(f"WINDOWS RELEASE SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    print("Windows standalone, Runtime Manager, and portable release smokes passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
