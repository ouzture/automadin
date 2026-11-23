"""Secure Pushover configuration and bounded notification delivery."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_CREDENTIAL_TARGET = "Automadin/Pushover/default"
PUSHOVER_DEVICE_SETTING = "pushover_default_device"
PUSHOVER_REQUEST_TIMEOUT_SECONDS = 10.0

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{30}$")
_DEVICE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,25}$")
_ERROR_NOT_FOUND = 1168
_MAX_RESPONSE_BYTES = 64 * 1024


class PushoverError(RuntimeError):
    """Base class for safe, user-presentable Pushover failures."""


class PushoverConfigurationError(PushoverError):
    """The local Pushover configuration is missing or invalid."""


class PushoverCredentialStoreError(PushoverError):
    """Windows Credential Manager could not complete an operation."""


class PushoverDeliveryError(PushoverError):
    """Pushover did not accept a notification."""


class PushoverPriority(str, Enum):
    NORMAL = "normal"
    HIGH = "high"
    EMERGENCY = "emergency"

    @property
    def api_value(self) -> int:
        return {
            PushoverPriority.NORMAL: 0,
            PushoverPriority.HIGH: 1,
            PushoverPriority.EMERGENCY: 2,
        }[self]


@dataclass(frozen=True)
class PushoverCredentials:
    app_token: str = field(repr=False)
    user_key: str = field(repr=False)

    def __post_init__(self) -> None:
        app_token = self.app_token.strip()
        user_key = self.user_key.strip()
        if not _TOKEN_PATTERN.fullmatch(app_token):
            raise PushoverConfigurationError(
                "The Pushover application token must contain exactly 30 letters or numbers."
            )
        if not _TOKEN_PATTERN.fullmatch(user_key):
            raise PushoverConfigurationError(
                "The Pushover user or group key must contain exactly 30 letters or numbers."
            )
        object.__setattr__(self, "app_token", app_token)
        object.__setattr__(self, "user_key", user_key)


@dataclass(frozen=True)
class PushoverConfiguration:
    credentials: PushoverCredentials = field(repr=False)
    device: str = ""

    def __post_init__(self) -> None:
        device = self.device.strip()
        if device and not _DEVICE_PATTERN.fullmatch(device):
            raise PushoverConfigurationError(
                "The optional Pushover device name must be 1-25 letters, numbers, "
                "underscores, or hyphens."
            )
        object.__setattr__(self, "device", device)


@dataclass(frozen=True)
class PushoverMessage:
    message: str
    title: str = "Automadin"
    priority: PushoverPriority = PushoverPriority.NORMAL
    retry_seconds: int = 60
    expire_seconds: int = 1800

    def __post_init__(self) -> None:
        message = self.message.strip()
        title = self.title.strip()
        if not message:
            raise PushoverConfigurationError("Notification message cannot be empty.")
        if len(message) > 1024:
            raise PushoverConfigurationError(
                "Notification message cannot exceed 1,024 characters."
            )
        if len(title) > 250:
            raise PushoverConfigurationError(
                "Notification title cannot exceed 250 characters."
            )
        try:
            priority = PushoverPriority(self.priority)
        except ValueError as exc:
            raise PushoverConfigurationError(
                "Notification priority must be normal, high, or emergency."
            ) from exc
        if priority is PushoverPriority.EMERGENCY:
            if not isinstance(self.retry_seconds, int) or isinstance(
                self.retry_seconds, bool
            ):
                raise PushoverConfigurationError(
                    "Emergency retry interval must be a whole number of seconds."
                )
            if not isinstance(self.expire_seconds, int) or isinstance(
                self.expire_seconds, bool
            ):
                raise PushoverConfigurationError(
                    "Emergency expiry must be a whole number of seconds."
                )
            if not 30 <= self.retry_seconds <= 10800:
                raise PushoverConfigurationError(
                    "Emergency retry interval must be between 30 and 10,800 seconds."
                )
            if not 30 <= self.expire_seconds <= 10800:
                raise PushoverConfigurationError(
                    "Emergency expiry must be between 30 and 10,800 seconds."
                )
            if self.retry_seconds > self.expire_seconds:
                raise PushoverConfigurationError(
                    "Emergency retry interval cannot exceed the expiry time."
                )
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "priority", priority)


@dataclass(frozen=True)
class PushoverSendResult:
    request_id: str
    receipt: str | None = None


def _credential_api() -> Any:
    if os.name != "nt":
        raise PushoverCredentialStoreError(
            "Secure Pushover credential storage is available only on Windows."
        )
    try:
        # ``win32cred.CredRead`` imports this helper only when it converts the
        # credential's timestamp.  Freezers cannot discover that native-module
        # dependency from the extension module, so keep the import explicit.
        import win32timezone  # type: ignore  # noqa: F401
        import win32cred  # type: ignore
    except ImportError as exc:
        raise PushoverCredentialStoreError(
            "Windows Credential Manager support is unavailable. Repair the "
            "Automadin installation and try again."
        ) from exc
    return win32cred


def _exception_winerror(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None


def save_pushover_credentials(
    credentials: PushoverCredentials,
    *,
    credential_api: Any | None = None,
) -> None:
    """Persist secrets in the current user's Windows Credential Manager."""
    api = credential_api or _credential_api()
    payload = json.dumps(
        {
            "schema_version": 1,
            "app_token": credentials.app_token,
            "user_key": credentials.user_key,
        },
        separators=(",", ":"),
    )
    try:
        api.CredWrite(
            {
                "Type": api.CRED_TYPE_GENERIC,
                "TargetName": PUSHOVER_CREDENTIAL_TARGET,
                "CredentialBlob": payload,
                "Persist": api.CRED_PERSIST_LOCAL_MACHINE,
                "UserName": "Automadin",
                "Comment": "Automadin default Pushover destination",
            },
            0,
        )
    except BaseException as exc:
        raise PushoverCredentialStoreError(
            "Windows Credential Manager could not save the Pushover credentials."
        ) from exc


