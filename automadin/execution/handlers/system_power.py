"""Handlers for power actions that affect the local Automadin host."""

from __future__ import annotations

import subprocess
import sys

from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowTerminate
from automadin.models.flow import FlowCommand
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


def handle_hibernate_computer(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Ask Windows to hibernate the local computer and end the current flow."""
    del cmd
    if sys.platform != "win32":
        raise RuntimeError("Hibernate Computer requires Windows")

    try:
        result = subprocess.run(
            ["shutdown.exe", "/h"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Windows could not start hibernation. Check that hibernation is "
            "supported and enabled."
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        message = (
            "Windows rejected the hibernation request. Check that hibernation "
            "is supported and enabled."
        )
        if detail:
            message = f"{message} Windows reported: {detail}"
        raise RuntimeError(message)

    # A FlowTerminate raised inside Run Flow only exits that child flow. Mark the
    # shared context stopped as well so no parent flow resumes after wake.
    ctx.stop()
    if ctx.listener:
        ctx.listener.on_log(
            "Windows accepted the hibernation request; ending automation run"
        )
    raise FlowTerminate()


__all__ = ["handle_hibernate_computer"]
