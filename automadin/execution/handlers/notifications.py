"""Flow commands that deliver external notifications."""

from __future__ import annotations

from automadin.execution.context import FlowContext
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType
from automadin.services.notification_channels import (
    NotificationChannel,
    NotificationRequest,
    send_notification,
)


def _resolve_integer(ctx: FlowContext, value: object, default: int) -> int:
    raw_value = default if value is None else value
    resolver = getattr(ctx, "resolve_command_scalar", None)
    resolved = (
        resolver(raw_value, ValueType.INTEGER)
        if callable(resolver)
        else raw_value
    )
    return int(resolved)


def handle_send_notification(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Send one bounded provider request and continue after API acceptance."""
    title = ctx.interpolate(cmd.params.get("title", "Automadin"))
    message = ctx.interpolate(cmd.params.get("message", ""))
    channel = NotificationChannel(str(cmd.params.get("channel", "pushover")))
    retry_seconds = 60
    expire_seconds = 1800
    if channel is NotificationChannel.PUSHOVER:
        retry_seconds = _resolve_integer(
            ctx,
            cmd.params.get("retry_seconds"),
            retry_seconds,
        )
        expire_seconds = _resolve_integer(
            ctx,
            cmd.params.get("expire_seconds"),
            expire_seconds,
        )
    executor = getattr(ctx, "_executor", None)
    db = getattr(executor, "_db", None)
    result = send_notification(
        db,
        channel,
        NotificationRequest(
            title=title,
            message=message,
            priority=str(cmd.params.get("priority", "normal")),
            retry_seconds=retry_seconds,
            expire_seconds=expire_seconds,
        ),
    )
    if ctx.listener:
        label = result.channel.value.title()
        suffix = (
            "; emergency alert will repeat until acknowledged"
            if result.acknowledgement_pending
            else ""
        )
        ctx.listener.on_log(f"{label} notification accepted{suffix}")
