"""Thread-safe execution contract for user-attention checkpoints.

The runtime object deliberately contains no Qt/UI dependency. Interactive
listeners may present Retry/Continue/Stop, while non-interactive listeners
resolve it immediately to ``STOP`` and record the reason.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automadin.execution.context import FlowContext
    from automadin.models.flow import FlowCommand


class AttentionResolution(StrEnum):
    RETRY = "retry"
    CONTINUE = "continue"
    STOP = "stop"


_DEFAULT_ATTENTION_RESOLUTIONS = (
    AttentionResolution.RETRY,
    AttentionResolution.CONTINUE,
    AttentionResolution.STOP,
)


class RequiredVisualCondition(RuntimeError):
    """A configured visual prerequisite that must fail the flow immediately."""


@dataclass(slots=True)
class AttentionRequest:
    """One pending human decision owned by an executing flow thread."""

    message: str
    command_id: str
    step_id: str
    allowed_resolutions: tuple[AttentionResolution, ...] = _DEFAULT_ATTENTION_RESOLUTIONS
    _ready: threading.Event = field(default_factory=threading.Event, init=False)
    _resolution: AttentionResolution | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        allowed = tuple(AttentionResolution(value) for value in self.allowed_resolutions)
        if not allowed:
            raise ValueError("An attention request needs at least one resolution.")
        if AttentionResolution.STOP not in allowed:
            raise ValueError("An attention request must allow Stop.")
        if len(set(allowed)) != len(allowed):
            raise ValueError("Attention request resolutions must be unique.")
        self.allowed_resolutions = allowed

    @property
    def resolution(self) -> AttentionResolution | None:
        return self._resolution

    def resolve(self, resolution: AttentionResolution | str) -> None:
        if self._ready.is_set():
            return
        resolved = AttentionResolution(resolution)
        if resolved not in self.allowed_resolutions:
            raise ValueError(f"Attention resolution '{resolved}' is not allowed.")
        self._resolution = resolved
        self._ready.set()

    def wait(self, *, stopped: threading.Event) -> AttentionResolution:
        """Wait in short intervals so a flow Stop always unblocks the request."""
        while not self._ready.wait(0.1):
            if stopped.is_set():
                return AttentionResolution.STOP
        return self._resolution or AttentionResolution.STOP


def _resolve_visual_policy(
    ctx: FlowContext,
    command: FlowCommand,
    default_message: str,
    *,
    behavior_name: str,
    message_name: str,
    invalid_setting_message: str,
) -> AttentionResolution:
    behavior = str(command.params.get(behavior_name, "normal")).casefold()
    message = str(command.params.get(message_name, "")).strip()
    message = ctx.interpolate(message) if message else default_message
    if behavior == "normal":
        return AttentionResolution.CONTINUE
    if behavior == "fail":
        raise RequiredVisualCondition(message)
    if behavior == "attention":
        return ctx.request_attention(
            message, command_id=command.id, step_id=command.step_id
        )
    raise RequiredVisualCondition(invalid_setting_message)


def resolve_not_found_policy(
    ctx: FlowContext,
    command: FlowCommand,
    default_message: str,
) -> AttentionResolution:
    """Resolve a Click/Find Image not-found policy without importing UI code."""
    return _resolve_visual_policy(
        ctx,
        command,
        default_message,
        behavior_name="not_found_behavior",
        message_name="not_found_message",
        invalid_setting_message="Image command has an invalid When not found setting.",
    )


def resolve_condition_unmet_policy(
    ctx: FlowContext,
    command: FlowCommand,
    default_message: str,
) -> AttentionResolution:
    """Resolve an image-condition policy without treating ``not_found`` as an error."""
    return _resolve_visual_policy(
        ctx,
        command,
        default_message,
        behavior_name="condition_unmet_behavior",
        message_name="condition_unmet_message",
        invalid_setting_message=(
            "Image condition command has an invalid When Condition Is Unmet setting."
        ),
    )


__all__ = [
    "AttentionRequest",
    "AttentionResolution",
    "RequiredVisualCondition",
    "resolve_condition_unmet_policy",
    "resolve_not_found_policy",
]
