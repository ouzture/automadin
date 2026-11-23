from __future__ import annotations

from typing import Any, Sequence

from automadin.engine.device_controller import (
    SurfaceSizeCapability,
    get_device_capability,
)
from automadin.execution.context import FlowContext
from automadin.models.device import BrowserDevice, WindowsDevice
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


CoordinateUnit = str


def _normalize_unit(value: Any, default: CoordinateUnit) -> CoordinateUnit:
    unit = str(value or default).strip().lower()
    if unit in {"pixel", "pixels", "px"}:
        return "pixels"
    if unit in {"percent", "percentage", "pct", "%"}:
        return "percent"
    return default


def get_coordinate_unit(
    cmd: FlowCommand,
    *,
    param_name: str = "coordinate_unit",
    default: CoordinateUnit = "pixels",
) -> tuple[CoordinateUnit, bool]:
    """Return the coordinate unit and whether the command explicitly set it."""
    return (
        _normalize_unit(cmd.params.get(param_name, default), default),
        param_name in cmd.params,
    )


def _percent_fraction(value: float, explicit_unit: bool) -> float:
    """Convert percent input to a fraction.

    New commands with an explicit unit use human-friendly 0-100 percentages.
    Legacy flows without a unit still accept both 0.0-1.0 and 0-100 values.
    """
    if explicit_unit:
        return value / 100.0
    if abs(value) > 1.0:
        return value / 100.0
    return value


def _axis_to_pixels(
    value: Any,
    size: int,
    unit: CoordinateUnit,
    explicit_unit: bool,
    ctx: FlowContext,
    value_type: ValueType,
) -> int:
    resolver = getattr(ctx, "resolve_command_scalar", None)
    resolved = (
        resolver(value, value_type)
        if callable(resolver)
        else value
    )
    number = float(resolved or 0)
    if unit == "percent":
        return int(_percent_fraction(number, explicit_unit) * size)
    return int(number)


def _get_browser_viewport_size(ctx: FlowContext) -> tuple[int, int]:
    from automadin.capabilities.browser.port import browser_automation_port

    return browser_automation_port(
        ctx.session.data["browser_data"]
    ).viewport_size()


def _get_window_size(ctx: FlowContext) -> tuple[int, int]:
    import ctypes
    import ctypes.wintypes
    from automadin.engine.windows_engine import WindowsDeviceEngine

    win_data = ctx.session.data.get("windows_data") or ctx.session.get_data("windows_data")
    if win_data is None:
        raise RuntimeError("No Windows session data")
    win_data = WindowsDeviceEngine().resolve_session_data(win_data)
    hwnd = int(win_data["hwnd"])
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))  # type: ignore[attr-defined]
    return int(rect.right - rect.left), int(rect.bottom - rect.top)


def get_surface_size(ctx: FlowContext) -> tuple[int, int]:
    """Return the coordinate surface size for the current device."""
    capability = get_device_capability(ctx.session, SurfaceSizeCapability)
    if capability is not None:
        return capability.get_surface_size()
    device = ctx.session.device
    if isinstance(device, BrowserDevice):
        return _get_browser_viewport_size(ctx)
    if isinstance(device, WindowsDevice):
        return _get_window_size(ctx)
    raise RuntimeError(f"Unsupported device type for coordinate conversion: {type(device)}")


def resolve_point(
    cmd: FlowCommand,
    ctx: FlowContext,
    x_name: str | Sequence[str],
    y_name: str | Sequence[str],
    *,
    unit_param: str = "coordinate_unit",
    default_unit: CoordinateUnit = "pixels",
    surface_size: tuple[int, int] | None = None,
    default_x: Any = 0,
    default_y: Any = 0,
    value_type: ValueType = ValueType.NUMBER,
) -> tuple[int, int]:
    """Resolve a command point to pixel coordinates."""
    unit, explicit_unit = get_coordinate_unit(
        cmd,
        param_name=unit_param,
        default=default_unit,
    )
    if unit == "percent":
        width, height = surface_size or get_surface_size(ctx)
    else:
        width, height = 1, 1
    x_names = (x_name,) if isinstance(x_name, str) else x_name
    y_names = (y_name,) if isinstance(y_name, str) else y_name
    raw_x = _first_param(cmd, x_names, default_x)
    raw_y = _first_param(cmd, y_names, default_y)
    x = _axis_to_pixels(raw_x, width, unit, explicit_unit, ctx, value_type)
    y = _axis_to_pixels(raw_y, height, unit, explicit_unit, ctx, value_type)
    if unit == "pixels" and ctx.resolution_scaling_active:
        width, height = surface_size or get_surface_size(ctx)
        transform = ctx.resolution_transform((width, height))
        if transform.scale_pixel_geometry:
            # Match Built-ins are already expressed in the live screenshot's
            # coordinate surface. Authored literals still need reference-size
            # scaling, but scaling live coordinates a second time mis-clicks.
            if not _is_runtime_match_coordinate(raw_x, "x"):
                x = int(x * transform.x_scale)
            if not _is_runtime_match_coordinate(raw_y, "y"):
                y = int(y * transform.y_scale)
    return x, y


def _first_param(cmd: FlowCommand, names: Sequence[str], default: Any = 0) -> Any:
    for name in names:
        if name in cmd.params:
            return cmd.params[name]
    return default


def _is_runtime_match_coordinate(value: Any, axis: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == "builtin"
        and value.get("name") == f"_match_{axis}"
    )


def resolve_offset(
    cmd: FlowCommand,
    ctx: FlowContext,
    x_names: Sequence[str] = ("xmargin", "margin_x"),
    y_names: Sequence[str] = ("ymargin", "margin_y"),
    *,
    unit_param: str = "offset_unit",
    default_unit: CoordinateUnit = "pixels",
    surface_size: tuple[int, int] | None = None,
    value_type: ValueType = ValueType.NUMBER,
) -> tuple[int, int]:
    """Resolve an image-match offset to pixel deltas."""
    unit, explicit_unit = get_coordinate_unit(
        cmd,
        param_name=unit_param,
        default=default_unit,
    )
    if unit == "percent":
        width, height = surface_size or get_surface_size(ctx)
    else:
        width, height = 1, 1
    x = _axis_to_pixels(
        _first_param(cmd, x_names), width, unit, explicit_unit, ctx, value_type
    )
    y = _axis_to_pixels(
        _first_param(cmd, y_names), height, unit, explicit_unit, ctx, value_type
    )
    if unit == "pixels" and ctx.resolution_scaling_active:
        width, height = surface_size or get_surface_size(ctx)
        transform = ctx.resolution_transform((width, height))
        if transform.scale_pixel_geometry:
            x = int(x * transform.x_scale)
            y = int(y * transform.y_scale)
    return x, y
