"""Handlers for Windows device commands (win_click, win_key, win_type, etc.)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import time

from automadin.execution.coordinates import resolve_offset, resolve_point
from automadin.execution.context import FlowContext
from automadin.execution.attention import (
    AttentionResolution,
    resolve_condition_unmet_policy,
    resolve_not_found_policy,
)
from automadin.execution.executor import CommandNotMatched
from automadin.execution.image_match_scope import image_match_child_scope
from automadin.execution.handlers.detection import (
    _find_any_image,
    _resolve_image_names,
    _take_screenshot_for_device,
)
from automadin.engine.windows_engine import WindowsDeviceEngine
from automadin.engine.windows_input import (
    click_window,
    drag_window,
    press_window_keys,
    scroll_window,
    type_window_text,
)
from automadin.models.device import WindowsDevice
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType

_IMAGE_CONDITION_POLL_INTERVAL_SECONDS = 0.05

# Virtual key codes for common keys
_VK_MAP = {
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "space": 0x20,
    "up": 0x26, "arrowup": 0x26,
    "down": 0x28, "arrowdown": 0x28,
    "left": 0x25, "arrowleft": 0x25,
    "right": 0x27, "arrowright": 0x27,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "shift": 0x10,
    "meta": 0x5B, "win": 0x5B, "windows": 0x5B,
}


def _timeout_seconds(cmd: FlowCommand, ctx: FlowContext, default_ms: int) -> float:
    raw_timeout = cmd.params.get("timeout", default_ms)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout_ms = (
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    return max(1, int(timeout_ms)) / 1000.0


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


def _resolve_integer(cmd: FlowCommand, ctx: FlowContext, name: str, default: int) -> int:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.INTEGER)
        if callable(resolver)
        else raw_value
    )
    return int(value)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows handlers require Windows OS")


def _get_win_data(ctx: FlowContext) -> dict:
    device = ctx.session.device
    if not isinstance(device, WindowsDevice):
        raise RuntimeError("This command requires a Windows device")
    data = ctx.session.get_data("windows_data")
    if data is None:
        raise RuntimeError("No Windows session data")
    data = WindowsDeviceEngine().resolve_session_data(data)
    ctx.session.put_data("windows_data", data)
    return data


def _window_rect(hwnd: int) -> ctypes.wintypes.RECT:
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))  # type: ignore
    return rect


def handle_win_click(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Click at coordinates within the connected Windows window.

    Params:
        coordinate_unit: "pixels" or "percent" (default: "pixels").
        x: X coordinate within the window.
        y: Y coordinate within the window.
        click: "single" or "double" (default: "single").
    """
    _require_windows()
    win_data = _get_win_data(ctx)
    hwnd = int(win_data["hwnd"])
    x, y = resolve_point(
        cmd, ctx, "x", "y", default_unit="pixels", value_type=ValueType.INTEGER
    )
    click_type = cmd.params.get("click", "single")
    button = cmd.params.get("button", "left")
    mode = cmd.params.get("mode", "message")
    focus_delay_ms = _resolve_duration_ms(cmd, ctx, "focus_delay_ms", 200)
    click_duration_ms = _resolve_duration_ms(cmd, ctx, "click_duration_ms", 50)
    restore_delay_ms = _resolve_duration_ms(cmd, ctx, "restore_delay_ms", 100)
    click_window(
        hwnd,
        x,
        y,
        button=str(button),
        click_type=str(click_type),
        mode=str(mode),
        focus_delay_ms=focus_delay_ms,
        click_duration_ms=click_duration_ms,
        restore_delay_ms=restore_delay_ms,
    )

    if ctx.listener:
        ctx.listener.on_log(
            f"win_click at ({x}, {y}) [{click_type}, {button}, {mode}]"
        )


