"""Browser element command handlers.

The handlers use the browser session port so Playwright objects stay on their
owner thread.
"""

from __future__ import annotations

from typing import Any

from automadin.capabilities.browser.port import browser_automation_port
from automadin.execution.browser_wait import wait_with_browser_hook_polling
from automadin.execution.context import FlowContext
from automadin.execution.runtime_diagnostics import (
    format_runtime_value,
    variable_display_name,
)
from automadin.models.flow import FlowCommand
from automadin.models.browser_dom import BrowserDomScope, BrowserElementLoopItem
from automadin.models.locator import normalize_locator
from automadin.models.typed_value import ValueType
from automadin.execution.flow_values import RuntimeTypedValue


def _interpolate_locator(value: Any, ctx: FlowContext) -> Any:
    if isinstance(value, str):
        return ctx.interpolate(value)
    if isinstance(value, dict):
        return {key: _interpolate_locator(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate_locator(item, ctx) for item in value]
    return value


def _resolve_duration_ms(
    cmd: FlowCommand, ctx: FlowContext, name: str, default: int = 0
) -> int:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_value
    )
    return int(value)


def _click_geometry(cmd: FlowCommand, ctx: FlowContext) -> dict[str, object]:
    resolver = getattr(ctx, "resolve_command_scalar", None)

    def number(name: str) -> float:
        raw = cmd.params.get(name, 0)
        value = resolver(raw, ValueType.NUMBER) if callable(resolver) else raw
        return float(value)

    geometry: dict[str, object] = {
        "anchor": str(cmd.params.get("click_anchor", "center")),
        "offset_x": number("offset_x"),
        "offset_y": number("offset_y"),
        "offset_unit": str(cmd.params.get("offset_unit", "pixels")),
        "require_new_child_tab": bool(
            ctx.resolve_command_scalar(
                cmd.params.get("require_new_child_tab", False),
                ValueType.BOOLEAN,
            )
            if callable(getattr(ctx, "resolve_command_scalar", None))
            else cmd.params.get("require_new_child_tab", False)
        ),
    }
    if geometry == {
        "anchor": "center",
        "offset_x": 0.0,
        "offset_y": 0.0,
        "offset_unit": "pixels",
        "require_new_child_tab": False,
    }:
        return {}
    return geometry


