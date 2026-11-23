"""Provider-neutral notification delivery for flow commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class NotificationChannel(str, Enum):
    """Notification providers available to flow commands."""

    PUSHOVER = "pushover"
    TELEGRAM = "telegram"


@dataclass(frozen=True)
class NotificationRequest:
    """Provider-neutral notification content.

    Priority and emergency timing are retained for Pushover compatibility. A
    provider that does not advertise those capabilities may safely ignore them.
    """

    title: str
    message: str
    priority: str = "normal"
    retry_seconds: int = 60
    expire_seconds: int = 1800


@dataclass(frozen=True)
class NotificationResult:
    channel: NotificationChannel
    delivery_id: str
    acknowledgement_pending: bool = False


@dataclass(frozen=True)
class NotificationCapabilities:
    priority: bool = False
    acknowledgement: bool = False


class NotificationProvider(Protocol):
    channel: NotificationChannel
    capabilities: NotificationCapabilities

    def send(self, db: Any, notification: NotificationRequest) -> NotificationResult:
        """Deliver one notification using configuration associated with ``db``."""


class _PushoverProvider:
    channel = NotificationChannel.PUSHOVER
    capabilities = NotificationCapabilities(priority=True, acknowledgement=True)

    def send(self, db: Any, notification: NotificationRequest) -> NotificationResult:
        from automadin.services.pushover import (
            PushoverMessage,
            PushoverPriority,
            load_pushover_configuration,
            send_pushover_notification,
        )

        result = send_pushover_notification(
            load_pushover_configuration(db),
            PushoverMessage(
                title=notification.title,
                message=notification.message,
                priority=PushoverPriority(notification.priority),
                retry_seconds=notification.retry_seconds,
                expire_seconds=notification.expire_seconds,
            ),
        )
        return NotificationResult(
            channel=self.channel,
            delivery_id=result.request_id,
            acknowledgement_pending=result.receipt is not None,
        )


class _TelegramProvider:
    channel = NotificationChannel.TELEGRAM
    capabilities = NotificationCapabilities()

    def send(self, db: Any, notification: NotificationRequest) -> NotificationResult:
        from automadin.services.telegram import (
            TelegramMessage,
            load_telegram_configuration,
            send_telegram_message,
        )

        result = send_telegram_message(
            load_telegram_configuration(db),
            TelegramMessage(
                title=notification.title,
                message=notification.message,
            ),
        )
        return NotificationResult(
            channel=self.channel,
            delivery_id=str(result.message_id),
        )


_PROVIDERS: dict[NotificationChannel, NotificationProvider] = {
    NotificationChannel.PUSHOVER: _PushoverProvider(),
    NotificationChannel.TELEGRAM: _TelegramProvider(),
}


def notification_provider(channel: NotificationChannel | str) -> NotificationProvider:
    """Resolve a configured provider identifier or reject unsupported values."""
    try:
        provider_id = NotificationChannel(channel)
    except ValueError as exc:
        raise ValueError(f"Unsupported notification channel: {channel}") from exc
    return _PROVIDERS[provider_id]


def send_notification(
    db: Any,
    channel: NotificationChannel | str,
    notification: NotificationRequest,
) -> NotificationResult:
    """Deliver a notification through the selected provider."""
    return notification_provider(channel).send(db, notification)
