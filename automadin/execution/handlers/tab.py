from __future__ import annotations

from automadin.capabilities.browser.port import browser_automation_port
from automadin.execution.browser_wait import (
    BrowserTimeoutBudget,
    wait_with_browser_hook_polling,
)
from automadin.execution.context import FlowContext
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


def _resolve_integer(cmd: FlowCommand, ctx: FlowContext, name: str, default: int) -> int:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.INTEGER)
        if callable(resolver)
        else raw_value
    )
    return int(value)


def _resolve_boolean(cmd: FlowCommand, ctx: FlowContext, name: str, default: bool) -> bool:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_value
    )
    return bool(value)


def _resolve_duration(cmd: FlowCommand, ctx: FlowContext, name: str, default: int) -> int:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_value
    )
    return int(value)


def handle_open_new_tab(cmd: FlowCommand, ctx: FlowContext) -> None:
    url = ctx.interpolate(cmd.params.get("url", ""))
    browser = browser_automation_port(ctx.session.data["browser_data"])
    timeout_ms = _resolve_duration(cmd, ctx, "timeout", 30000)
    budget = BrowserTimeoutBudget(timeout_ms)
    browser.open_new_tab(url or None, timeout_ms=budget.remaining_ms())
    if url and not wait_with_browser_hook_polling(
        cmd,
        ctx,
        timeout_ms=budget.remaining_ms(),
        operation=lambda slice_ms: browser.wait_for_load_state(
            "domcontentloaded", timeout_ms=slice_ms
        ),
        description="new tab DOM content",
    ):
        return
    if ctx.listener:
        ctx.listener.on_log(
            f"Opened a new tab{' → ' + url if url else ''}"
        )


def handle_close_tab(cmd: FlowCommand, ctx: FlowContext) -> None:
    browser = browser_automation_port(ctx.session.data["browser_data"])
    result = browser.close_tab()
    if ctx.listener:
        if result is None:
            message = "Close skipped — only one tab remains"
        elif result.opened_replacement:
            message = "Closed the current tab and opened a fresh tab"
        else:
            message = "Closed the current tab"
        ctx.listener.on_log(message)


def handle_switch_tab(cmd: FlowCommand, ctx: FlowContext) -> None:
    index = _resolve_integer(cmd, ctx, "index", 0)
    browser = browser_automation_port(ctx.session.data["browser_data"])
    browser.switch_tab(index)
    if ctx.listener:
        ctx.listener.on_log(f"Selected Automadin tab position {index + 1}")


def handle_select_browser_page(cmd: FlowCommand, ctx: FlowContext) -> None:
    strategy = str(cmd.params.get("strategy", "newest"))
    value = ctx.interpolate(cmd.params.get("value", ""))
    browser = browser_automation_port(ctx.session.data["browser_data"])
    browser.select_page(
        strategy, value,
        match_case=_resolve_boolean(cmd, ctx, "match_case", False),
    )
    if ctx.listener:
        ctx.listener.on_log(f"Selected browser page by {strategy}")


def handle_ensure_browser_page(cmd: FlowCommand, ctx: FlowContext) -> None:
    url = ctx.interpolate(cmd.params.get("url", "")).strip()
    browser = browser_automation_port(ctx.session.data["browser_data"])
    timeout_ms = _resolve_duration(cmd, ctx, "timeout", 30000)
    budget = BrowserTimeoutBudget(timeout_ms)
    result = browser.ensure_page(url or None, timeout_ms=budget.remaining_ms())
    if result.created and url and not wait_with_browser_hook_polling(
        cmd,
        ctx,
        timeout_ms=budget.remaining_ms(),
        operation=lambda slice_ms: browser.wait_for_load_state(
            "domcontentloaded", timeout_ms=slice_ms
        ),
        description="replacement page DOM content",
    ):
        return
    if ctx.listener:
        action = "opened a replacement page" if result.created else "page is available"
        ctx.listener.on_log(f"Browser lifecycle: {action}")
