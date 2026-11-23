from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from automadin.services import browser_health
from automadin.services.browser_health import BrowserHealthState, check_browser_launch

HELPER = Path(__file__).parent / "fixtures" / "browser_health_helper.py"


def _argv(mode: str, target: str) -> tuple[str, ...]:
    return (sys.executable, str(HELPER), mode, target)


class _MockWindowsProcess:
    def __init__(self) -> None:
        self.wait_timeouts: list[float] = []
        self.returncode: int | None = None

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        self.returncode = 1
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode


def _mock_windows_job_modules(
    monkeypatch,
    *,
    query,
) -> tuple[list[object], list[tuple[object, int]]]:
    closed: list[object] = []
    terminated: list[tuple[object, int]] = []
    win32api = ModuleType("win32api")
    win32api.CloseHandle = closed.append  # type: ignore[attr-defined]
    win32job = ModuleType("win32job")
    win32job.JobObjectBasicAccountingInformation = 1  # type: ignore[attr-defined]
    win32job.TerminateJobObject = (  # type: ignore[attr-defined]
        lambda handle, exit_code: terminated.append((handle, exit_code))
    )
    win32job.QueryInformationJobObject = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32api", win32api)
    monkeypatch.setitem(sys.modules, "win32job", win32job)
    monkeypatch.setattr(browser_health.sys, "platform", "win32")
    return closed, terminated


def test_windows_job_cleanup_confirms_zero_active_processes(monkeypatch) -> None:
    process = _MockWindowsProcess()
    active_counts = iter((2, 0))
    closed, terminated = _mock_windows_job_modules(
        monkeypatch,
        query=lambda handle, information_class: {
            "ActiveProcesses": next(active_counts)
        },
    )
    now = iter((100.0, 100.0, 100.0))
    monkeypatch.setattr(browser_health.time, "monotonic", lambda: next(now))
    monkeypatch.setattr(browser_health.time, "sleep", lambda duration: None)

    assert browser_health._close_process_containment(process, "job") is True
    assert terminated == [("job", 1)]
    assert process.wait_timeouts == [10.0]
    assert closed == ["job"]


def test_windows_job_cleanup_query_failure_is_fail_closed(monkeypatch) -> None:
    process = _MockWindowsProcess()

    def fail_query(handle: object, information_class: int) -> dict[str, int]:
        raise OSError("query failed")

    closed, _ = _mock_windows_job_modules(monkeypatch, query=fail_query)
    now = iter((100.0, 100.0))
    monkeypatch.setattr(browser_health.time, "monotonic", lambda: next(now))

    assert browser_health._close_process_containment(process, "job") is False
    assert closed == ["job"]


def test_windows_job_cleanup_timeout_shares_root_wait_deadline(monkeypatch) -> None:
    process = _MockWindowsProcess()
    closed, _ = _mock_windows_job_modules(
        monkeypatch,
        query=lambda handle, information_class: {"ActiveProcesses": 1},
    )
    now = iter((100.0, 103.0, 109.99, 110.0))
    sleeps: list[float] = []
    monkeypatch.setattr(browser_health.time, "monotonic", lambda: next(now))
    monkeypatch.setattr(browser_health.time, "sleep", sleeps.append)

    assert browser_health._close_process_containment(process, "job") is False
    assert process.wait_timeouts == [7.0]
    assert sleeps == [pytest.approx(0.01)]
    assert closed == ["job"]


def test_supervisor_accepts_one_bounded_record_and_scrubs_profile_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOMADIN_PROFILES_DIR", "must-not-reach-child")

    result = check_browser_launch(
        "chromium",
        safe_temp_parent=tmp_path,
        argv=_argv("success", "chromium"),
        timeout=2,
    )

    assert result.state is BrowserHealthState.HEALTHY
    assert result.stage == "complete"
    assert result.duration >= 0
    assert list(tmp_path.iterdir()) == []


