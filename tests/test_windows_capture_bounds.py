from __future__ import annotations

import sys
from pathlib import Path

import pytest

from automadin.services.windows_capture import (
    WindowsCaptureError,
    WindowsCaptureTimedOut,
    capture_window,
)


def _worker_command(script: str):
    def build(_hwnd: int, output_path: Path) -> tuple[str, ...]:
        return (
            sys.executable,
            "-c",
            script,
            str(output_path),
        )

    return build


def test_isolated_capture_returns_bounded_payload() -> None:
    payload = b"\x89PNG\r\n\x1a\nbounded"
    command = _worker_command(
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes("
        + repr(payload)
        + ")"
    )

    assert capture_window(123, command_builder=command) == payload


def test_isolated_capture_reports_worker_failure() -> None:
    with pytest.raises(WindowsCaptureError, match="worker failed"):
        capture_window(
            123,
            command_builder=_worker_command("raise SystemExit(7)"),
        )


def test_isolated_capture_hard_timeout_stops_worker() -> None:
    with pytest.raises(WindowsCaptureTimedOut, match="within 0.1 seconds"):
        capture_window(
            123,
            timeout=0.1,
            command_builder=_worker_command("import time; time.sleep(30)"),
        )
