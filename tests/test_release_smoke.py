from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest

from tools import smoke_windows_release
from tools.smoke_windows_release import (
    CREATE_SUSPENDED,
    INHERITED_RUNTIME_OVERRIDES,
    ManagedProcess,
    SmokeFailure,
    inspect_portable_archive,
    isolated_environment,
    snapshot_tree,
    sqlite_quick_check,
    validate_release_dir,
    validate_scratch_parent,
)


class _FakeHandle:
    def __init__(self, events: list[str], name: str) -> None:
        self._events = events
        self._name = name

    def Close(self) -> None:
        self._events.append(f"close:{self._name}")


class _FakeProcess:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._handle = _FakeHandle(events, "process")
        self.pid = 123
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        self._events.append("wait")
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self._events.append("kill")
        self.returncode = 91


class _RetryCloseProcess(_FakeProcess):
    def __init__(self, events: list[str], waits_before_exit: int) -> None:
        super().__init__(events)
        self._waits_before_exit = waits_before_exit

    def wait(self, timeout: float) -> int:
        self._events.append("wait")
        if self._waits_before_exit:
            self._waits_before_exit -= 1
            raise subprocess.TimeoutExpired(["Automadin.exe"], timeout)
        self.returncode = 0
        return 0


def _fake_win32job(
    events: list[str],
    *,
    assign_error: Exception | None = None,
) -> types.SimpleNamespace:
    job = _FakeHandle(events, "job")

    def query(_job: object, information_class: int) -> dict[str, object]:
        if information_class == 1:
            return {"BasicLimitInformation": {"LimitFlags": 0}}
        return {"ActiveProcesses": 0}

    def assign(_job: object, _process: object) -> None:
        events.append("assign")
        if assign_error is not None:
            raise assign_error

    return types.SimpleNamespace(
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=1,
        JobObjectExtendedLimitInformation=1,
        JobObjectBasicAccountingInformation=2,
        CreateJobObject=lambda _security, _name: job,
        QueryInformationJobObject=query,
        SetInformationJobObject=lambda *_args: events.append("configure"),
        AssignProcessToJobObject=assign,
        TerminateJobObject=lambda *_args: events.append("terminate-job"),
    )


def test_managed_process_is_contained_before_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fake_process = _FakeProcess(events)
    monkeypatch.setitem(sys.modules, "win32job", _fake_win32job(events))

    def popen(_command: list[str], **options: object) -> _FakeProcess:
        assert options["creationflags"] & CREATE_SUSPENDED
        events.append("create-suspended")
        return fake_process

    monkeypatch.setattr(smoke_windows_release.subprocess, "Popen", popen)
    monkeypatch.setattr(
        smoke_windows_release,
        "_resume_suspended_process",
        lambda _process: events.append("resume"),
    )

    managed = ManagedProcess(["Automadin.exe"], {})
    managed.cleanup()

    assert events[:4] == ["configure", "create-suspended", "assign", "resume"]
    assert events[-2:] == ["close:job", "close:process"]


def test_managed_process_assignment_failure_kills_suspended_root_and_closes_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fake_process = _FakeProcess(events)
    monkeypatch.setitem(
        sys.modules,
        "win32job",
        _fake_win32job(events, assign_error=OSError("assignment failed")),
    )
    monkeypatch.setattr(
        smoke_windows_release.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )
    monkeypatch.setattr(
        smoke_windows_release,
        "_resume_suspended_process",
        lambda _process: events.append("resume"),
    )

    with pytest.raises(OSError, match="assignment failed"):
        ManagedProcess(["Automadin.exe"], {})

    assert "resume" not in events
    assert events[-4:] == ["kill", "wait", "close:job", "close:process"]


def test_managed_process_resume_failure_terminates_job_and_closes_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fake_process = _FakeProcess(events)
    monkeypatch.setitem(sys.modules, "win32job", _fake_win32job(events))
    monkeypatch.setattr(
        smoke_windows_release.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )

    def fail_resume(_process: object) -> None:
        events.append("resume")
        raise OSError("resume failed")

    monkeypatch.setattr(smoke_windows_release, "_resume_suspended_process", fail_resume)

    with pytest.raises(OSError, match="resume failed"):
        ManagedProcess(["Automadin.exe"], {})

    assert "kill" not in events
    assert events[-4:] == ["terminate-job", "wait", "close:job", "close:process"]


def test_graceful_close_retries_while_application_defers_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    process = _RetryCloseProcess(events, waits_before_exit=2)
    managed = ManagedProcess.__new__(ManagedProcess)
    managed.process = process
    managed._job = object()
    managed._win32job = types.SimpleNamespace(
        JobObjectBasicAccountingInformation=2,
        QueryInformationJobObject=lambda *_args: {"ActiveProcesses": 0},
    )
    monkeypatch.setattr(
        smoke_windows_release,
        "_visible_windows_for_pid",
        lambda _pid: (101,),
    )
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        types.SimpleNamespace(
            PostMessage=lambda *_args: events.append("post-close"),
        ),
    )

    managed.close_gracefully(5)

    assert events == [
        "post-close",
        "wait",
        "post-close",
        "wait",
        "post-close",
        "wait",
    ]