def _drag_params(cmd: FlowCommand, ctx: FlowContext) -> tuple[str, str, int, int, int, int]:
    button = str(cmd.params.get("button", "left"))
    mode = str(cmd.params.get("mode", "input"))
    drag_duration_ms = _resolve_duration_ms(cmd, ctx, "drag_duration_ms", 300)
    raw_steps = cmd.params.get("drag_steps", cmd.params.get("steps", 12))
    resolver = getattr(ctx, "resolve_command_scalar", None)
    steps = int(
        resolver(raw_steps, ValueType.INTEGER)
        if callable(resolver)
        else raw_steps
    )
    focus_delay_ms = _resolve_duration_ms(cmd, ctx, "focus_delay_ms", 200)
    restore_delay_ms = _resolve_duration_ms(cmd, ctx, "restore_delay_ms", 100)
    return button, mode, drag_duration_ms, steps, focus_delay_ms, restore_delay_ms


def _resolve_drag_offset(cmd: FlowCommand, ctx: FlowContext) -> tuple[int, int]:
    return resolve_offset(
        cmd,
        ctx,
        x_names=("xoffset", "offset_x", "drag_x", "drag_offset_x"),
        y_names=("yoffset", "offset_y", "drag_y", "drag_offset_y"),
        unit_param="offset_unit",
        default_unit="pixels",
        value_type=ValueType.INTEGER,
    )


