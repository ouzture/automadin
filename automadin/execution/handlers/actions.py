"""Handlers for click_image, swipe, and press_keycode commands."""

from __future__ import annotations

from automadin.engine.device_controller import (
    KeyInputCapability,
    SwipeCapability,
    TapCapability,
    get_device_capability,
)
from automadin.capabilities.browser.port import browser_automation_port
from automadin.execution.coordinates import get_surface_size, resolve_offset, resolve_point
from automadin.execution.context import FlowContext
from automadin.execution.attention import AttentionResolution, resolve_not_found_policy
from automadin.execution.executor import CommandNotMatched
from automadin.execution.handlers.detection import (
    _find_any_image,
    _resolve_image_names,
    _take_screenshot_for_device,
)
from automadin.models.device import BrowserDevice, WindowsDevice
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


def _resolve_number(cmd: FlowCommand, ctx: FlowContext, name: str, default: float) -> float:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.NUMBER)
        if callable(resolver)
        else raw_value
    )
    return float(value)


def _resolve_boolean(cmd: FlowCommand, ctx: FlowContext, name: str, default: bool) -> bool:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_value
    )
    return bool(value)


def _resolve_duration_ms(cmd: FlowCommand, ctx: FlowContext, name: str, default: int) -> int:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_value
    )
    return int(value)


def _perform_click_at(
    ctx: FlowContext,
    x: int,
    y: int,
    click_type: str = "single",
    hold_duration_ms: int = 0,
) -> None:
    """Click or press-and-hold at a resolved coordinate on the active device."""
    hold_duration_ms = max(0, int(hold_duration_ms))
    device = ctx.session.device
    tap_capability = get_device_capability(ctx.session, TapCapability)

    if tap_capability is not None:
        tap_capability.tap(
            x,
            y,
            click_type=click_type,
            hold_duration_ms=hold_duration_ms,
        )
    elif isinstance(device, BrowserDevice):
        browser_automation_port(ctx.session.data["browser_data"]).click_position(
            x,
            y,
            click_type=click_type,
            hold_duration_ms=hold_duration_ms,
        )
    elif isinstance(device, WindowsDevice):
        from automadin.engine.windows_input import click_window
        from automadin.engine.windows_engine import WindowsDeviceEngine

        win_data = WindowsDeviceEngine().resolve_session_data(
            ctx.session.data["windows_data"]
        )
        click_window(
            int(win_data["hwnd"]),
            x,
            y,
            click_type=click_type,
            hold_duration_ms=hold_duration_ms,
            mode="message",
        )
    else:
        raise RuntimeError(f"Unsupported device type for click action: {type(device)}")