def _portable_zip(path: Path, extra: dict[str, bytes] | None = None) -> Path:
    members = {
        "Automadin-Portable/Automadin.exe": b"MZ",
        "Automadin-Portable/portable.flag": b"",
        "Automadin-Portable/runtime.dll": b"runtime",
    }
    members.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def test_validate_scratch_parent_requires_existing_non_root_directory(
    tmp_path: Path,
) -> None:
    assert validate_scratch_parent(tmp_path) == tmp_path.resolve()
    with pytest.raises(SmokeFailure, match="does not exist"):
        validate_scratch_parent(tmp_path / "missing")
    file = tmp_path / "file"
    file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SmokeFailure, match="not a directory"):
        validate_scratch_parent(file)


def test_snapshot_tree_detects_changes_but_is_stable(tmp_path: Path) -> None:
    child = tmp_path / "existing"
    child.mkdir()
    file = child / "value.txt"
    file.write_text("one", encoding="utf-8")
    before = snapshot_tree(tmp_path)
    assert snapshot_tree(tmp_path) == before
    file.write_text("a longer value", encoding="utf-8")
    assert snapshot_tree(tmp_path) != before


def test_validate_release_dir_requires_clean_standalone_layout(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    executable = release / "Automadin.exe"
    executable.write_bytes(b"MZ")
    assert validate_release_dir(release) == release.resolve()
    (release / "portable.flag").touch()
    with pytest.raises(SmokeFailure, match="portable.flag"):
        validate_release_dir(release)


def test_inspect_portable_archive_accepts_clean_single_root(tmp_path: Path) -> None:
    archive = _portable_zip(tmp_path / "portable.zip")
    layout = inspect_portable_archive(archive)
    assert layout.root_name == "Automadin-Portable"
    assert len(layout.members) == 3


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"../escape": b"x"}, "Unsafe ZIP member"),
        ({"other/file.dll": b"x"}, "one top-level"),
        ({"Automadin-Portable/data/state.txt": b"x"}, "bundled data"),
        ({"Automadin-Portable/state.sqlite-wal": b"x"}, "database artifact"),
        ({"Automadin-Portable/PORTABLE.FLAG": b"x"}, "path collision"),
        ({"Automadin-Portable/nested/portable.flag": b"x"}, "exactly one root"),
    ],
)
def test_inspect_portable_archive_rejects_unsafe_or_dirty_members(
    tmp_path: Path,
    extra: dict[str, bytes],
    message: str,
) -> None:
    archive = _portable_zip(tmp_path / "portable.zip", extra)
    with pytest.raises(SmokeFailure, match=message):
        inspect_portable_archive(archive)


def test_inspect_portable_archive_requires_marker_and_executable(tmp_path: Path) -> None:
    archive = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Automadin-Portable/runtime.dll", b"x")
    with pytest.raises(SmokeFailure, match="portable.flag"):
        inspect_portable_archive(archive)


def test_isolated_environment_redirects_user_storage_and_removes_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in INHERITED_RUNTIME_OVERRIDES:
        monkeypatch.setenv(variable, "C:/real-user-or-runtime-path")
    environment = isolated_environment(tmp_path)
    assert INHERITED_RUNTIME_OVERRIDES.isdisjoint(environment)
    for variable in ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
        assert Path(environment[variable]).resolve().is_relative_to(tmp_path.resolve())
        assert Path(environment[variable]).is_dir()


def test_sqlite_quick_check_accepts_clean_database(tmp_path: Path) -> None:
    database = tmp_path / "automadin.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
        connection.execute("INSERT INTO example VALUES ('ok')")
    sqlite_quick_check(database)


def test_sqlite_quick_check_closes_its_read_only_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        @staticmethod
        def fetchall() -> list[tuple[str]]:
            return [("ok",)]

    class _Connection:
        closed = False

        @staticmethod
        def execute(_statement: str) -> _Cursor:
            return _Cursor()

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    monkeypatch.setattr(
        smoke_windows_release.sqlite3,
        "connect",
        lambda *args, **kwargs: connection,
    )
    database = tmp_path / "automadin.db"
    database.touch()

    sqlite_quick_check(database)

    assert connection.closed is True


def test_sqlite_quick_check_rejects_non_database(tmp_path: Path) -> None:
    database = tmp_path / "automadin.db"
    database.write_bytes(os.urandom(128))
    with pytest.raises(SmokeFailure, match="Could not inspect database"):
        sqlite_quick_check(database)
