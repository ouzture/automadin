"""Thin flow handlers for Android application lifecycle commands."""

from __future__ import annotations

from automadin.engine.device_controller import (
    AndroidAppCapability,
    get_device_capability,
)
from automadin.execution.context import FlowContext
from automadin.models.device import AndroidDevice
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


def _app_controller(ctx: FlowContext) -> AndroidAppCapability:
    if not isinstance(ctx.session.device, AndroidDevice):
        raise RuntimeError("Android app commands require an Android device")
    capability = get_device_capability(ctx.session, AndroidAppCapability)
    if capability is None:
        raise RuntimeError("Android application control is not connected")
    return capability


def _package_name(cmd: FlowCommand, ctx: FlowContext) -> str:
    return ctx.interpolate(cmd.params.get("package_name", ""))


def _timeout_ms(cmd: FlowCommand, ctx: FlowContext, default_ms: int) -> int:
    raw_timeout = cmd.params.get("timeout", default_ms)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout = (
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    return max(1, int(timeout))


def _timeout_seconds(cmd: FlowCommand, ctx: FlowContext, default_ms: int) -> float:
    return _timeout_ms(cmd, ctx, default_ms) / 1000.0


def _resolve_boolean(cmd: FlowCommand, ctx: FlowContext, name: str, default: bool) -> bool:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_value
    )
    return bool(value)


def handle_android_install_apk(cmd: FlowCommand, ctx: FlowContext) -> None:
    controller = _app_controller(ctx)
    apk_path = ctx.interpolate(cmd.params.get("apk_path", ""))
    timeout_ms = _timeout_ms(cmd, ctx, 120000)
    output = controller.install_apk(
        apk_path,
        replace_existing=_resolve_boolean(cmd, ctx, "replace_existing", True),
        grant_permissions=_resolve_boolean(cmd, ctx, "grant_permissions", False),
        timeout=max(1, (timeout_ms + 999) // 1000),
    )
    if ctx.listener:
        ctx.listener.on_log(f"Android APK installed: {output}")


def handle_android_launch_app(cmd: FlowCommand, ctx: FlowContext) -> None:
    controller = _app_controller(ctx)
    package = _package_name(cmd, ctx)
    launched = controller.ensure_package_foreground(
        package,
        timeout=_timeout_seconds(cmd, ctx, 15000),
    )
    if ctx.listener:
        action = "Launched" if launched else "Already foreground"
        ctx.listener.on_log(f"{action}: {package}")


def handle_android_force_stop_app(cmd: FlowCommand, ctx: FlowContext) -> None:
    controller = _app_controller(ctx)
    package = _package_name(cmd, ctx)
    controller.force_stop_package(package)
    if ctx.listener:
        ctx.listener.on_log(f"Force-stopped Android app: {package}")


def handle_android_restart_app(cmd: FlowCommand, ctx: FlowContext) -> None:
    controller = _app_controller(ctx)
    package = _package_name(cmd, ctx)
    controller.restart_package(
        package,
        timeout=_timeout_seconds(cmd, ctx, 15000),
    )
    if ctx.listener:
        ctx.listener.on_log(f"Restarted Android app: {package}")


def handle_android_start_activity(cmd: FlowCommand, ctx: FlowContext) -> None:
    controller = _app_controller(ctx)
    package = _package_name(cmd, ctx)
    activity = ctx.interpolate(cmd.params.get("activity_name", ""))
    controller.start_activity(
        package,
        activity,
        timeout=_timeout_seconds(cmd, ctx, 15000),
    )
    if ctx.listener:
        ctx.listener.on_log(f"Started Android activity: {package}/{activity}")


def handle_android_open_deep_link(cmd: FlowCommand, ctx: FlowContext) -> None:
    controller = _app_controller(ctx)
    package = _package_name(cmd, ctx)
    uri = ctx.interpolate(cmd.params.get("uri", ""))
    controller.open_deep_link(
        uri,
        package_name=package,
        timeout=_timeout_seconds(cmd, ctx, 15000),
    )
    if ctx.listener:
        ctx.listener.on_log(f"Opened Android deep link: {uri}")