def handle_click_image(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Find an image on screen and click its center.

    Params:
        images: list of template image names (Java format).
        image_name: single template image name.
        threshold: match confidence 0.0-1.0 (default: 0.90).
        click: click type - "single" or "double" (default: "single").
        hold_duration_ms: hold press duration in milliseconds (default: 0).
        xmargin: X offset added to click position (default: 0).
        ymargin: Y offset added to click position (default: 0).
    """
    image_names = _resolve_image_names(cmd, ctx)
    threshold = _resolve_number(cmd, ctx, "threshold", 0.90)
    click_type = cmd.params.get("click", "single")
    hold_duration_ms = _resolve_duration_ms(cmd, ctx, "hold_duration_ms", 0)
    xmargin, ymargin = resolve_offset(cmd, ctx, value_type=ValueType.INTEGER)
    color_match = _resolve_boolean(cmd, ctx, "color_match", False)
    color_tolerance = _resolve_number(cmd, ctx, "color_tolerance", 0.3)

    if not image_names:
        raise RuntimeError("click_image: 'images' or 'image_name' parameter is required")

    while True:
        screenshot = _take_screenshot_for_device(ctx)
        if screenshot is None:
            raise RuntimeError("click_image: failed to take screenshot")
        ctx._last_screenshot = screenshot

        matched_name, result = _find_any_image(
            ctx,
            image_names,
            screenshot,
            threshold,
            color_match,
            color_tolerance,
        )
        if result is not None:
            break
        resolution = resolve_not_found_policy(
            ctx,
            cmd,
            f"Could not find {', '.join(image_names)}. Make it visible, then choose Retry.",
        )
        if resolution is AttentionResolution.RETRY and not ctx.is_stopped:
            continue
        if resolution is AttentionResolution.STOP:
            ctx.stop()
            return
        raise CommandNotMatched(
            f"click_image: none of {image_names} found on screen"
        )

    click_x = result.center_x + xmargin
    click_y = result.center_y + ymargin
    _perform_click_at(
        ctx,
        click_x,
        click_y,
        click_type=click_type,
        hold_duration_ms=hold_duration_ms,
    )

    if ctx.listener:
        action = (
            f"held image '{matched_name}' for {hold_duration_ms}ms"
            if hold_duration_ms > 0
            else f"clicked image '{matched_name}'"
        )
        ctx.listener.on_log(f"{action} at ({click_x}, {click_y})")


def handle_swipe(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Perform a swipe gesture.

    Coordinates are percentages by default, or pixels when coordinate_unit is
    set to "pixels". Legacy percentage values without coordinate_unit accept
    both 0.0-1.0 and 0-100 scales.

    Params:
        coordinate_unit: "percent" or "pixels" (default: "percent").
        start_x, start_y: start position.
        end_x, end_y: end position.
        duration_ms: swipe duration in ms (default: 300).
    """
    duration = _resolve_duration_ms(
        cmd,
        ctx,
        "duration_ms",
        cmd.params.get("duration", 300),
    )

    device = ctx.session.device
    swipe_capability = get_device_capability(ctx.session, SwipeCapability)

    if swipe_capability is not None:
        surface_size = get_surface_size(ctx)
        sx, sy = resolve_point(
            cmd,
            ctx,
            ("start_x", "startXPercent"),
            ("start_y", "startYPercent"),
            default_unit="percent",
            surface_size=surface_size,
            value_type=ValueType.NUMBER,
        )
        ex, ey = resolve_point(
            cmd,
            ctx,
            ("end_x", "endXPercent"),
            ("end_y", "endYPercent"),
            default_unit="percent",
            surface_size=surface_size,
            value_type=ValueType.NUMBER,
        )

        swipe_capability.swipe(
            sx,
            sy,
            ex,
            ey,
            duration_ms=duration,
        )
    elif isinstance(device, BrowserDevice):
        surface_size = get_surface_size(ctx)
        sx, sy = resolve_point(
            cmd,
            ctx,
            ("start_x", "startXPercent"),
            ("start_y", "startYPercent"),
            default_unit="percent",
            surface_size=surface_size,
            value_type=ValueType.NUMBER,
        )
        ex, ey = resolve_point(
            cmd,
            ctx,
            ("end_x", "endXPercent"),
            ("end_y", "endYPercent"),
            default_unit="percent",
            surface_size=surface_size,
            value_type=ValueType.NUMBER,
        )

        browser_automation_port(ctx.session.data["browser_data"]).swipe(
            sx, sy, ex, ey, duration_ms=duration
        )
    else:
        raise RuntimeError("swipe: not supported for this device type")

    if ctx.listener:
        unit = cmd.params.get("coordinate_unit", "percent")
        ctx.listener.on_log(
            f"Swiped ({sx},{sy}) -> ({ex},{ey}) [{unit}] duration={duration}ms"
        )


_ANDROID_KEYCODE_MAP: dict[int, str] = {
    3: "Home", 4: "Backspace", 24: "AudioVolumeUp", 25: "AudioVolumeDown",
    26: "Power", 27: "Camera", 66: "Enter", 67: "Backspace",
    82: "ContextMenu", 84: "Search", 111: "Escape",
    187: "Meta",
}

# Playwright key names are case-sensitive.  Map common upper/mixed variants
# (including Java KEYCODE_* names) to the exact Playwright key name.
_KEY_NORMALIZE: dict[str, str] = {
    k.upper(): k
    for k in [
        "Enter", "Tab", "Escape", "Backspace", "Delete", "Space",
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        "Home", "End", "PageUp", "PageDown", "Insert",
        "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
        "Control", "Shift", "Alt", "Meta",
        "CapsLock", "NumLock", "ScrollLock", "Pause", "PrintScreen",
        "ContextMenu",
    ]
}


def _normalize_key(key: str) -> str:
    """Normalise a key name to the exact casing Playwright expects."""
    # Strip Java KEYCODE_ prefix if present (e.g. "KEYCODE_ENTER" → "ENTER")
    if key.startswith("KEYCODE_"):
        key = key[8:]
    return _KEY_NORMALIZE.get(key.upper(), key)


def _resolve_keys(cmd: FlowCommand, ctx: FlowContext) -> list[str]:
    """Extract key(s) from command params.

    Supports:
      - ``keys``: list of key names (Python format from key_list widget)
      - ``keycode``: single key name or Android int (Java legacy format)
    """
    keys = cmd.params.get("keys")
    if isinstance(keys, list) and keys:
        return [_normalize_key(ctx.interpolate(k)) for k in keys]

    keycode = cmd.params.get("keycode", "")
    if isinstance(keycode, int):
        name = _ANDROID_KEYCODE_MAP.get(keycode, str(keycode))
        return [name]
    keycode_str = ctx.interpolate(keycode)
    if keycode_str:
        return [_normalize_key(keycode_str)]
    return []


def handle_press_keycode(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Press key(s) on the device.

    Params:
        keys: list of key names (e.g., ["Enter", "Tab"]).
        keycode: single key name or Android keycode int (legacy).
        mode: "simultaneous" or "sequential" (default: "simultaneous").
        pause_ms: pause between sequential keys in milliseconds.
    """
    keys = _resolve_keys(cmd, ctx)
    if not keys:
        raise RuntimeError("press_keycode: 'keys' or 'keycode' parameter is required")

    mode = cmd.params.get("mode", "simultaneous")
    pause_ms = _resolve_duration_ms(cmd, ctx, "pause_ms", 0)

    device = ctx.session.device
    key_capability = get_device_capability(ctx.session, KeyInputCapability)

    if key_capability is not None:
        key_capability.press_keys(keys, mode=str(mode), pause_ms=pause_ms)
    elif isinstance(device, BrowserDevice):
        browser_automation_port(ctx.session.data["browser_data"]).press_keys(
            keys, mode=str(mode), pause_ms=pause_ms
        )
    else:
        raise RuntimeError("press_keycode: not supported for this device type")

    if ctx.listener:
        ctx.listener.on_log(f"Pressed key(s): {keys}")