def handle_click_element(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Click a structured browser locator through the owner-thread handle."""
    raw_timeout = cmd.params.get("timeout", 5000)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout_ms = int(
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    browser = browser_automation_port(ctx.session.data["browser_data"])
    geometry = _click_geometry(cmd, ctx)

    if cmd.params.get("target", "locator") == "current_element":
        current = ctx.current_browser_element
        if current is None:
            raise RuntimeError(
                "Click Element can target the current element only inside "
                "For Each Browser Element"
            )
        browser.click_snapshot_locator(
            current.snapshot_token,
            current.index,
            timeout_ms=timeout_ms,
            **geometry,
        )
        if ctx.listener:
            ctx.listener.on_log(
                f"Clicked browser collection element {current.index + 1}/{current.count}"
            )
        return

    raw_locator = cmd.params.get("locator", cmd.params.get("selector", ""))
    locator = normalize_locator(_interpolate_locator(raw_locator, ctx))

    browser.click_locator(locator, timeout_ms=timeout_ms, **geometry)
    if ctx.listener:
        ctx.listener.on_log("Clicked browser element")


def handle_for_each_browser_element(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Snapshot a scoped DOM collection and execute children once per element."""
    locator = _locator(cmd, ctx)
    raw_container = cmd.params.get("container_locator", "")
    scope = BrowserDomScope.parse(
        normalize_locator(_interpolate_locator(raw_container, ctx))
        if raw_container not in (None, "", {})
        else None
    )
    browser = browser_automation_port(ctx.session.data["browser_data"])
    snapshot = browser.snapshot_locators(locator, scope=scope)

    try:
        for index in range(snapshot.count):
            if ctx.is_stopped:
                break
            trace = getattr(ctx, "log_trace", None)
            if callable(trace):
                trace(
                    "Browser element loop: "
                    f"iteration {index + 1}/{snapshot.count}; index={index}"
                )
            item = BrowserElementLoopItem(snapshot.token, index, snapshot.count)
            with ctx.browser_element_scope(item):
                if ctx.flow_values is None:
                    ctx.set_variable("element_index", str(index))
                    ctx.set_variable("element_count", str(snapshot.count))
                    ctx._executor._execute_children(cmd.children, ctx)
                else:
                    with ctx.flow_values.builtin_scope(
                        {
                            "element_index": RuntimeTypedValue(ValueType.INTEGER, index),
                            "element_count": RuntimeTypedValue(
                                ValueType.INTEGER,
                                snapshot.count,
                            ),
                        }
                    ):
                        ctx._executor._execute_children(cmd.children, ctx)
    finally:
        browser.release_locator_snapshot(snapshot.token)
    if ctx.listener:
        ctx.listener.on_log(
            f"Browser element collection completed ({snapshot.count} elements)"
        )


def _locator(cmd: FlowCommand, ctx: FlowContext) -> dict[str, Any]:
    raw = cmd.params.get("locator", cmd.params.get("selector", ""))
    return normalize_locator(_interpolate_locator(raw, ctx))


def handle_wait_for_selector(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Wait for a legacy CSS or structured browser locator."""
    locator = _locator(cmd, ctx)
    raw_timeout = cmd.params.get("timeout", 5000)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout_ms = int(
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    browser = browser_automation_port(ctx.session.data["browser_data"])

    def operation(slice_ms: int) -> None:
        browser.wait_for_locator(locator, timeout_ms=slice_ms)
    completed = wait_with_browser_hook_polling(
        cmd,
        ctx,
        timeout_ms=timeout_ms,
        operation=operation,
        description="browser element",
    )
    if not completed:
        return
    if ctx.listener:
        ctx.listener.on_log("Browser element found")


def handle_scroll_browser(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Scroll the page or bring a located browser element into view."""
    browser = browser_automation_port(ctx.session.data["browser_data"])
    target = str(cmd.params.get("target", "page"))
    timeout_ms = _resolve_duration_ms(cmd, ctx, "timeout", 5000)
    if target == "page":
        resolver = getattr(ctx, "resolve_command_scalar", None)

        def integer(name: str, default: int) -> int:
            raw = cmd.params.get(name, default)
            value = resolver(raw, ValueType.INTEGER) if callable(resolver) else raw
            return int(value)

        delta_x = integer("delta_x", 0)
        delta_y = integer("delta_y", 600)
        browser.scroll_page(delta_x=delta_x, delta_y=delta_y)
        if ctx.listener:
            ctx.listener.on_log(f"Scrolled browser page by ({delta_x}, {delta_y}) px")
        return

    if target == "current_element":
        current = ctx.current_browser_element
        if current is None:
            raise RuntimeError(
                "Scroll Browser can target the current element only inside "
                "For Each Browser Element"
            )
        browser.scroll_snapshot_locator_into_view(
            current.snapshot_token,
            current.index,
            timeout_ms=timeout_ms,
        )
    elif target == "locator":
        locator = _locator(cmd, ctx)
        browser.scroll_locator_into_view(locator, timeout_ms=timeout_ms)
    else:
        raise ValueError(f"Unsupported browser scroll target: {target}")
    if ctx.listener:
        ctx.listener.on_log("Scrolled browser element into view")


def handle_input_fill(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Fill a legacy CSS or structured browser locator."""
    locator = _locator(cmd, ctx)
    value = ctx.interpolate(cmd.params.get("value", ""))
    min_delay = _resolve_duration_ms(
        cmd,
        ctx,
        "min_delay",
        cmd.params.get("minDelay", 0),
    )
    max_delay = _resolve_duration_ms(
        cmd,
        ctx,
        "max_delay",
        cmd.params.get("maxDelay", 0),
    )
    delay_ms = _resolve_duration_ms(cmd, ctx, "delay_ms")
    if min_delay == 0 and max_delay == 0 and delay_ms > 0:
        min_delay = max_delay = delay_ms
    browser = browser_automation_port(ctx.session.data["browser_data"])
    browser.fill_locator(
        locator,
        value,
        min_delay=min_delay,
        max_delay=max_delay,
    )
    if ctx.listener:
        ctx.listener.on_log("Filled browser element")


def handle_extract_text(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Extract text from a legacy CSS or structured browser locator."""
    variable = cmd.params.get("variable", "result")
    browser = browser_automation_port(ctx.session.data["browser_data"])
    if cmd.params.get("target", "locator") == "current_element":
        current = ctx.current_browser_element
        if current is None:
            raise RuntimeError(
                "Extract Text can target the current element only inside "
                "For Each Browser Element"
            )
        child_selector = ctx.interpolate(cmd.params.get("child_selector", "")).strip()
        value = browser.extract_snapshot_locator_text(
            current.snapshot_token,
            current.index,
            child_selector=child_selector,
        )
    else:
        locator = _locator(cmd, ctx)
        value = browser.extract_locator_text(locator)
    assigned = ctx.set_output_variable(variable, value)
    if ctx.listener:
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
            f"Extracted browser text into Variable '{variable_name}'{detail}"
        )


def handle_assert_visible(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Assert visibility for a legacy CSS or structured browser locator."""
    locator = _locator(cmd, ctx)
    browser = browser_automation_port(ctx.session.data["browser_data"])
    browser.assert_locator_visible(locator)
    if ctx.listener:
        ctx.listener.on_log("Browser element is visible")
