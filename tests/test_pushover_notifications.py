from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from automadin.execution.handlers import notifications as notification_handlers
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.services.flow_preflight import PreflightMode, preflight_flow
from automadin.services.notification_channels import (
    NotificationChannel,
    NotificationRequest,
    NotificationResult,
)
from automadin.services.pushover import (
    PUSHOVER_API_URL,
    PUSHOVER_CREDENTIAL_TARGET,
    PushoverConfiguration,
    PushoverConfigurationError,
    PushoverCredentials,
    PushoverDeliveryError,
    PushoverMessage,
    PushoverPriority,
    delete_pushover_credentials,
    load_pushover_credentials,
    save_pushover_configuration,
    save_pushover_credentials,
    send_pushover_notification,
)


APP_TOKEN = "A" * 30
USER_KEY = "u" * 30


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
        assert target == PUSHOVER_CREDENTIAL_TARGET
        assert credential_type == self.CRED_TYPE_GENERIC
        assert flags == 0
        if self.stored is None:
            raise _MissingCredentialError()
        result = dict(self.stored)
        result["CredentialBlob"] = str(result["CredentialBlob"]).encode("utf-16-le")
        return result

    def CredDelete(self, target: str, credential_type: int, flags: int) -> None:  # noqa: N802
        assert target == PUSHOVER_CREDENTIAL_TARGET
        assert credential_type == self.CRED_TYPE_GENERIC
        assert flags == 0
        if self.stored is None:
            raise _MissingCredentialError()
        self.stored = None


def _configuration(*, device: str = "") -> PushoverConfiguration:
    return PushoverConfiguration(PushoverCredentials(APP_TOKEN, USER_KEY), device)


def test_credentials_round_trip_through_generic_windows_credential_api() -> None:
    api = _FakeCredentialApi()
    credentials = PushoverCredentials(APP_TOKEN, USER_KEY)

    assert load_pushover_credentials(credential_api=api) is None

    save_pushover_credentials(credentials, credential_api=api)

    assert api.stored is not None
    assert api.stored["TargetName"] == PUSHOVER_CREDENTIAL_TARGET
    assert api.stored["CredentialBlob"] != APP_TOKEN
    assert load_pushover_credentials(credential_api=api) == credentials

    delete_pushover_credentials(credential_api=api)
    assert load_pushover_credentials(credential_api=api) is None


def test_credentials_hide_secrets_from_repr() -> None:
    credentials = PushoverCredentials(APP_TOKEN, USER_KEY)
    configuration = PushoverConfiguration(credentials, "iphone")

    assert APP_TOKEN not in repr(credentials)
    assert USER_KEY not in repr(credentials)
    assert APP_TOKEN not in repr(configuration)
    assert USER_KEY not in repr(configuration)


def test_configuration_stores_only_non_secret_device_in_database(monkeypatch) -> None:
    saved_credentials: list[PushoverCredentials] = []
    settings: dict[str, str] = {}
    db = SimpleNamespace(
        set_app_setting=lambda key, value: settings.__setitem__(key, value),
        delete_app_setting=lambda key: settings.pop(key, None),
    )
    monkeypatch.setattr(
        "automadin.services.pushover.save_pushover_credentials",
        saved_credentials.append,
    )

    save_pushover_configuration(db, _configuration(device="iphone"))

    assert saved_credentials == [PushoverCredentials(APP_TOKEN, USER_KEY)]
    assert settings == {"pushover_default_device": "iphone"}
    assert APP_TOKEN not in repr(settings)
    assert USER_KEY not in repr(settings)


def test_normal_notification_posts_expected_bounded_form() -> None:
    captured: dict[str, object] = {}

    def transport(request, timeout: float) -> tuple[int, bytes]:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        captured["form"] = parse_qs(request.data.decode("utf-8"))
        return 200, b'{"status":1,"request":"request-id"}'

    result = send_pushover_notification(
        _configuration(device="iphone"),
        PushoverMessage(title="Ready", message="Target found"),
        transport=transport,
    )

    assert result.request_id == "request-id"
    assert result.receipt is None
    assert captured == {
        "url": PUSHOVER_API_URL,
        "method": "POST",
        "timeout": 10.0,
        "form": {
            "token": [APP_TOKEN],
            "user": [USER_KEY],
            "message": ["Target found"],
            "title": ["Ready"],
            "priority": ["0"],
            "device": ["iphone"],
        },
    }


