"""Process-isolated checks for an ephemeral browser launch.

This module is deliberately Qt-free.  It does not inspect or use browser
profiles, network access, authentication state, or headed-mode behavior.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS

SUPPORTED_BROWSER_HEALTH_TARGETS = frozenset(
    {"chromium", "firefox", "webkit", "chrome", "msedge"}
)
MAX_PROTOCOL_BYTES = 4096
_PROTOCOL_KEYS = frozenset({"target", "state", "stage", "message"})
RESULT_PATH_ENV = "AUTOMADIN_BROWSER_HEALTH_RESULT"
LAUNCH_TIMEOUT_ENV = "AUTOMADIN_BROWSER_HEALTH_LAUNCH_TIMEOUT_MS"
CONTAINMENT_READY_ENV = "AUTOMADIN_BROWSER_HEALTH_CONTAINED"
_ALLOWED_ENVIRONMENT_KEYS = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMSPEC",
        "DISPLAY",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "LOCALAPPDATA",
        "LOGNAME",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "XDG_RUNTIME_DIR",
    }
)
class BrowserHealthState(str, Enum):
    HEALTHY = "healthy"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    PROTOCOL_ERROR = "protocol_error"
    CLEANUP_FAILED = "cleanup_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class BrowserHealthResult:
    """Immutable outcome of one isolated, ephemeral launch check."""

    target: str
    state: BrowserHealthState
    stage: str
    checked_at: datetime
    duration: float
    message: str

    @property
    def blocks_new_launch(self) -> bool:
        """Return whether repeating the probe is unsafe.

        The health worker deliberately tests a temporary headless browser, while
        Automadin's real browser sessions are headed and may use a managed
        profile.  A failed headless check is useful evidence but cannot
        conclusively reject that different launch mode.  Missing runtime files
        remain gated by ``RuntimeComponentStatus`` before this result is
        attached; only unconfirmed process cleanup blocks selection here.
        """
        return self.state is BrowserHealthState.CLEANUP_FAILED


CommandBuilder = Callable[[str], Sequence[str]]
CancelHook = Callable[[], bool]


def _result(
    target: str,
    state: BrowserHealthState,
    stage: str,
    checked_at: datetime,
    started: float,
    message: str,
) -> BrowserHealthResult:
    return BrowserHealthResult(
        target=target,
        state=state,
        stage=stage,
        checked_at=checked_at,
        duration=max(0.0, time.monotonic() - started),
        message=message,
    )


def _validated_parent(path: str | Path) -> Path:
    parent = Path(path).expanduser().resolve(strict=True)
    if not parent.is_dir():
        raise NotADirectoryError(f"Browser health temporary parent is not a directory: {parent}")
    return parent


def _worker_environment(
    workspace: Path,
    result_path: Path,
    containment_ready_path: Path,
    launch_timeout: float,
    playwright_browsers_path: str | Path | None = None,
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ALLOWED_ENVIRONMENT_KEYS
    }
    if playwright_browsers_path is not None:
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(
            Path(playwright_browsers_path).expanduser().resolve()
        )
    temp = str(workspace)
    launch_timeout_ms = max(500, int(max(0.5, launch_timeout - 1.0) * 1000))
    env.update(
        {
            "TMP": temp,
            "TEMP": temp,
            "TMPDIR": temp,
            "PYTHONUTF8": "1",
            RESULT_PATH_ENV: str(result_path),
            CONTAINMENT_READY_ENV: str(containment_ready_path),
            LAUNCH_TIMEOUT_ENV: str(launch_timeout_ms),
        }
    )
    return env


def _popen_options() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": HIDDEN_PROCESS_CREATION_FLAGS}
    return {"start_new_session": True}


def _establish_process_containment(process: subprocess.Popen[bytes]) -> Any:
    """Contain the complete worker tree before Playwright can outlive its worker."""
    if sys.platform != "win32":
        # start_new_session=True made the worker PID its process-group ID.
        return process.pid

    import win32api
    import win32job

    job = win32job.CreateJobObject(None, "")
    try:
        information = win32job.QueryInformationJobObject(
            job,
            win32job.JobObjectExtendedLimitInformation,
        )
        information["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job,
            win32job.JobObjectExtendedLimitInformation,
            information,
        )
        win32job.AssignProcessToJobObject(job, process._handle)
    except Exception:
        win32api.CloseHandle(job)
        raise
    return job


def _close_process_containment(
    process: subprocess.Popen[bytes],
    containment: Any,
) -> bool:
    """Close containment, terminate any descendants, and confirm the root exit."""
    if sys.platform == "win32":
        try:
            import win32api
            import win32job
        except ImportError:
            return False
        confirmed = False
        deadline = time.monotonic() + 10
        try:
            win32job.TerminateJobObject(containment, 1)
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
            while True:
                accounting = win32job.QueryInformationJobObject(
                    containment,
                    win32job.JobObjectBasicAccountingInformation,
                )
                if accounting["ActiveProcesses"] == 0:
                    confirmed = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.02, remaining))
        except Exception:
            confirmed = False
        finally:
            try:
                win32api.CloseHandle(containment)
            except Exception:
                confirmed = False
        return confirmed and process.poll() is not None

    group_id = int(containment)
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.monotonic() + 2
    while True:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return process.poll() is not None
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _taskkill_tree(pid: int) -> bool:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    taskkill = system_root / "System32" / "taskkill.exe"
    if not taskkill.is_file():
        return False
    completed = subprocess.run(
        [str(taskkill), "/PID", str(pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        check=False,
        timeout=10,
    )
    return completed.returncode == 0


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> bool:
    """Force-stop the worker tree and confirm that its root exited."""

    attempted = False
    try:
        if sys.platform == "win32":
            attempted = _taskkill_tree(process.pid)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                attempted = True
            except ProcessLookupError:
                attempted = True
    except (OSError, subprocess.SubprocessError):
        attempted = False

    try:
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return attempted and process.poll() is not None


def _decode_protocol(stdout: bytes, target: str) -> tuple[BrowserHealthState, str, str]:
    if not stdout or len(stdout) > MAX_PROTOCOL_BYTES:
        raise ValueError("Worker output was empty or exceeded the protocol limit.")
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Worker output was not UTF-8.") from exc
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise ValueError("Worker output was not exactly one record.")
    try:
        record = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ValueError("Worker output was not valid JSON.") from exc
    if not isinstance(record, dict) or frozenset(record) != _PROTOCOL_KEYS:
        raise ValueError("Worker record fields did not match the protocol.")
    if not all(isinstance(record[key], str) for key in _PROTOCOL_KEYS):
        raise ValueError("Worker record fields were not strings.")
    if record["target"] != target:
        raise ValueError("Worker record target did not match the requested target.")
    try:
        state = BrowserHealthState(record["state"])
    except ValueError as exc:
        raise ValueError("Worker record contained an unknown state.") from exc
    if state in {
        BrowserHealthState.TIMED_OUT,
        BrowserHealthState.PROTOCOL_ERROR,
        BrowserHealthState.CANCELLED,
    }:
        raise ValueError("Worker record contained a supervisor-owned state.")
    if not record["stage"] or len(record["stage"]) > 64 or len(record["message"]) > 512:
        raise ValueError("Worker record text exceeded the protocol limits.")
    return state, record["stage"], record["message"]


def _read_protocol_file(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("Worker result file was not available.") from exc
    if size <= 0 or size > MAX_PROTOCOL_BYTES:
        raise ValueError("Worker result file exceeded the protocol limit.")
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_PROTOCOL_BYTES + 1)
    except OSError as exc:
        raise ValueError("Worker result file could not be read.") from exc
    if len(payload) != size or len(payload) > MAX_PROTOCOL_BYTES:
        raise ValueError("Worker result file changed while it was read.")
    return payload


def check_browser_launch(
    target: str,
    *,
    safe_temp_parent: str | Path,
    timeout: float = 15.0,
    cancel: CancelHook = lambda: False,
    argv: Sequence[str] | None = None,
    command_builder: CommandBuilder | None = None,
    playwright_browsers_path: str | Path | None = None,
) -> BrowserHealthResult:
    """Run one browser launch check in a disposable child process.

    ``timeout`` bounds the active worker probe.  Forced process-tree and
    workspace cleanup remains bounded separately and may extend the wall-clock
    duration after timeout or cancellation; cleanup confirmation takes priority
    over returning early and potentially leaving a browser behind.
    """

    checked_at = datetime.now(UTC)
    started = time.monotonic()
    if target not in SUPPORTED_BROWSER_HEALTH_TARGETS:
        return _result(
            target,
            BrowserHealthState.UNAVAILABLE,
            "validation",
            checked_at,
            started,
            "This isolated headless launch target is not supported.",
        )
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if argv is not None and command_builder is not None:
        raise ValueError("Pass either argv or command_builder, not both.")
    if argv is None and command_builder is None:
        raise ValueError("Pass argv or a command_builder for the worker entrypoint.")

    try:
        parent = _validated_parent(safe_temp_parent)
        workspace = Path(tempfile.mkdtemp(prefix="automadin-browser-health-", dir=parent))
    except (OSError, ValueError) as exc:
        return _result(
            target,
            BrowserHealthState.FAILED,
            "workspace",
            checked_at,
            started,
            f"Could not create the isolated launch workspace: {type(exc).__name__}.",
        )

    process: subprocess.Popen[bytes] | None = None
    containment: Any | None = None
    containment_closed = False
    result: BrowserHealthResult | None = None
    result_path = workspace / "result.json"
    containment_ready_path = workspace / "contained.ready"
    try:
        try:
            already_cancelled = cancel()
        except Exception:
            result = _result(
                target,
                BrowserHealthState.FAILED,
                "cancel",
                checked_at,
                started,
                "The launch-check cancellation state could not be read.",
            )
        else:
            result = None
        if result is None and already_cancelled:
            result = _result(
                target,
                BrowserHealthState.CANCELLED,
                "cancel",
                checked_at,
                started,
                "The isolated headless launch check was cancelled.",
            )

        if result is None:
            if argv is not None:
                command = tuple(argv)
            else:
                assert command_builder is not None
                command = tuple(command_builder(target))
            if not command or not all(
                isinstance(part, str) and part for part in command
            ):
                raise ValueError(
                    "Worker command must contain non-empty string arguments."
                )
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=_worker_environment(
                    workspace,
                    result_path,
                    containment_ready_path,
                    timeout,
                    playwright_browsers_path,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                **_popen_options(),
            )
            try:
                containment = _establish_process_containment(process)
                containment_ready_path.write_bytes(b"contained\n")
            except Exception:
                stopped = _terminate_process_tree(process)
                result = _result(
                    target,
                    (
                        BrowserHealthState.FAILED
                        if stopped
                        else BrowserHealthState.CLEANUP_FAILED
                    ),
                    "containment" if stopped else "cleanup",
                    checked_at,
                    started,
                    (
                        "The isolated launch worker could not be safely contained."
                        if stopped
                        else "Worker containment failed and process cleanup was not confirmed."
                    ),
                )

        deadline = started + timeout
        while result is None and process is not None and process.poll() is None:
            if cancel():
                tree_stopped = (
                    _close_process_containment(process, containment)
                    if containment is not None
                    else _terminate_process_tree(process)
                )
                containment_closed = containment is not None
                result = _result(
                    target,
                    (
                        BrowserHealthState.CANCELLED
                        if tree_stopped
                        else BrowserHealthState.CLEANUP_FAILED
                    ),
                    "cancel",
                    checked_at,
                    started,
                    (
                        "The isolated headless launch check was cancelled."
                        if tree_stopped
                        else "Cancellation could not confirm worker process cleanup."
                    ),
                )
                break
            if time.monotonic() >= deadline:
                tree_stopped = (
                    _close_process_containment(process, containment)
                    if containment is not None
                    else _terminate_process_tree(process)
                )
                containment_closed = containment is not None
                result = _result(
                    target,
                    (
                        BrowserHealthState.TIMED_OUT
                        if tree_stopped
                        else BrowserHealthState.CLEANUP_FAILED
                    ),
                    "timeout",
                    checked_at,
                    started,
                    (
                        "The isolated headless launch check exceeded its time limit."
                        if tree_stopped
                        else "Timeout cleanup could not confirm worker process termination."
                    ),
                )
                break
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

        if result is None and process is not None and containment is not None:
            containment_closed = True
            if not _close_process_containment(process, containment):
                result = _result(
                    target,
                    BrowserHealthState.CLEANUP_FAILED,
                    "process_cleanup",
                    checked_at,
                    started,
                    "The isolated worker exited, but descendant cleanup was not confirmed.",
                )

        if result is None and process is not None:
            if process.returncode != 0:
                result = _result(
                    target,
                    BrowserHealthState.FAILED,
                    "worker",
                    checked_at,
                    started,
                    "The isolated headless launch worker exited unsuccessfully.",
                )
            else:
                try:
                    state, stage, message = _decode_protocol(
                        _read_protocol_file(result_path),
                        target,
                    )
                except ValueError:
                    result = _result(
                        target,
                        BrowserHealthState.PROTOCOL_ERROR,
                        "protocol",
                        checked_at,
                        started,
                        "The isolated headless launch worker returned an invalid record.",
                    )
                else:
                    result = _result(
                        target, state, stage, checked_at, started, message
                    )
    except Exception as exc:
        if process is not None and process.poll() is None:
            if containment is not None and not containment_closed:
                stopped = _close_process_containment(process, containment)
                containment_closed = True
            else:
                stopped = _terminate_process_tree(process)
        else:
            stopped = True
        result = _result(
            target,
            BrowserHealthState.FAILED,
            "supervisor" if stopped else "cleanup",
            checked_at,
            started,
            (
                f"Could not start the isolated headless launch worker: {type(exc).__name__}."
                if stopped
                else "Worker failure cleanup could not confirm process termination."
            ),
        )
        if not stopped:
            result = _result(
                target,
                BrowserHealthState.CLEANUP_FAILED,
                "cleanup",
                checked_at,
                started,
                "Worker failure cleanup could not confirm process termination.",
            )
    finally:
        if (
            process is not None
            and containment is not None
            and not containment_closed
        ):
            containment_closed = True
            if not _close_process_containment(process, containment):
                result = _result(
                    target,
                    BrowserHealthState.CLEANUP_FAILED,
                    "process_cleanup",
                    checked_at,
                    started,
                    "The isolated worker process tree could not be fully cleaned up.",
                )
        workspace_cleanup_deadline = time.monotonic() + 2
        while True:
            try:
                shutil.rmtree(workspace)
                break
            except PermissionError:
                if (
                    sys.platform != "win32"
                    or time.monotonic() >= workspace_cleanup_deadline
                ):
                    result = _result(
                        target,
                        BrowserHealthState.CLEANUP_FAILED,
                        "workspace_cleanup",
                        checked_at,
                        started,
                        "The isolated launch workspace could not be removed.",
                    )
                    break
                time.sleep(0.02)
            except OSError:
                result = _result(
                    target,
                    BrowserHealthState.CLEANUP_FAILED,
                    "workspace_cleanup",
                    checked_at,
                    started,
                    "The isolated launch workspace could not be removed.",
                )
                break

    assert result is not None
    return result