def test_supervisor_pins_the_selected_playwright_payload(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "selected-playwright-payload"
    payload.mkdir()

    result = check_browser_launch(
        "firefox",
        safe_temp_parent=tmp_path,
        argv=(
            sys.executable,
            str(HELPER),
            "environment",
            "firefox",
            str(payload),
        ),
        playwright_browsers_path=payload,
        timeout=2,
    )

    assert result.state is BrowserHealthState.HEALTHY


@pytest.mark.parametrize("mode", ["malformed", "duplicate"])
def test_supervisor_rejects_invalid_protocol(mode: str, tmp_path: Path) -> None:
    result = check_browser_launch(
        "firefox",
        safe_temp_parent=tmp_path,
        argv=_argv(mode, "firefox"),
        timeout=2,
    )

    assert result.state is BrowserHealthState.PROTOCOL_ERROR
    assert result.stage == "protocol"


def test_supervisor_hard_timeout_stops_worker_tree(tmp_path: Path) -> None:
    result = check_browser_launch(
        "webkit",
        safe_temp_parent=tmp_path,
        argv=_argv("hang", "webkit"),
        timeout=0.1,
    )

    assert result.state is BrowserHealthState.TIMED_OUT
    assert result.stage == "timeout"
    assert list(tmp_path.iterdir()) == []


def test_supervisor_cancellation_stops_worker_tree(tmp_path: Path) -> None:
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    result = check_browser_launch(
        "chrome",
        safe_temp_parent=tmp_path,
        argv=_argv("hang", "chrome"),
        timeout=2,
        cancel=cancel,
    )

    assert result.state is BrowserHealthState.CANCELLED
    assert result.stage == "cancel"
    assert list(tmp_path.iterdir()) == []


def test_workspace_cleanup_failure_overrides_healthy(
    tmp_path: Path, monkeypatch
) -> None:
    real_rmtree = shutil.rmtree

    def fail_workspace_cleanup(path: str | os.PathLike[str]) -> None:
        real_rmtree(path)
        raise OSError("simulated cleanup report")

    monkeypatch.setattr(browser_health.shutil, "rmtree", fail_workspace_cleanup)
    result = check_browser_launch(
        "msedge",
        safe_temp_parent=tmp_path,
        argv=_argv("success", "msedge"),
        timeout=2,
    )

    assert result.state is BrowserHealthState.CLEANUP_FAILED
    assert result.stage == "workspace_cleanup"


def test_supervisor_requires_explicit_safe_existing_parent(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    result = check_browser_launch(
        "chromium",
        safe_temp_parent=missing,
        argv=_argv("success", "chromium"),
    )

    assert result.state is BrowserHealthState.FAILED
    assert result.stage == "workspace"


def test_argv_and_builder_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either argv or command_builder"):
        check_browser_launch(
            "chromium",
            safe_temp_parent=tmp_path,
            argv=_argv("success", "chromium"),
            command_builder=lambda target: _argv("success", target),
        )


def test_supervisor_requires_an_explicit_worker_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="argv or a command_builder"):
        check_browser_launch("chromium", safe_temp_parent=tmp_path)


def test_missing_worker_is_probe_failure_not_browser_absence(
    tmp_path: Path,
) -> None:
    result = check_browser_launch(
        "chromium",
        safe_temp_parent=tmp_path,
        argv=(str(tmp_path / "missing-worker.exe"),),
    )

    assert result.state is BrowserHealthState.FAILED
    assert result.stage == "supervisor"


@pytest.mark.parametrize(
    ("mode", "expected_state", "timeout"),
    [
        ("success_child", BrowserHealthState.HEALTHY, 2),
        ("malformed_child", BrowserHealthState.PROTOCOL_ERROR, 2),
        ("nonzero_child", BrowserHealthState.FAILED, 2),
        ("child_hang", BrowserHealthState.TIMED_OUT, 0.15),
    ],
)
def test_worker_descendants_cannot_outlive_any_completion_path(
    mode: str,
    expected_state: BrowserHealthState,
    timeout: float,
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / f"{mode}.leaked"

    result = check_browser_launch(
        "chromium",
        safe_temp_parent=tmp_path,
        argv=(sys.executable, str(HELPER), mode, "chromium", str(sentinel)),
        timeout=timeout,
    )

    assert result.state is expected_state
    time.sleep(0.55)
    assert not sentinel.exists()


def test_raising_cancel_hook_still_contains_and_cleans_worker_tree(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "cancel-error.leaked"
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("cancel source failed")
        return False

    result = check_browser_launch(
        "firefox",
        safe_temp_parent=tmp_path,
        argv=(sys.executable, str(HELPER), "child_hang", "firefox", str(sentinel)),
        timeout=2,
        cancel=cancel,
    )

    assert result.state is BrowserHealthState.FAILED
    time.sleep(0.55)
    assert not sentinel.exists()