def load_pushover_credentials(
    *,
    credential_api: Any | None = None,
) -> PushoverCredentials | None:
    """Load the default Pushover credentials without exposing their values."""
    api = credential_api or _credential_api()
    try:
        stored = api.CredRead(
            PUSHOVER_CREDENTIAL_TARGET,
            api.CRED_TYPE_GENERIC,
            0,
        )
    except BaseException as exc:
        if _exception_winerror(exc) == _ERROR_NOT_FOUND:
            return None
        raise PushoverCredentialStoreError(
            "Windows Credential Manager could not read the Pushover credentials."
        ) from exc

    blob = stored.get("CredentialBlob", b"") if isinstance(stored, Mapping) else b""
    try:
        if isinstance(blob, str):
            text = blob
        elif isinstance(blob, (bytes, bytearray)):
            raw_blob = bytes(blob)
            # pywin32 accepts a Unicode CredentialBlob and returns its UTF-16-LE
            # bytes. Keep UTF-8 compatibility for injected/test credential APIs.
            encoding = "utf-16-le" if b"\x00" in raw_blob else "utf-8"
            text = raw_blob.decode(encoding)
        else:
            raise ValueError("unsupported credential blob")
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported credential schema")
        return PushoverCredentials(
            str(payload.get("app_token", "")),
            str(payload.get("user_key", "")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise PushoverCredentialStoreError(
            "The saved Pushover credentials are unreadable. Remove and configure "
            "them again."
        ) from exc


def delete_pushover_credentials(*, credential_api: Any | None = None) -> None:
    api = credential_api or _credential_api()
    try:
        api.CredDelete(
            PUSHOVER_CREDENTIAL_TARGET,
            api.CRED_TYPE_GENERIC,
            0,
        )
    except BaseException as exc:
        if _exception_winerror(exc) == _ERROR_NOT_FOUND:
            return
        raise PushoverCredentialStoreError(
            "Windows Credential Manager could not remove the Pushover credentials."
        ) from exc


def load_pushover_configuration(db: Any) -> PushoverConfiguration:
    credentials = load_pushover_credentials()
    if credentials is None:
        raise PushoverConfigurationError(
            "Pushover is not configured. Open Tools > Pushover Notifications first."
        )
    device = ""
    if db is not None and hasattr(db, "get_app_setting"):
        try:
            device = db.get_app_setting(PUSHOVER_DEVICE_SETTING, "") or ""
        except BaseException as exc:
            raise PushoverConfigurationError(
                "Automadin could not read the optional Pushover device name."
            ) from exc
    return PushoverConfiguration(credentials, device)


def save_pushover_configuration(
    db: Any,
    configuration: PushoverConfiguration,
) -> None:
    # Write the secret first. A database failure leaves a safe all-device default,
    # never plaintext credentials in SQLite.
    save_pushover_credentials(configuration.credentials)
    if db is None or not hasattr(db, "set_app_setting"):
        if configuration.device:
            raise PushoverConfigurationError(
                "Automadin cannot save the optional Pushover device name."
            )
        return
    try:
        if configuration.device:
            db.set_app_setting(PUSHOVER_DEVICE_SETTING, configuration.device)
        elif hasattr(db, "delete_app_setting"):
            db.delete_app_setting(PUSHOVER_DEVICE_SETTING)
        else:
            db.set_app_setting(PUSHOVER_DEVICE_SETTING, "")
    except BaseException as exc:
        raise PushoverConfigurationError(
            "Automadin saved the Pushover keys securely but could not save the "
            "optional device name."
        ) from exc


def delete_pushover_configuration(db: Any) -> None:
    delete_pushover_credentials()
    if db is not None and hasattr(db, "delete_app_setting"):
        try:
            db.delete_app_setting(PUSHOVER_DEVICE_SETTING)
        except BaseException as exc:
            raise PushoverConfigurationError(
                "The Pushover keys were removed, but Automadin could not clear the "
                "saved device name."
            ) from exc


def _safe_http_failure(status: int) -> str:
    if status in {400, 401}:
        return "Pushover rejected the notification. Check the application token, user key, and device name."
    if status == 413:
        return "Pushover rejected the notification because it is too large."
    if status == 429:
        return "The Pushover account has reached its current message limit."
    if status >= 500:
        return "Pushover is temporarily unavailable. Try again later."
    return f"Pushover rejected the notification (HTTP {status})."


def _default_transport(request: Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS endpoint
            status = int(getattr(response, "status", response.getcode()))
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise PushoverDeliveryError(_safe_http_failure(exc.code)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise PushoverDeliveryError(
            "Automadin could not reach Pushover. Check the internet connection and try again."
        ) from exc
    if len(body) > _MAX_RESPONSE_BYTES:
        raise PushoverDeliveryError("Pushover returned an unexpectedly large response.")
    return status, body


PushoverTransport = Callable[[Request, float], tuple[int, bytes]]


def send_pushover_notification(
    configuration: PushoverConfiguration,
    notification: PushoverMessage,
    *,
    timeout: float = PUSHOVER_REQUEST_TIMEOUT_SECONDS,
    transport: PushoverTransport | None = None,
) -> PushoverSendResult:
    """Send one message and return only non-secret delivery identifiers."""
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > 60
    ):
        raise PushoverConfigurationError(
            "Pushover request timeout must be between 0 and 60 seconds."
        )
    payload: dict[str, str | int] = {
        "token": configuration.credentials.app_token,
        "user": configuration.credentials.user_key,
        "message": notification.message,
        "title": notification.title,
        "priority": notification.priority.api_value,
    }
    if configuration.device:
        payload["device"] = configuration.device
    if notification.priority is PushoverPriority.EMERGENCY:
        payload["retry"] = notification.retry_seconds
        payload["expire"] = notification.expire_seconds

    request = Request(
        PUSHOVER_API_URL,
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Automadin/1",
        },
        method="POST",
    )
    status, body = (transport or _default_transport)(request, float(timeout))
    if not 200 <= status < 300:
        raise PushoverDeliveryError(_safe_http_failure(status))
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PushoverDeliveryError(
            "Pushover returned an unreadable response."
        ) from exc
    if not isinstance(response, dict) or response.get("status") != 1:
        raise PushoverDeliveryError(
            "Pushover did not accept the notification. Check the saved configuration."
        )
    request_id = response.get("request")
    if not isinstance(request_id, str) or not request_id:
        raise PushoverDeliveryError(
            "Pushover accepted the request but returned no delivery identifier."
        )
    receipt = response.get("receipt")
    if receipt is not None and not isinstance(receipt, str):
        raise PushoverDeliveryError("Pushover returned an invalid emergency receipt.")
    if notification.priority is PushoverPriority.EMERGENCY and not receipt:
        raise PushoverDeliveryError(
            "Pushover accepted the emergency alert but returned no acknowledgement receipt."
        )
    return PushoverSendResult(request_id=request_id, receipt=receipt)
