"""Handler for click_position command — supports browser, Android, and Windows devices."""

from __future__ import annotations

from automadin.engine.device_controller import TapCapability, get_device_capability
from automadin.capabilities.browser.port import browser_automation_port
from automadin.execution.coordinates import resolve_point
from automadin.execution.context import FlowContext
from automadin.models.device import BrowserDevice, WindowsDevice
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


def handle_click_position(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Click at a position on the current device.

    Coordinates are pixels by default, or percentages when coordinate_unit is
    set to "percent".

    Params:
        coordinate_unit: "pixels" or "percent" (default: "pixels").
        x: X coordinate.
        y: Y coordinate.
        click: "single" or "double" (default: "single").
    """
    x, y = resolve_point(
        cmd, ctx, "x", "y", default_unit="pixels", value_type=ValueType.NUMBER
    )
    click_type = cmd.params.get("click", cmd.params.get("clickType", "single"))

    device = ctx.session.device
    tap_capability = get_device_capability(ctx.session, TapCapability)

    if tap_capability is not None:
        tap_capability.tap(
            x,
            y,
            click_type=str(click_type),
            double_tap_interval_s=0.15,
        )

    elif isinstance(device, BrowserDevice):
        browser_automation_port(ctx.session.data["browser_data"]).click_position(
            x, y, click_type=str(click_type)
        )

    elif isinstance(device, WindowsDevice):
        from automadin.execution.handlers.actions import _perform_click_at

        _perform_click_at(ctx, x, y, click_type=str(click_type))

    else:
        raise RuntimeError(f"click_position: unsupported device type {type(device)}")

    if ctx.listener:
        ctx.listener.on_log(f"Clicked at ({x}, {y}) [{click_type}]")
