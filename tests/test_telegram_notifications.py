from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from automadin.execution.handlers import notifications as notification_handlers
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.services.flow_preflight import PreflightMode, preflight_flow
from automadin.services.notification_channels import (
    NotificationChannel,
    NotificationRequest,
    NotificationResult,
    notification_provider,
)
from automadin.services.telegram import (
    TELEGRAM_API_ROOT,
    TELEGRAM_CHAT_ID_SETTING,
    TELEGRAM_CREDENTIAL_TARGET,
    TelegramConfiguration,
    TelegramCredentials,
    TelegramDeliveryError,
    TelegramMessage,
    delete_telegram_credentials,
    load_telegram_credentials,
    save_telegram_configuration,
    save_telegram_credentials,
    send_telegram_message,
)


BOT_TOKEN = "123456789:" + "A" * 35
CHAT_ID = "-1001234567890"


class _MissingCredentialError(Exception):
    winerror = 1168


class _FakeCredentialApi:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self) -> None:
        self.stored: dict[str, object] | None = None

    def CredWrite(self, value: dict[str, object], flags: int) -> None:  # noqa: N802
        assert flags == 0
        self.stored = dict(value)

    def CredRead(self, target: str, credential_type: int, flags: int):  # noqa: N802
        assert target == TELEGRAM_CREDENTIAL_TARGET
        assert credential_type == self.CRED_TYPE_GENERIC
        assert flags == 0
        if self.stored is None:
            raise _MissingCredentialError()
        result = dict(self.stored)
        result["CredentialBlob"] = str(result["CredentialBlob"]).encode("utf-16-le")
        return result

    def CredDelete(self, target: str, credential_type: int, flags: int) -> None:  # noqa: N802
        assert target == TELEGRAM_CREDENTIAL_TARGET
        assert credential_type == self.CRED_TYPE_GENERIC
        assert flags == 0
        if self.stored is None:
            raise _MissingCredentialError()
        self.stored = None


def _configuration() -> TelegramConfiguration:
    return TelegramConfiguration(TelegramCredentials(BOT_TOKEN), CHAT_ID)


def test_credentials_round_trip_without_exposing_token() -> None:
    api = _FakeCredentialApi()
    credentials = TelegramCredentials(BOT_TOKEN)

    assert load_telegram_credentials(credential_api=api) is None
    save_telegram_credentials(credentials, credential_api=api)

    assert api.stored is not None
    assert api.stored["TargetName"] == TELEGRAM_CREDENTIAL_TARGET
    assert api.stored["CredentialBlob"] != BOT_TOKEN
    assert load_telegram_credentials(credential_api=api) == credentials
    assert BOT_TOKEN not in repr(credentials)
    assert BOT_TOKEN not in repr(_configuration())

    delete_telegram_credentials(credential_api=api)
    assert load_telegram_credentials(credential_api=api) is None


def test_configuration_stores_only_chat_id_in_database(monkeypatch) -> None:
    saved_credentials: list[TelegramCredentials] = []
    settings: dict[str, str] = {}
    db = SimpleNamespace(
        set_app_setting=lambda key, value: settings.__setitem__(key, value),
    )
    monkeypatch.setattr(
        "automadin.services.telegram.save_telegram_credentials",
        saved_credentials.append,
    )

    save_telegram_configuration(db, _configuration())

    assert saved_credentials == [TelegramCredentials(BOT_TOKEN)]
    assert settings == {TELEGRAM_CHAT_ID_SETTING: CHAT_ID}
    assert BOT_TOKEN not in repr(settings)


def test_send_message_posts_bounded_json() -> None:
    captured: dict[str, object] = {}

    def transport(request, timeout: float) -> tuple[int, bytes]:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return 200, b'{"ok":true,"result":{"message_id":42}}'

    result = send_telegram_message(
        _configuration(),
        TelegramMessage(title="Ready", message="Target found"),
        transport=transport,
    )

    assert result.message_id == 42
    assert captured == {
        "url": f"{TELEGRAM_API_ROOT}/bot{BOT_TOKEN}/sendMessage",
        "method": "POST",
        "timeout": 10.0,
        "payload": {"chat_id": CHAT_ID, "text": "Ready\n\nTarget found"},
    }


def test_delivery_failure_never_repeats_response_or_token() -> None:
    def transport(_request, _timeout: float) -> tuple[int, bytes]:
        return 401, f"bad bot token {BOT_TOKEN}".encode()

    with pytest.raises(TelegramDeliveryError) as caught:
        send_telegram_message(
            _configuration(),
            TelegramMessage(message="Test"),
            transport=transport,
        )

    assert BOT_TOKEN not in str(caught.value)


def test_provider_registry_exposes_channel_capabilities() -> None:
    pushover = notification_provider(NotificationChannel.PUSHOVER)
    telegram = notification_provider(NotificationChannel.TELEGRAM)

    assert pushover.capabilities.priority is True
    assert pushover.capabilities.acknowledgement is True
    assert telegram.capabilities.priority is False
    assert telegram.capabilities.acknowledgement is False


def test_handler_dispatches_telegram_without_acknowledgement(monkeypatch) -> None:
    sent: list[tuple[object, NotificationChannel, NotificationRequest]] = []
    database = object()
    listener = SimpleNamespace(messages=[], on_log=lambda value: listener.messages.append(value))
    context = SimpleNamespace(
        interpolate=str,
        resolve_command_scalar=lambda _value, _expected: (_ for _ in ()).throw(
            AssertionError("Telegram must not resolve Pushover emergency timing")
        ),
        _executor=SimpleNamespace(_db=database),
        listener=listener,
    )
    monkeypatch.setattr(
        notification_handlers,
        "send_notification",
        lambda db, channel, message: sent.append((db, channel, message))
        or NotificationResult(channel, "42"),
    )

    notification_handlers.handle_send_notification(
        FlowCommand(
            "send_notification",
            params={
                "channel": "telegram",
                "title": "Ready",
                "message": "Target found",
            },
        ),
        context,
    )

    assert sent == [
        (
            database,
            NotificationChannel.TELEGRAM,
            NotificationRequest(title="Ready", message="Target found"),
        )
    ]
    assert listener.messages == ["Telegram notification accepted"]


def test_preflight_ignores_pushover_emergency_fields_for_telegram() -> None:
    flow = AutomationFlow(
        "Telegram notification",
        commands=[
            FlowCommand(
                "send_notification",
                params={
                    "channel": "telegram",
                    "title": "Ready",
                    "message": "Target found",
                    "priority": "emergency",
                    "retry_seconds": 120,
                    "expire_seconds": 60,
                },
            )
        ],
    )

    report = preflight_flow(flow, mode=PreflightMode.DOCUMENT)

    assert not any(
        issue.code == "INVALID_PARAMETER_VALUE"
        and issue.location.parameter == "retry_seconds"
        for issue in report.errors
    )
