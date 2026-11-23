"""Secure Telegram configuration and bounded Bot API delivery."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TELEGRAM_API_ROOT = "https://api.telegram.org"
TELEGRAM_CREDENTIAL_TARGET = "Automadin/Telegram/default"
TELEGRAM_CHAT_ID_SETTING = "telegram_default_chat_id"
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 10.0

_TOKEN_PATTERN = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{20,240}$")
_CHAT_ID_PATTERN = re.compile(r"^-?[1-9][0-9]{0,19}$")
_ERROR_NOT_FOUND = 1168
_MAX_RESPONSE_BYTES = 64 * 1024


class TelegramError(RuntimeError):
    """Base class for safe, user-presentable Telegram failures."""


class TelegramConfigurationError(TelegramError):
    """The local Telegram configuration is missing or invalid."""


class TelegramCredentialStoreError(TelegramError):
    """Windows Credential Manager could not complete an operation."""


class TelegramDeliveryError(TelegramError):
    """Telegram did not accept a message."""


@dataclass(frozen=True)
class TelegramCredentials:
    bot_token: str = field(repr=False)

    def __post_init__(self) -> None:
        token = self.bot_token.strip()
        if not _TOKEN_PATTERN.fullmatch(token):
            raise TelegramConfigurationError(
                "Enter the complete bot token supplied by Telegram BotFather."
            )
        object.__setattr__(self, "bot_token", token)


@dataclass(frozen=True)
class TelegramConfiguration:
    credentials: TelegramCredentials = field(repr=False)
    chat_id: str

    def __post_init__(self) -> None:
        chat_id = self.chat_id.strip()
        if not _CHAT_ID_PATTERN.fullmatch(chat_id):
            raise TelegramConfigurationError(
                "Telegram chat ID must be a non-zero whole number. Group chat IDs "
                "normally begin with a minus sign."
            )
        object.__setattr__(self, "chat_id", chat_id)


@dataclass(frozen=True)
class TelegramMessage:
    message: str
    title: str = "Automadin"

    def __post_init__(self) -> None:
        message = self.message.strip()
        title = self.title.strip()
        if not message:
            raise TelegramConfigurationError("Notification message cannot be empty.")
        text = f"{title}\n\n{message}" if title else message
        if len(text) > 4096:
            raise TelegramConfigurationError(
                "Telegram title and message cannot exceed 4,096 characters combined."
            )
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "title", title)

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.message}" if self.title else self.message


@dataclass(frozen=True)
class TelegramSendResult:
    message_id: int


def _credential_api() -> Any:
    if os.name != "nt":
        raise TelegramCredentialStoreError(
            "Secure Telegram credential storage is available only on Windows."
        )
    try:
        import win32timezone  # type: ignore  # noqa: F401
        import win32cred  # type: ignore
    except ImportError as exc:
        raise TelegramCredentialStoreError(
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


def save_telegram_credentials(
    credentials: TelegramCredentials,
    *,
    credential_api: Any | None = None,
) -> None:
    api = credential_api or _credential_api()
    payload = json.dumps(
        {"schema_version": 1, "bot_token": credentials.bot_token},
        separators=(",", ":"),
    )
    try:
        api.CredWrite(
            {
                "Type": api.CRED_TYPE_GENERIC,
                "TargetName": TELEGRAM_CREDENTIAL_TARGET,
                "CredentialBlob": payload,
                "Persist": api.CRED_PERSIST_LOCAL_MACHINE,
                "UserName": "Automadin",
                "Comment": "Automadin default Telegram destination",
            },
            0,
        )
    except BaseException as exc:
        raise TelegramCredentialStoreError(
            "Windows Credential Manager could not save the Telegram bot token."
        ) from exc


def load_telegram_credentials(
    *,
    credential_api: Any | None = None,
) -> TelegramCredentials | None:
    api = credential_api or _credential_api()
    try:
        stored = api.CredRead(
            TELEGRAM_CREDENTIAL_TARGET,
            api.CRED_TYPE_GENERIC,
            0,
        )
    except BaseException as exc:
        if _exception_winerror(exc) == _ERROR_NOT_FOUND:
            return None
        raise TelegramCredentialStoreError(
            "Windows Credential Manager could not read the Telegram bot token."
        ) from exc

    blob = stored.get("CredentialBlob", b"") if isinstance(stored, Mapping) else b""
    try:
        if isinstance(blob, str):
            text = blob
        elif isinstance(blob, (bytes, bytearray)):
            raw_blob = bytes(blob)
            encoding = "utf-16-le" if b"\x00" in raw_blob else "utf-8"
            text = raw_blob.decode(encoding)
        else:
            raise ValueError("unsupported credential blob")
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported credential schema")
        return TelegramCredentials(str(payload.get("bot_token", "")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise TelegramCredentialStoreError(
            "The saved Telegram bot token is unreadable. Remove and configure it again."
        ) from exc


def delete_telegram_credentials(*, credential_api: Any | None = None) -> None:
    api = credential_api or _credential_api()
    try:
        api.CredDelete(
            TELEGRAM_CREDENTIAL_TARGET,
            api.CRED_TYPE_GENERIC,
            0,
        )
    except BaseException as exc:
        if _exception_winerror(exc) == _ERROR_NOT_FOUND:
            return
        raise TelegramCredentialStoreError(
            "Windows Credential Manager could not remove the Telegram bot token."
        ) from exc


def load_telegram_configuration(db: Any) -> TelegramConfiguration:
    credentials = load_telegram_credentials()
    if credentials is None:
        raise TelegramConfigurationError(
            "Telegram is not configured. Open Tools > Telegram Notifications first."
        )
    try:
        chat_id = db.get_app_setting(TELEGRAM_CHAT_ID_SETTING, "") or ""
    except BaseException as exc:
        raise TelegramConfigurationError(
            "Automadin could not read the Telegram chat ID."
        ) from exc
    return TelegramConfiguration(credentials, chat_id)


def save_telegram_configuration(db: Any, configuration: TelegramConfiguration) -> None:
    save_telegram_credentials(configuration.credentials)
    if db is None or not hasattr(db, "set_app_setting"):
        raise TelegramConfigurationError("Automadin cannot save the Telegram chat ID.")
    try:
        db.set_app_setting(TELEGRAM_CHAT_ID_SETTING, configuration.chat_id)
    except BaseException as exc:
        raise TelegramConfigurationError(
            "Automadin saved the bot token securely but could not save the chat ID."
        ) from exc


def delete_telegram_configuration(db: Any) -> None:
    delete_telegram_credentials()
    if db is not None and hasattr(db, "delete_app_setting"):
        try:
            db.delete_app_setting(TELEGRAM_CHAT_ID_SETTING)
        except BaseException as exc:
            raise TelegramConfigurationError(
                "The Telegram bot token was removed, but Automadin could not clear "
                "the saved chat ID."
            ) from exc


def _safe_http_failure(status: int) -> str:
    if status in {400, 401, 403, 404}:
        return "Telegram rejected the message. Check the bot token and chat ID."
    if status == 429:
        return "Telegram is temporarily rate-limiting this bot. Try again later."
    if status >= 500:
        return "Telegram is temporarily unavailable. Try again later."
    return f"Telegram rejected the message (HTTP {status})."


def _default_transport(request: Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS root
            status = int(getattr(response, "status", response.getcode()))
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        # Bot tokens are part of Telegram API URLs. Suppress the urllib
        # exception context so a traceback cannot copy that URL into logs.
        raise TelegramDeliveryError(_safe_http_failure(exc.code)) from None
    except (URLError, TimeoutError, OSError):
        raise TelegramDeliveryError(
            "Automadin could not reach Telegram. Check the internet connection and try again."
        ) from None
    if len(body) > _MAX_RESPONSE_BYTES:
        raise TelegramDeliveryError("Telegram returned an unexpectedly large response.")
    return status, body


TelegramTransport = Callable[[Request, float], tuple[int, bytes]]


def send_telegram_message(
    configuration: TelegramConfiguration,
    message: TelegramMessage,
    *,
    timeout: float = TELEGRAM_REQUEST_TIMEOUT_SECONDS,
    transport: TelegramTransport | None = None,
) -> TelegramSendResult:
    """Send one plain-text Bot API message without exposing the bot token."""
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > 60
    ):
        raise TelegramConfigurationError(
            "Telegram request timeout must be between 0 and 60 seconds."
        )
    payload = json.dumps(
        {"chat_id": configuration.chat_id, "text": message.text},
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        f"{TELEGRAM_API_ROOT}/bot{configuration.credentials.bot_token}/sendMessage",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Automadin/1",
        },
        method="POST",
    )
    status, body = (transport or _default_transport)(request, float(timeout))
    if not 200 <= status < 300:
        raise TelegramDeliveryError(_safe_http_failure(status))
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramDeliveryError("Telegram returned an unreadable response.") from exc
    result = response.get("result") if isinstance(response, dict) else None
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if (
        not isinstance(response, dict)
        or response.get("ok") is not True
        or not isinstance(message_id, int)
    ):
        raise TelegramDeliveryError(
            "Telegram did not accept the message. Check the saved configuration."
        )
    return TelegramSendResult(message_id=message_id)
