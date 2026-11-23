from __future__ import annotations

from automadin.capabilities.browser.port import browser_automation_port
from automadin.execution.browser_wait import (
    BrowserTimeoutBudget,
    wait_with_browser_hook_polling,
)
from automadin.execution.context import FlowContext
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType

_NAVIGATION_COMPLETE_TIMEOUT_MS = 30000


def _navigation_timeout(cmd: FlowCommand, ctx: FlowContext) -> int:
    raw = cmd.params.get("timeout", _NAVIGATION_COMPLETE_TIMEOUT_MS)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    return int(
        resolver(raw, ValueType.DURATION_MS)
        if callable(resolver)
        else raw
    )


def _wait_for_dom_content(
    cmd: FlowCommand,
    ctx: FlowContext,
    *,
    timeout_ms: int,
) -> bool:
    browser = browser_automation_port(ctx.session.data["browser_data"])
    return wait_with_browser_hook_polling(
        cmd,
        ctx,
        timeout_ms=timeout_ms,
        operation=lambda slice_ms: browser.wait_for_load_state(
            "domcontentloaded", timeout_ms=slice_ms
        ),
        description="page DOM content",
    )


def handle_navigate_to(cmd: FlowCommand, ctx: FlowContext) -> None:
    url = ctx.interpolate(cmd.params.get("url", ""))
    browser = browser_automation_port(ctx.session.data["browser_data"])
    timeout_ms = _navigation_timeout(cmd, ctx)
    budget = BrowserTimeoutBudget(timeout_ms)
    browser.navigate_to(url, timeout_ms=budget.remaining_ms())
    if not _wait_for_dom_content(cmd, ctx, timeout_ms=budget.remaining_ms()):
        return
    if ctx.listener:
        ctx.listener.on_log(f"Navigated to {url}")


def handle_go_back(cmd: FlowCommand, ctx: FlowContext) -> None:
    browser = browser_automation_port(ctx.session.data["browser_data"])
    timeout_ms = _navigation_timeout(cmd, ctx)
    budget = BrowserTimeoutBudget(timeout_ms)
    browser.go_back(timeout_ms=budget.remaining_ms())
    if not _wait_for_dom_content(cmd, ctx, timeout_ms=budget.remaining_ms()):
        return
    if ctx.listener:
        ctx.listener.on_log("Navigated back")


def handle_go_forward(cmd: FlowCommand, ctx: FlowContext) -> None:
    browser = browser_automation_port(ctx.session.data["browser_data"])
    timeout_ms = _navigation_timeout(cmd, ctx)
    budget = BrowserTimeoutBudget(timeout_ms)
    browser.go_forward(timeout_ms=budget.remaining_ms())
    if not _wait_for_dom_content(cmd, ctx, timeout_ms=budget.remaining_ms()):
        return
    if ctx.listener:
        ctx.listener.on_log("Navigated forward")


def handle_reload_page(cmd: FlowCommand, ctx: FlowContext) -> None:
    browser = browser_automation_port(ctx.session.data["browser_data"])
    timeout_ms = _navigation_timeout(cmd, ctx)
    budget = BrowserTimeoutBudget(timeout_ms)
    browser.reload_page(timeout_ms=budget.remaining_ms())
    if not _wait_for_dom_content(cmd, ctx, timeout_ms=budget.remaining_ms()):
        return
    if ctx.listener:
        ctx.listener.on_log("Page reloaded")
