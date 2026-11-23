"""Cooperative browser waits that keep automatic DOM hooks observable."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from automadin.execution.context import FlowContext
from automadin.models.flow import FlowCommand

_BROWSER_HOOK_POLL_MS = 500


@dataclass(frozen=True)
class BrowserTimeoutBudget:
    """One monotonic timeout budget shared by consecutive browser phases."""

    timeout_ms: int
    started_at: float = field(default_factory=time.monotonic)

    def remaining_ms(self) -> int:
        """Return Playwright-compatible remaining milliseconds.

        Playwright uses zero for no timeout.  A finite exhausted budget gets a
        final 1 ms probe so an already-completed load state can still succeed
        without granting the next phase a second full timeout.
        """
        if self.timeout_ms <= 0:
            return 0
        elapsed_ms = (time.monotonic() - self.started_at) * 1000
        return max(1, math.ceil(self.timeout_ms - elapsed_ms))


def _poll_browser_hooks(cmd: FlowCommand, ctx: FlowContext) -> None:
    executor = getattr(ctx, "_executor", None)
    dispatcher = getattr(executor, "hook_dispatcher", None)
    poll = getattr(dispatcher, "dispatch_host_before", None)
    if callable(poll):
        poll(cmd, ctx)


def wait_with_browser_hook_polling(
    cmd: FlowCommand,
    ctx: FlowContext,
    *,
    timeout_ms: int,
    operation: Callable[[int], None],
    description: str,
) -> bool:
    """Retry one non-mutating browser wait in bounded hook-aware slices.

    ``operation`` must be safe to retry after a Playwright timeout.  Mutating
    actions such as clicks must never use this helper because their outcome can
    be uncertain after a timeout.

    Returns ``False`` when the run was stopped before the wait completed.
    """
    deadline = None if timeout_ms <= 0 else time.monotonic() + timeout_ms / 1000.0
    while True:
        if bool(getattr(ctx, "is_stopped", False)):
            return False
        if deadline is None:
            slice_ms = _BROWSER_HOOK_POLL_MS
        else:
            remaining_ms = math.ceil((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise PlaywrightTimeoutError(
                    f"Timeout {timeout_ms}ms exceeded while waiting for {description}."
                )
            slice_ms = min(_BROWSER_HOOK_POLL_MS, remaining_ms)
        try:
            operation(slice_ms)
            return not bool(getattr(ctx, "is_stopped", False))
        except PlaywrightTimeoutError as exc:
            if bool(getattr(ctx, "is_stopped", False)):
                return False
            _poll_browser_hooks(cmd, ctx)
            if bool(getattr(ctx, "is_stopped", False)):
                return False
            if deadline is not None and time.monotonic() >= deadline:
                raise PlaywrightTimeoutError(
                    f"Timeout {timeout_ms}ms exceeded while waiting for {description}."
                ) from exc


def sleep_with_browser_hook_polling(
    cmd: FlowCommand,
    ctx: FlowContext,
    *,
    duration_ms: int,
) -> None:
    """Sleep interruptibly while allowing browser hooks to react promptly."""
    browser = getattr(ctx.session, "data", {}).get("browser_data")
    executor = getattr(ctx, "_executor", None)
    if browser is None or getattr(executor, "hook_dispatcher", None) is None:
        ctx.interruptible_sleep(max(0, duration_ms) / 1000.0)
        return

    deadline = time.monotonic() + max(0, duration_ms) / 1000.0
    while not bool(getattr(ctx, "is_stopped", False)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        completed = ctx.interruptible_sleep(
            min(_BROWSER_HOOK_POLL_MS / 1000.0, remaining)
        )
        if not completed:
            return
        _poll_browser_hooks(cmd, ctx)


__all__ = [
    "BrowserTimeoutBudget",
    "sleep_with_browser_hook_polling",
    "wait_with_browser_hook_polling",
]
