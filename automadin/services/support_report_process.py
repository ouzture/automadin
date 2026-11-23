"""Bounded subprocess collection for privacy-safe support reports."""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from automadin.runtime_components import application_command
from automadin.services.support_report import (
    SupportReport,
    decode_support_report_record_json,
)
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS
from automadin.support_report_worker import (
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    REQUEST_FILE_NAME,
    REQUEST_VERSION,
    RESULT_FILE_NAME,
)

CommandBuilder = Callable[[Path], Sequence[str]]
CancelCallback = Callable[[], bool]


class SupportReportCollectionError(RuntimeError):
    """Base error for isolated support-report collection."""


class SupportReportCollectionCancelled(SupportReportCollectionError):
    """Raised after cooperative collection cancellation."""


class SupportReportCollectionTimedOut(SupportReportCollectionError):
    """Raised when the isolated collector exceeds its deadline."""


class SupportReportProtocolError(SupportReportCollectionError):
    """Raised when private worker protocol data is invalid."""


class SupportReportCleanupError(SupportReportCollectionError):
    """Raised when worker or scratch cleanup cannot be confirmed."""


def _default_command(request_path: Path) -> Sequence[str]:
    return application_command("--support-report-worker", str(request_path))


def _lexical_absolute_path(value: str | Path) -> Path:
    """Make a path absolute without touching its potentially remote target."""
    raw_value = os.fspath(value)
    if not isinstance(raw_value, str) or not raw_value:
        raise ValueError("Support-report path was invalid.")
    return Path(os.path.abspath(os.path.expanduser(raw_value)))


def _validated_temp_parent(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    return _lexical_absolute_path(value)


def _encode_request(database_path: Path, data_directory: Path) -> bytes:
    record = {
        "request_version": REQUEST_VERSION,
        "database_path": str(database_path),
        "data_directory": str(data_directory),
    }
    encoded = json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload = encoded + b"\n"
    if len(payload) > MAX_REQUEST_BYTES:
        raise SupportReportProtocolError(
            "The support-report request exceeded its protocol limit."
        )
    return payload


def _write_request(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _read_bounded_regular_file(path: Path, maximum_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum_bytes
        ):
            raise SupportReportProtocolError(
                "The support-report worker returned an invalid record."
            )
        with path.open("rb") as stream:
            payload = stream.read(maximum_bytes + 1)
    except SupportReportProtocolError:
        raise
    except OSError:
        raise SupportReportProtocolError(
            "The support-report worker returned an invalid record."
        ) from None
    if len(payload) != metadata.st_size or len(payload) > maximum_bytes:
        raise SupportReportProtocolError(
            "The support-report worker returned an invalid record."
        )
    return payload


def _decode_result(payload: bytes) -> SupportReport:
    try:
        return decode_support_report_record_json(payload)
    except (ValueError, TypeError):
        raise SupportReportProtocolError(
            "The support-report worker returned an invalid record."
        ) from None


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)):
        raise SupportReportCollectionError(
            "The support-report worker command was invalid."
        )
    parts = tuple(command)
    if not parts or not all(isinstance(part, str) and part for part in parts):
        raise SupportReportCollectionError(
            "The support-report worker command was invalid."
        )
    return parts


def _cancelled(cancel_requested: CancelCallback) -> bool:
    try:
        return bool(cancel_requested())
    except Exception:
        raise SupportReportCollectionError(
            "The support-report cancellation state could not be read."
        ) from None


def _check_active(
    cancel_requested: CancelCallback,
    deadline: float,
) -> None:
    if _cancelled(cancel_requested):
        raise SupportReportCollectionCancelled(
            "Support-report collection was cancelled."
        )
    if time.monotonic() >= deadline:
        raise SupportReportCollectionTimedOut(
            "Support-report collection exceeded its time limit."
        )


def _kill_and_wait(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.kill()
        process.wait(timeout=1.0)
    except (OSError, subprocess.SubprocessError):
        return process.poll() is not None
    return process.poll() is not None


def _remove_workspace(workspace: Path) -> bool:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            shutil.rmtree(workspace)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        except OSError:
            return False


def collect_support_report(
    database_path: str | Path,
    data_dir: str | Path,
    *,
    cancel_requested: CancelCallback = lambda: False,
    timeout_seconds: float = 5.0,
    command_builder: CommandBuilder | None = None,
    safe_temp_parent: str | Path | None = None,
) -> SupportReport:
    """Collect a support report through a bounded, disposable child process."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be greater than zero")

    deadline = time.monotonic() + timeout_seconds
    _check_active(cancel_requested, deadline)
    try:
        database = _lexical_absolute_path(database_path)
        data_directory = _lexical_absolute_path(data_dir)
        parent = _validated_temp_parent(safe_temp_parent)
        request_payload = _encode_request(database, data_directory)
    except SupportReportCollectionError:
        raise
    except (OSError, TypeError, ValueError):
        raise SupportReportCollectionError(
            "The support-report paths could not be prepared."
        ) from None
    builder = command_builder or _default_command

    _check_active(cancel_requested, deadline)

    try:
        workspace = Path(
            tempfile.mkdtemp(
                prefix="automadin-support-report-",
                dir=parent,
            )
        ).resolve()
    except OSError:
        raise SupportReportCollectionError(
            "The support-report scratch workspace could not be created."
        ) from None

    request_path = workspace / REQUEST_FILE_NAME
    result_path = workspace / RESULT_FILE_NAME
    process: subprocess.Popen[bytes] | None = None
    outcome: SupportReport | SupportReportCollectionError | None = None
    try:
        _check_active(cancel_requested, deadline)
        _write_request(request_path, request_payload)
        _check_active(cancel_requested, deadline)
        command = _validate_command(builder(request_path))
        _check_active(cancel_requested, deadline)
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )

        while process.poll() is None:
            try:
                _check_active(cancel_requested, deadline)
            except (
                SupportReportCollectionCancelled,
                SupportReportCollectionTimedOut,
            ):
                if not _kill_and_wait(process):
                    raise SupportReportCleanupError(
                        "The support-report worker could not be stopped."
                    )
                raise
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

        if process.returncode != 0:
            raise SupportReportCollectionError(
                "The support-report worker exited unsuccessfully."
            )
        if _read_bounded_regular_file(request_path, MAX_REQUEST_BYTES) != request_payload:
            raise SupportReportProtocolError(
                "The support-report request was modified by the worker."
            )
        outcome = _decode_result(
            _read_bounded_regular_file(result_path, MAX_RESULT_BYTES)
        )
    except SupportReportCollectionError as exc:
        outcome = exc
    except Exception:
        outcome = SupportReportCollectionError(
            "The isolated support-report worker could not be run."
        )
    finally:
        process_cleaned = (
            process is None
            or process.poll() is not None
            or _kill_and_wait(process)
        )
        workspace_cleaned = _remove_workspace(workspace)
        if not process_cleaned or not workspace_cleaned:
            outcome = SupportReportCleanupError(
                "Support-report cleanup could not be confirmed."
            )

    if isinstance(outcome, SupportReportCollectionError):
        raise outcome
    if outcome is None:
        raise SupportReportCollectionError(
            "Support-report collection did not produce a result."
        )
    return outcome