def test_emergency_notification_requires_and_returns_receipt() -> None:
    captured: dict[str, list[str]] = {}

    def transport(request, _timeout: float) -> tuple[int, bytes]:
        captured.update(parse_qs(request.data.decode("utf-8")))
        return 200, b'{"status":1,"request":"request-id","receipt":"receipt-id"}'

    result = send_pushover_notification(
        _configuration(),
        PushoverMessage(
            title="Alarm",
            message="Action required",
            priority=PushoverPriority.EMERGENCY,
            retry_seconds=45,
            expire_seconds=300,
        ),
        transport=transport,
    )

    assert result.receipt == "receipt-id"
    assert captured["priority"] == ["2"]
    assert captured["retry"] == ["45"]
    assert captured["expire"] == ["300"]


def test_delivery_failure_never_repeats_response_or_credentials() -> None:
    def transport(_request, _timeout: float) -> tuple[int, bytes]:
        return 401, f"bad token {APP_TOKEN} user {USER_KEY}".encode()

    with pytest.raises(PushoverDeliveryError) as caught:
        send_pushover_notification(
            _configuration(),
            PushoverMessage(message="Test"),
            transport=transport,
        )

    assert APP_TOKEN not in str(caught.value)
    assert USER_KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("retry_seconds", "expire_seconds"),
    ((29, 300), (60, 29), (301, 300), (60, 10801)),
)
def test_emergency_timing_is_validated(
    retry_seconds: int,
    expire_seconds: int,
) -> None:
    with pytest.raises(PushoverConfigurationError):
        PushoverMessage(
            message="Alarm",
            priority=PushoverPriority.EMERGENCY,
            retry_seconds=retry_seconds,
            expire_seconds=expire_seconds,
        )


def test_handler_interpolates_message_and_defaults_to_pushover(monkeypatch) -> None:
    sent: list[tuple[object, NotificationChannel, NotificationRequest]] = []
    database = object()
    listener = SimpleNamespace(messages=[], on_log=lambda value: listener.messages.append(value))
    context = SimpleNamespace(
        interpolate=lambda value: str(value).replace("{{item}}", "frog"),
        resolve_command_scalar=lambda value, _expected: value,
        _executor=SimpleNamespace(_db=database),
        listener=listener,
    )
    monkeypatch.setattr(
        notification_handlers,
        "send_notification",
        lambda db, channel, message: sent.append((db, channel, message))
        or NotificationResult(
            channel=NotificationChannel.PUSHOVER,
            delivery_id="request-id",
            acknowledgement_pending=True,
        ),
    )

    notification_handlers.handle_send_notification(
        FlowCommand(
            "send_notification",
            params={
                "title": "Found {{item}}",
                "message": "The image for {{item}} matched.",
                "priority": "emergency",
                "retry_seconds": 60,
                "expire_seconds": 600,
            },
        ),
        context,
    )

    assert sent[0][0] is database
    assert sent[0][1] is NotificationChannel.PUSHOVER
    assert sent[0][2] == NotificationRequest(
        title="Found frog",
        message="The image for frog matched.",
        priority="emergency",
        retry_seconds=60,
        expire_seconds=600,
    )
    assert listener.messages == [
        "Pushover notification accepted; emergency alert will repeat until acknowledged"
    ]


def test_preflight_rejects_emergency_retry_later_than_expiry() -> None:
    flow = AutomationFlow(
        "Invalid notification",
        commands=[
            FlowCommand(
                "send_notification",
                params={
                    "title": "Alarm",
                    "message": "Target found",
                    "priority": "emergency",
                    "retry_seconds": 120,
                    "expire_seconds": 60,
                },
            )
        ],
    )

    report = preflight_flow(flow, mode=PreflightMode.DOCUMENT)

    assert any(
        issue.code == "INVALID_PARAMETER_VALUE"
        and issue.location.parameter == "retry_seconds"
        for issue in report.errors
    )
