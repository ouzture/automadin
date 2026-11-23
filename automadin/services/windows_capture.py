"""Bounded process supervisor for native Windows window capture."""

from __future__ import annotations

import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from automadin.runtime_components import application_command
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS

_DEFAULT_TIMEOUT_SECONDS = 5.0
_CLEANUP_TIMEOUT_SECONDS = 5.0
_MAX_CAPTURE_BYTES = 64 * 1024 * 1024

CommandBuilder = Callable[[int, Path], Sequence[str]]


class WindowsCaptureError(RuntimeError):
    """A native Windows window capture failed."""


class WindowsCaptureTimedOut(WindowsCaptureError):
    """A native capture exceeded its hard deadline."""


class WindowsCaptureCleanupError(WindowsCaptureError):
    """The isolated capture process could not be stopped."""


def _default_command(hwnd: int, output_path: Path) -> Sequence[str]:
    return application_command(
        "--windows-capture-worker",
        str(hwnd),
        str(output_path),
    )


def _stop_process(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return False
    return process.poll() is not None


def _read_capture(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise WindowsCaptureError(
            "The isolated Windows capture did not produce an image."
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_CAPTURE_BYTES
    ):
        raise WindowsCaptureError(
            "The isolated Windows capture produced an invalid image payload."
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WindowsCaptureError(
            "The isolated Windows capture image could not be read."
        ) from exc


def capture_window(
    hwnd: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    command_builder: CommandBuilder = _default_command,
) -> bytes:
    """Capture ``hwnd`` in a killable helper process and return PNG bytes."""
    if hwnd <= 0:
        raise ValueError("A positive Windows window handle is required.")
    if timeout <= 0:
        raise ValueError("Windows capture timeout must be positive.")

    with tempfile.TemporaryDirectory(prefix="automadin-window-capture-") as raw_dir:
        output_path = Path(raw_dir) / "capture.png"
        command = tuple(command_builder(hwnd, output_path))
        if not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise ValueError(
                "Windows capture worker command must contain non-empty strings."
            )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if not _stop_process(process):
                raise WindowsCaptureCleanupError(
                    "The timed-out Windows capture worker could not be stopped."
                ) from exc
            raise WindowsCaptureTimedOut(
                f"Windows capture did not finish within {timeout:g} seconds."
            ) from exc
        except BaseException:
            if not _stop_process(process):
                raise WindowsCaptureCleanupError(
                    "The Windows capture worker could not be stopped."
                )
            raise

        if return_code != 0:
            raise WindowsCaptureError(
                "The isolated Windows capture worker failed."
            )
        return _read_capture(output_path)
