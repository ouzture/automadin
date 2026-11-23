"""Structured details for one flow-execution failure."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import datetime


def _safe_summary(exception: BaseException) -> str:
    """Return a concise summary suitable for the always-visible execution log."""
    public_message = getattr(exception, "public_message", None)
    if isinstance(public_message, str) and public_message.strip():
        return public_message.strip()
    if isinstance(exception, TimeoutError) or exception.__class__.__name__ == "TimeoutError":
        return "Timed out while waiting for the command to finish."
    return f"{exception.__class__.__name__} while running the command."


@dataclass(frozen=True, slots=True)
class ExecutionErrorReport:
    """Local, user-inspectable error information for a failed flow command."""

    reference: str
    summary: str
    details: str
    command_id: str | None = None
    step_id: str | None = None
    occurred_at: datetime | None = None

    @classmethod
    def from_exception(
        cls,
        exception: BaseException,
        *,
        reference: str,
        command_id: str | None = None,
        step_id: str | None = None,
    ) -> ExecutionErrorReport:
        return cls(
            reference=reference,
            summary=_safe_summary(exception),
            details="".join(
                traceback.format_exception(
                    type(exception),
                    exception,
                    exception.__traceback__,
                )
            ).rstrip(),
            command_id=command_id,
            step_id=step_id,
            occurred_at=datetime.now().astimezone(),
        )

    def log_message(self) -> str:
        if self.command_id and self.step_id:
            subject = f"[{self.step_id[:4]}] {self.command_id} —"
        else:
            subject = self.command_id or "Execution"
        return f"{subject} failed: {self.summary} (reference {self.reference})"

    def copy_text(self) -> str:
        lines = ["Automadin execution error", f"Reference: {self.reference}"]
        if self.occurred_at is not None:
            lines.append(f"Time: {self.occurred_at:%Y-%m-%d %H:%M:%S %Z}")
        if self.command_id:
            lines.append(f"Command: {self.command_id}")
        if self.step_id:
            lines.append(f"Step: {self.step_id}")
        lines.extend((f"Summary: {self.summary}", "", self.details))
        return "\n".join(lines).rstrip()
