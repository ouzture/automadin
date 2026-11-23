"""Handlers for type_text and clear_text_field commands."""

from __future__ import annotations

from automadin.engine.device_controller import (
    TextInputCapability,
    get_device_capability,
)
from automadin.capabilities.browser.port import browser_automation_port
from automadin.execution.context import FlowContext
from automadin.models.device import BrowserDevice, WindowsDevice
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


def handle_type_text(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Type text on the current device.

    Params:
        text: the text to type.
        minDelay / min_delay: min delay between chars in ms (default: 0).
        maxDelay / max_delay: max delay between chars in ms (default: 0).
        delay_ms: fallback delay (used as both min and max). Android/Windows only.
    """
    text = ctx.interpolate(cmd.params.get("text", ""))
    # Canonical snake_case values win when both formats are present.
    resolver = getattr(ctx, "resolve_command_scalar", None)
    raw_min_delay = cmd.params.get("min_delay", cmd.params.get("minDelay", 0))
    raw_max_delay = cmd.params.get("max_delay", cmd.params.get("maxDelay", 0))
    min_delay = int(
        resolver(raw_min_delay, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_min_delay
    )
    max_delay = int(
        resolver(raw_max_delay, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_max_delay
    )
    raw_delay = cmd.params.get("delay_ms", 0)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    delay_ms = int(
        resolver(raw_delay, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_delay
    )
    # If only delay_ms is set, use it as both min and max
    if min_delay == 0 and max_delay == 0 and delay_ms > 0:
        min_delay = delay_ms
        max_delay = delay_ms
    device = ctx.session.device
    text_capability = get_device_capability(ctx.session, TextInputCapability)

    if text_capability is not None:
        text_capability.type_text(
            text,
            min_delay_ms=min_delay,
            max_delay_ms=max_delay,
        )

    elif isinstance(device, BrowserDevice):
        browser_automation_port(ctx.session.data["browser_data"]).type_text(
            text,
            min_delay_ms=min_delay,
            max_delay_ms=max_delay,
        )

    elif isinstance(device, WindowsDevice):
        win_data = ctx.session.data["windows_data"]
        from automadin.engine.windows_engine import WindowsDeviceEngine
        from automadin.engine.windows_input import type_window_text

        win_data = WindowsDeviceEngine().resolve_session_data(win_data)
        ctx.session.put_data("windows_data", win_data)
        hwnd = int(win_data["hwnd"])
        raw_focus_delay = cmd.params.get("focus_delay_ms", 200)
        raw_restore_delay = cmd.params.get("restore_delay_ms", 100)
        focus_delay_ms = int(
            resolver(raw_focus_delay, ValueType.DURATION_MS)
            if callable(resolver)
            else raw_focus_delay
        )
        restore_delay_ms = int(
            resolver(raw_restore_delay, ValueType.DURATION_MS)
            if callable(resolver)
            else raw_restore_delay
        )
        type_window_text(
            hwnd,
            text,
            delivery_mode=str(cmd.params.get("delivery_mode", "input")),
            min_delay_ms=min_delay,
            max_delay_ms=max(min_delay, max_delay),
            focus_delay_ms=focus_delay_ms,
            restore_delay_ms=restore_delay_ms,
        )

    else:
        raise RuntimeError(f"type_text: unsupported device type {type(device)}")

    if ctx.listener:
        ctx.listener.on_log(f"Typed text ({len(text)} chars)")


def handle_clear_text_field(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Clear a text field. Browser-only: uses CSS selector. Android: triple-tap + delete."""
    selector = ctx.interpolate(cmd.params.get("selector", ""))
    device = ctx.session.device
    text_capability = get_device_capability(ctx.session, TextInputCapability)

    if text_capability is not None:
        text_capability.clear_text_field()

    elif isinstance(device, BrowserDevice):
        browser_automation_port(
            ctx.session.data["browser_data"]
        ).clear_text_field(selector)

    else:
        raise RuntimeError(f"clear_text_field: unsupported device type {type(device)}")

    if ctx.listener:
        ctx.listener.on_log(f"Cleared field '{selector}'")