def handle_win_drag(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Drag from a window coordinate to that coordinate plus an offset."""
    _require_windows()
    win_data = _get_win_data(ctx)
    hwnd = int(win_data["hwnd"])

    start_x, start_y = resolve_point(
        cmd, ctx, "x", "y", default_unit="pixels", value_type=ValueType.INTEGER
    )
    offset_x, offset_y = _resolve_drag_offset(cmd, ctx)
    end_x = start_x + offset_x
    end_y = start_y + offset_y
    button, mode, drag_duration_ms, steps, focus_delay_ms, restore_delay_ms = _drag_params(cmd, ctx)

    drag_window(
        hwnd,
        start_x,
        start_y,
        end_x,
        end_y,
        button=button,
        mode=mode,
        drag_duration_ms=drag_duration_ms,
        steps=steps,
        focus_delay_ms=focus_delay_ms,
        restore_delay_ms=restore_delay_ms,
    )

    if ctx.listener:
        ctx.listener.on_log(
            f"win_drag ({start_x}, {start_y}) -> ({end_x}, {end_y}) "
            f"[{button}, {mode}, {drag_duration_ms}ms]"
        )


def _resolve_win_keys(cmd: FlowCommand, ctx: FlowContext) -> list[tuple[str, int]]:
    raw_keys = cmd.params.get("keys")
    if isinstance(raw_keys, list) and raw_keys:
        keys = [ctx.interpolate(key).strip() for key in raw_keys]
    else:
        legacy_key = ctx.interpolate(cmd.params.get("key", "")).strip()
        keys = [legacy_key] if legacy_key else []

    if not keys or any(not key for key in keys):
        raise RuntimeError("win_key: non-empty 'keys' list (or legacy 'key') is required")

    resolved: list[tuple[str, int]] = []
    for key in keys:
        normalized = key.lower()
        vk = _VK_MAP.get(normalized)
        if vk is None and len(key) == 1 and key.isascii() and key.isalnum():
            vk = ord(key.upper())
        if vk is None:
            raise RuntimeError(f"win_key: unsupported key '{key}'")
        resolved.append((key, vk))
    return resolved


def handle_win_key(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Press a keyboard key on the Windows window.

    Params:
        keys: key names from the key-list editor.
        key: one key name (legacy).
        mode: "simultaneous" or "sequential".
    """
    _require_windows()
    win_data = _get_win_data(ctx)
    keys = _resolve_win_keys(cmd, ctx)
    mode = str(cmd.params.get("mode", "simultaneous")).lower()
    if mode not in {"simultaneous", "sequential"}:
        raise RuntimeError(
            f"win_key: unsupported mode '{mode}' (expected 'simultaneous' or 'sequential')"
        )
    pause_ms = _resolve_duration_ms(cmd, ctx, "pause_ms", 0)
    if pause_ms < 0:
        raise RuntimeError("win_key: 'pause_ms' cannot be negative")

    hwnd = int(win_data["hwnd"])
    delivery_mode = str(cmd.params.get("delivery_mode", "background_key")).lower()
    focus_delay_ms = _resolve_duration_ms(cmd, ctx, "focus_delay_ms", 200)
    restore_delay_ms = _resolve_duration_ms(cmd, ctx, "restore_delay_ms", 100)
    press_window_keys(
        hwnd,
        [virtual_key for _key, virtual_key in keys],
        sequence_mode=mode,
        delivery_mode=delivery_mode,
        pause_ms=pause_ms,
        focus_delay_ms=focus_delay_ms,
        restore_delay_ms=restore_delay_ms,
    )

    if ctx.listener:
        ctx.listener.on_log(
            f"win_key: {', '.join(key for key, _vk in keys)} "
            f"[{mode}, {delivery_mode}]"
        )


def handle_win_type(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Type text into the Windows window using SendInput.

    Params:
        text: the text to type.
        min_delay: minimum delay between characters in ms.
        max_delay: maximum delay between characters in ms.
        delay_ms: fixed delay between characters in ms (legacy).
    """
    _require_windows()
    win_data = _get_win_data(ctx)
    hwnd = int(win_data["hwnd"])
    text = ctx.interpolate(cmd.params.get("text", ""))
    min_delay = _resolve_duration_ms(cmd, ctx, "min_delay", 0)
    max_delay = _resolve_duration_ms(cmd, ctx, "max_delay", 0)
    delay_ms = _resolve_duration_ms(cmd, ctx, "delay_ms", 0)
    if min_delay == 0 and max_delay == 0 and delay_ms > 0:
        min_delay = delay_ms
        max_delay = delay_ms
    if min_delay < 0 or max_delay < 0:
        raise RuntimeError("win_type: delays cannot be negative")
    if max_delay < min_delay:
        raise RuntimeError("win_type: 'max_delay' cannot be less than 'min_delay'")

    if not text:
        raise RuntimeError("win_type: 'text' parameter is required")

    delivery_mode = str(cmd.params.get("delivery_mode", "background_text")).lower()
    focus_delay_ms = _resolve_duration_ms(cmd, ctx, "focus_delay_ms", 200)
    restore_delay_ms = _resolve_duration_ms(cmd, ctx, "restore_delay_ms", 100)
    type_window_text(
        hwnd,
        text,
        delivery_mode=delivery_mode,
        min_delay_ms=min_delay,
        max_delay_ms=max_delay,
        focus_delay_ms=focus_delay_ms,
        restore_delay_ms=restore_delay_ms,
    )

    if ctx.listener:
        ctx.listener.on_log(f"win_type: typed {len(text)} chars")


def handle_win_scroll(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Scroll the mouse wheel on the Windows window.

    Params:
        coordinate_unit: "pixels" or "percent" (default: "pixels").
        x: X coordinate within window.
        y: Y coordinate within window.
        amount: scroll amount (positive = up, negative = down, default: -3).
    """
    _require_windows()
    win_data = _get_win_data(ctx)
    hwnd = int(win_data["hwnd"])
    rect = _window_rect(hwnd)

    x, y = resolve_point(
        cmd,
        ctx,
        "x",
        "y",
        default_unit="pixels",
        surface_size=(rect.right - rect.left, rect.bottom - rect.top),
        default_x=(rect.right - rect.left) // 2,
        default_y=(rect.bottom - rect.top) // 2,
        value_type=ValueType.INTEGER,
    )
    amount = _resolve_integer(cmd, ctx, "amount", -3)

    delivery_mode = str(cmd.params.get("delivery_mode", "message")).lower()
    focus_delay_ms = _resolve_duration_ms(cmd, ctx, "focus_delay_ms", 200)
    restore_delay_ms = _resolve_duration_ms(cmd, ctx, "restore_delay_ms", 100)
    scroll_window(
        hwnd,
        x,
        y,
        amount,
        delivery_mode=delivery_mode,
        focus_delay_ms=focus_delay_ms,
        restore_delay_ms=restore_delay_ms,
    )

    if ctx.listener:
        ctx.listener.on_log(
            f"win_scroll at ({x}, {y}) amount={amount} [{delivery_mode}]"
        )


def handle_win_focus(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Restore and bring the connected Windows application to the foreground."""
    _require_windows()
    win_data = _get_win_data(ctx)
    win_data = WindowsDeviceEngine().restore_and_focus(win_data)
    ctx.session.put_data("windows_data", win_data)

    if ctx.listener:
        ctx.listener.on_log("win_focus: restored and brought window to front")


def handle_win_ensure_app(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Reacquire a live window, launching the application when necessary."""
    _require_windows()
    if not isinstance(ctx.session.device, WindowsDevice):
        raise RuntimeError("This command requires a Windows device")
    win_data = ctx.session.get_data("windows_data")
    if not isinstance(win_data, dict):
        raise RuntimeError("No Windows session data")

    raw_arguments = cmd.params.get("arguments", [])
    if not isinstance(raw_arguments, list):
        raise RuntimeError("win_ensure_app: 'arguments' must be a list")
    arguments = [ctx.interpolate(value) for value in raw_arguments]
    timeout = _timeout_seconds(cmd, ctx, 30000)
    engine = WindowsDeviceEngine()
    launched = engine.ensure_application(
        win_data,
        executable_path=ctx.interpolate(
            str(cmd.params.get("executable_path", ""))
        ),
        arguments=arguments,
        window_title=ctx.interpolate(cmd.params.get("window_title", "")),
        timeout=timeout,
    )
    if _resolve_boolean(cmd, ctx, "focus", True):
        engine.restore_and_focus(win_data)
    ctx.session.put_data("windows_data", win_data)
    ctx.session.device = WindowsDevice(
        hwnd=int(win_data["hwnd"]),
        title=str(win_data.get("title", "")),
        executable_path=str(win_data.get("executable_path", "")),
        auto_start=bool(win_data.get("auto_start", False)),
    )
    if ctx.listener:
        action = "launched" if launched else "already running"
        ctx.listener.on_log(f"win_ensure_app: {action}")


def handle_win_click_image(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Find an image on the Windows window and click it.

    Params:
        images: list of template image names (Java format).
        image_name: single template image name.
        threshold: match confidence (default: 0.90).
        click: "single" or "double" (default: "single").
        offset_unit: "pixels" or "percent" (default: "pixels").
        xmargin / margin_x: X offset for click (default: 0).
        ymargin / margin_y: Y offset for click (default: 0).
    """
    _require_windows()
    win_data = _get_win_data(ctx)
    hwnd = int(win_data["hwnd"])

    image_names = _resolve_image_names(cmd, ctx)
    threshold = _resolve_number(cmd, ctx, "threshold", 0.90)
    click_type = cmd.params.get("click", "single")
    button = cmd.params.get("button", "left")
    mode = cmd.params.get("mode", "message")
    focus_delay_ms = _resolve_duration_ms(cmd, ctx, "focus_delay_ms", 200)
    click_duration_ms = _resolve_duration_ms(cmd, ctx, "click_duration_ms", 50)
    restore_delay_ms = _resolve_duration_ms(cmd, ctx, "restore_delay_ms", 100)
    xmargin, ymargin = resolve_offset(cmd, ctx, value_type=ValueType.INTEGER)
    color_match = _resolve_boolean(cmd, ctx, "color_match", False)
    color_tolerance = _resolve_number(cmd, ctx, "color_tolerance", 0.3)

    if not image_names:
        raise RuntimeError("win_click_image: 'images' or 'image_name' required")

    while True:
        screenshot = _take_screenshot_for_device(ctx)
        if screenshot is None:
            raise RuntimeError("win_click_image: failed to take screenshot")
        ctx._last_screenshot = screenshot
        matched_name, result = _find_any_image(
            ctx, image_names, screenshot, threshold, color_match, color_tolerance
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
            f"win_click_image: none of {image_names} found on screen"
        )

    # Click at the match center + margin
    click_x = result.center_x + xmargin
    click_y = result.center_y + ymargin
    click_window(
        hwnd,
        click_x,
        click_y,
        button=str(button),
        click_type=str(click_type),
        mode=str(mode),
        focus_delay_ms=focus_delay_ms,
        click_duration_ms=click_duration_ms,
        restore_delay_ms=restore_delay_ms,
    )

    if ctx.listener:
        ctx.listener.on_log(
            f"win_click_image '{matched_name}' at ({click_x}, {click_y}) "
            f"[{click_type}, {button}, {mode}]"
        )


def handle_win_drag_image(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Find an image and drag from the matched point plus offset to a relative end."""
    _require_windows()
    win_data = _get_win_data(ctx)
    hwnd = int(win_data["hwnd"])

    image_names = _resolve_image_names(cmd, ctx)
    threshold = _resolve_number(cmd, ctx, "threshold", 0.90)
    start_offset_x, start_offset_y = resolve_offset(
        cmd,
        ctx,
        x_names=("xmargin", "margin_x", "start_xoffset", "start_offset_x"),
        y_names=("ymargin", "margin_y", "start_yoffset", "start_offset_y"),
        unit_param="start_offset_unit",
        default_unit="pixels",
        value_type=ValueType.INTEGER,
    )
    offset_x, offset_y = _resolve_drag_offset(cmd, ctx)
    button, mode, drag_duration_ms, steps, focus_delay_ms, restore_delay_ms = _drag_params(cmd, ctx)
    color_match = _resolve_boolean(cmd, ctx, "color_match", False)
    color_tolerance = _resolve_number(cmd, ctx, "color_tolerance", 0.3)

    if not image_names:
        raise RuntimeError("win_drag_image: 'images' or 'image_name' required")

    while True:
        screenshot = _take_screenshot_for_device(ctx)
        if screenshot is None:
            raise RuntimeError("win_drag_image: failed to take screenshot")
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
            f"win_drag_image: none of {image_names} found on screen"
        )

    start_x = result.center_x + start_offset_x
    start_y = result.center_y + start_offset_y
    end_x = start_x + offset_x
    end_y = start_y + offset_y

    drag_window(
        hwnd,
        start_x,
        start_y,
        end_x,
        end_y,
        button=button,
        mode=mode,
        drag_duration_ms=drag_duration_ms,
        steps=steps,
        focus_delay_ms=focus_delay_ms,
        restore_delay_ms=restore_delay_ms,
    )

    if ctx.listener:
        ctx.listener.on_log(
            f"win_drag_image '{matched_name}' ({start_x}, {start_y}) -> "
            f"({end_x}, {end_y}) [{button}, {mode}, {drag_duration_ms}ms]"
        )


def _win_click_at_match(
    ctx: FlowContext,
    result,
    xmargin: int = 0,
    ymargin: int = 0,
    *,
    button: str = "left",
    mode: str = "message",
    focus_delay_ms: int = 200,
    click_duration_ms: int = 50,
    restore_delay_ms: int = 100,
) -> None:
    """Click at the matched image position on the Windows window."""
    win_data = ctx.session.data["windows_data"]
    hwnd = int(win_data["hwnd"])
    click_x = result.center_x + xmargin
    click_y = result.center_y + ymargin

    click_window(
        hwnd,
        click_x,
        click_y,
        button=button,
        mode=mode,
        focus_delay_ms=focus_delay_ms,
        click_duration_ms=click_duration_ms,
        restore_delay_ms=restore_delay_ms,
    )

    if ctx.listener:
        ctx.listener.on_log(
            f"  click_if_found: clicked at ({click_x}, {click_y}) "
            f"[{button}, {mode}]"
        )


def _record_win_image_match(
    ctx: FlowContext, matched_name: str | None, result: object | None
) -> None:
    """Expose the latest Windows image check to downstream commands."""
    if result is None:
        ctx.set_variable("_image_found", "false")
        ctx.set_variable("_match_x", "0")
        ctx.set_variable("_match_y", "0")
        ctx.set_variable("_match_confidence", "0")
        ctx.set_variable("_matched_image", "")
        return

    ctx.set_variable("_image_found", "true")
    ctx.set_variable("_match_x", str(result.center_x))
    ctx.set_variable("_match_y", str(result.center_y))
    ctx.set_variable("_match_confidence", f"{result.confidence:.4f}")
    ctx.set_variable("_matched_image", matched_name or "")


def _wait_for_win_image_condition(
    ctx: FlowContext,
    image_names: list[str],
    condition: str,
    threshold: float,
    color_match: bool,
    color_tolerance: float,
    timeout_ms: int,
) -> tuple[bool, str | None, object | None, bool]:
    """Poll a Windows image condition without deciding how an unmet result is handled."""
    started = time.perf_counter()
    checks = 0

    def finish(met: bool) -> tuple[bool, str | None, object | None, bool]:
        elapsed_ms = (time.perf_counter() - started) * 1000
        ctx.log_detailed(
            f"Windows image condition '{condition}' "
            f"{'met' if met else 'not met'} after "
            f"{checks} check{'s' if checks != 1 else ''} over {elapsed_ms:.1f}ms"
        )
        return met, matched_name, result, found

    deadline = time.monotonic() + timeout_ms / 1000.0 if timeout_ms > 0 else None
    matched_name: str | None = None
    result: object | None = None
    found = False

    while not ctx.is_stopped:
        checks += 1
        screenshot = _take_screenshot_for_device(ctx)
        if screenshot is None:
            if deadline is None:
                raise RuntimeError("win_if_image: failed to take screenshot")
        else:
            ctx._last_screenshot = screenshot
            matched_name, result = _find_any_image(
                ctx,
                image_names,
                screenshot,
                threshold,
                color_match,
                color_tolerance,
            )
            _record_win_image_match(ctx, matched_name, result)
            found = result is not None
            if (found and condition == "found") or (
                not found and condition == "not_found"
            ):
                return finish(True)

        if deadline is None:
            return finish(False)
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return finish(False)
        time.sleep(min(_IMAGE_CONDITION_POLL_INTERVAL_SECONDS, remaining_seconds))
        if ctx.is_stopped or time.monotonic() >= deadline:
            return finish(False)

    return False, matched_name, result, found


def handle_win_if_image(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Conditional branch based on image detection on a Windows window.

    Params:
        images: list of template image names (Java format).
        image_name: single template image name.
        condition: "found" or "not_found" (default: "found").
        threshold: match confidence (default: 0.90).
        click_if_found / clickIfFound: click matched image center (default: false).
        color_match: validate colors after grayscale match (default: false).
        color_tolerance: Spatially tolerant color-distance threshold 0.0-1.0 (default: 0.3).
        offset_unit: "pixels" or "percent" (default: "pixels").
        xmargin / margin_x: X offset for click (default: 0).
        ymargin / margin_y: Y offset for click (default: 0).
    """
    _require_windows()
    _get_win_data(ctx)

    image_names = _resolve_image_names(cmd, ctx)
    condition = cmd.params.get("condition", "found")
    threshold = _resolve_number(cmd, ctx, "threshold", 0.90)
    raw_timeout = cmd.params.get("timeout", 0)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout_ms = int(
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    raw_click_if_found = cmd.params.get("click_if_found", cmd.params.get("clickIfFound", False))
    resolver = getattr(ctx, "resolve_command_scalar", None)
    click_if_found = bool(
        resolver(raw_click_if_found, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_click_if_found
    )
    button = str(cmd.params.get("button", "left"))
    mode = str(cmd.params.get("mode", "message"))
    focus_delay_ms = _resolve_duration_ms(cmd, ctx, "focus_delay_ms", 200)
    click_duration_ms = _resolve_duration_ms(cmd, ctx, "click_duration_ms", 50)
    restore_delay_ms = _resolve_duration_ms(cmd, ctx, "restore_delay_ms", 100)
    xmargin, ymargin = resolve_offset(cmd, ctx, value_type=ValueType.INTEGER)
    color_match = _resolve_boolean(cmd, ctx, "color_match", False)
    color_tolerance = _resolve_number(cmd, ctx, "color_tolerance", 0.3)

    if not image_names:
        raise RuntimeError("win_if_image: 'images' or 'image_name' required")

    executor = ctx._executor
    while not ctx.is_stopped:
        matched, matched_name, result, found = _wait_for_win_image_condition(
            ctx,
            image_names,
            condition,
            threshold,
            color_match,
            color_tolerance,
            timeout_ms,
        )
        if ctx.is_stopped:
            return
        if matched:
            if ctx.listener:
                ctx.listener.on_log(
                    f"win_if_image: '{condition}' met for '{matched_name or image_names}'"
                )

            if click_if_found and found and result is not None:
                _win_click_at_match(
                    ctx,
                    result,
                    xmargin,
                    ymargin,
                    button=button,
                    mode=mode,
                    focus_delay_ms=focus_delay_ms,
                    click_duration_ms=click_duration_ms,
                    restore_delay_ms=restore_delay_ms,
                )

            coordinates = (
                (int(result.center_x), int(result.center_y))
                if found and result is not None
                else None
            )
            with image_match_child_scope(ctx, coordinates):
                executor._execute_children(cmd.children, ctx)
            return

        resolution = resolve_condition_unmet_policy(
            ctx,
            cmd,
            (
                f"Expected image condition '{condition}' for {', '.join(image_names)} "
                "was not met. Make the required state visible, then choose Retry."
            ),
        )
        if resolution is AttentionResolution.RETRY:
            continue
        if resolution is AttentionResolution.STOP:
            ctx.stop()
            return

        if ctx.listener:
            ctx.listener.on_log(f"win_if_image: '{condition}' NOT met for {image_names}")
        if cmd.else_children:
            executor._execute_children(cmd.else_children, ctx)
        return


def handle_win_while(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Loop while an image condition is met on a Windows window.

    Params:
        images: list of template image names (Java format).
        image_name: single template image name.
        condition: "found" or "not_found" (default: "found").
        threshold: match confidence (default: 0.90).
        timeout: max duration in ms (default: 30000).
        interval_ms: sleep between iterations in ms (default: 1000).
        click_if_found / clickIfFound: click matched image on each iteration (default: false).
        color_match: validate colors after grayscale match (default: false).
        color_tolerance: Spatially tolerant color-distance threshold 0.0-1.0 (default: 0.3).
        offset_unit: "pixels" or "percent" (default: "pixels").
        xmargin / margin_x: X offset for click (default: 0).
        ymargin / margin_y: Y offset for click (default: 0).
    """
    _require_windows()
    _get_win_data(ctx)

    image_names = _resolve_image_names(cmd, ctx)
    condition = cmd.params.get("condition", "found")
    threshold = _resolve_number(cmd, ctx, "threshold", 0.90)
    raw_timeout = cmd.params.get("timeout", 30000)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout_ms = int(
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    interval_ms = _resolve_duration_ms(cmd, ctx, "interval_ms", 1000)
    raw_click_if_found = cmd.params.get("click_if_found", cmd.params.get("clickIfFound", False))
    resolver = getattr(ctx, "resolve_command_scalar", None)
    click_if_found = bool(
        resolver(raw_click_if_found, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_click_if_found
    )
    button = str(cmd.params.get("button", "left"))
    mode = str(cmd.params.get("mode", "message"))
    focus_delay_ms = _resolve_duration_ms(cmd, ctx, "focus_delay_ms", 200)
    click_duration_ms = _resolve_duration_ms(cmd, ctx, "click_duration_ms", 50)
    restore_delay_ms = _resolve_duration_ms(cmd, ctx, "restore_delay_ms", 100)
    xmargin, ymargin = resolve_offset(cmd, ctx, value_type=ValueType.INTEGER)
    color_match = _resolve_boolean(cmd, ctx, "color_match", False)
    color_tolerance = _resolve_number(cmd, ctx, "color_tolerance", 0.3)

    if not image_names:
        raise RuntimeError("win_while: 'images' or 'image_name' required")

    executor = ctx._executor
    start = time.monotonic()
    iterations = 0
    retry_after_attention = False

    while not ctx.is_stopped:
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms >= timeout_ms and not retry_after_attention:
            if ctx.listener:
                ctx.listener.on_log(f"win_while: timed out after {timeout_ms}ms")
            break
        retry_after_attention = False

        screenshot = _take_screenshot_for_device(ctx)
        if screenshot is None:
            raise RuntimeError("win_while: failed to take screenshot")
        ctx._last_screenshot = screenshot

        matched_name, result = _find_any_image(ctx, image_names, screenshot, threshold, color_match, color_tolerance)
        _record_win_image_match(ctx, matched_name, result)
        found = result is not None
        matched = (found and condition == "found") or (not found and condition == "not_found")

        if not matched:
            resolution = resolve_condition_unmet_policy(
                ctx,
                cmd,
                (
                    f"Expected image condition '{condition}' for {', '.join(image_names)} "
                    "was not met. Make the required state visible, then choose Retry."
                ),
            )
            if resolution is AttentionResolution.RETRY and not ctx.is_stopped:
                # Let Retry perform one fresh check after a human checkpoint,
                # independent of the elapsed wall-clock loop timeout.
                retry_after_attention = True
                continue
            if resolution is AttentionResolution.STOP:
                ctx.stop()
                break
            if ctx.listener:
                ctx.listener.on_log(f"win_while: condition no longer met after {iterations} iterations")

            # click_if_found on exit: if condition was "not_found" and image IS now found, click it
            if click_if_found and found and result is not None:
                _win_click_at_match(
                    ctx,
                    result,
                    xmargin,
                    ymargin,
                    button=button,
                    mode=mode,
                    focus_delay_ms=focus_delay_ms,
                    click_duration_ms=click_duration_ms,
                    restore_delay_ms=restore_delay_ms,
                )

            break

        # click_if_found during loop: if condition is "found" and image matched, click it
        if click_if_found and found and result is not None:
            _win_click_at_match(
                ctx,
                result,
                xmargin,
                ymargin,
                button=button,
                mode=mode,
                focus_delay_ms=focus_delay_ms,
                click_duration_ms=click_duration_ms,
                restore_delay_ms=restore_delay_ms,
            )

        coordinates = (
            (int(result.center_x), int(result.center_y))
            if found and result is not None
            else None
        )
        with image_match_child_scope(ctx, coordinates):
            executor._execute_children(cmd.children, ctx)
        iterations += 1

        # Sleep between iterations to avoid hammering CPU and respect timeout
        if interval_ms > 0 and not ctx.is_stopped:
            time.sleep(interval_ms / 1000.0)

    if ctx.listener:
        ctx.listener.on_log(f"win_while completed ({iterations} iterations)")
