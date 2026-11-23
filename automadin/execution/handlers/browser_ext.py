from __future__ import annotations

from automadin.capabilities.browser.port import browser_automation_port
from automadin.execution.browser_wait import wait_with_browser_hook_polling
from automadin.execution.context import FlowContext
from automadin.execution.runtime_diagnostics import (
    format_runtime_value,
    variable_display_name,
)
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


def handle_evaluate_js(cmd: FlowCommand, ctx: FlowContext) -> None:
    script = ctx.interpolate(cmd.params.get("script", ""))
    variable = cmd.params.get("variable", "")
    browser = browser_automation_port(ctx.session.data["browser_data"])
    result = browser.evaluate_js(script)
    assigned = None
    if variable:
        assigned = ctx.set_output_variable(variable, result)
    if ctx.listener:
        if not variable:
            ctx.listener.on_log("Evaluated JS without storing its result")
            return
        flow_values = getattr(ctx, "flow_values", None)
        variable_name = variable_display_name(
            variable,
            getattr(flow_values, "catalog", None),
        )
        detail = (
            f" = {format_runtime_value(assigned)}"
            if cmd.params.get("log_values", False) is True
            else "; value hidden"
        )
        ctx.listener.on_log(
            f"Evaluated JS into Variable '{variable_name}'{detail}"
        )


def handle_assert_url_contains(cmd: FlowCommand, ctx: FlowContext) -> None:
    text = ctx.interpolate(cmd.params.get("text", ""))
    browser = browser_automation_port(ctx.session.data["browser_data"])
    if text not in browser.current_url():
        raise AssertionError(
            f"Expected URL to contain {text!r}, got {browser.current_url()!r}"
        )
    if ctx.listener:
        ctx.listener.on_log(f"Assert URL contains '{text}' OK")


def handle_close_all_tabs(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Replace all current tabs with one fresh active tab."""
    browser_automation_port(
        ctx.session.data["browser_data"]
    ).close_all_tabs()
    if ctx.listener:
        ctx.listener.on_log("Closed all previous tabs and opened a fresh tab")


def handle_wait_for_navigation(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Wait for a page navigation to complete.

    Params:
        timeout: max wait time in ms (default: 30000).
    """
    raw_timeout = cmd.params.get("timeout", 30000)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout = int(
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    browser = browser_automation_port(ctx.session.data["browser_data"])
    completed = wait_with_browser_hook_polling(
        cmd,
        ctx,
        timeout_ms=timeout,
        operation=lambda slice_ms: browser.wait_for_load_state(
            "networkidle", timeout_ms=slice_ms
        ),
        description="navigation",
    )
    if not completed:
        return
    if ctx.listener:
        ctx.listener.on_log("Navigation completed")
