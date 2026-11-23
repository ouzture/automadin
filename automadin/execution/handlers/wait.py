from __future__ import annotations

import random

from automadin.execution.browser_wait import sleep_with_browser_hook_polling
from automadin.execution.context import FlowContext
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


def handle_wait(cmd: FlowCommand, ctx: FlowContext) -> None:
    ms = int(
        ctx.resolve_command_scalar(
            cmd.params.get("ms", 500),
            ValueType.DURATION_MS,
        )
    )
    if ctx.listener:
        ctx.listener.on_log(f"Waiting {ms}ms")
    sleep_with_browser_hook_polling(cmd, ctx, duration_ms=ms)


def handle_random_wait(cmd: FlowCommand, ctx: FlowContext) -> None:
    resolver = getattr(ctx, "resolve_command_scalar", None)
    raw_min = cmd.params.get("min_ms", cmd.params.get("minMs", 500))
    raw_max = cmd.params.get("max_ms", cmd.params.get("maxMs", 2000))
    min_ms = int(
        resolver(raw_min, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_min
    )
    max_ms = int(
        resolver(raw_max, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_max
    )
    ms = random.randint(min_ms, max_ms)
    if ctx.listener:
        ctx.listener.on_log(f"Waiting {ms}ms (random)")
    sleep_with_browser_hook_polling(cmd, ctx, duration_ms=ms)
