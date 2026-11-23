"""Detect and format useful identity metadata from browser profile folders."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from enum import IntFlag
from pathlib import Path
from typing import Any

from automadin.services.browser_profile_paths import path_has_redirect

_MAX_IDENTITY_TEXT_CHARS = 320
_MAX_CHECKED_AT_CHARS = 64


def _validated_identity_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Browser profile {field_name} must be text.")
    if len(value) > _MAX_IDENTITY_TEXT_CHARS:
        raise ValueError(
            f"Browser profile {field_name} cannot exceed "
            f"{_MAX_IDENTITY_TEXT_CHARS} characters."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(
            f"Browser profile {field_name} cannot contain control characters."
        )
    return value


def _validated_identity_checked_at(value: object, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise TypeError("Profile identity observation checked_at must be text.")
    if not value:
        if allow_empty:
            return ""
        raise ValueError("Profile identity observation checked_at cannot be empty.")
    if len(value) > _MAX_CHECKED_AT_CHARS:
        raise ValueError("Profile identity observation checked_at is too long.")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
    except ValueError as exc:
        raise ValueError(
            "Profile identity observation checked_at must be an ISO timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "Profile identity observation checked_at must include a timezone."
        )
    return value


@dataclass(frozen=True)
class BrowserProfileIdentity:
    browser: str = ""
    source_profile: str = ""
    account_name: str = ""
    account_email: str = ""

    def friendly_label(self, fallback: str = "") -> str:
        context = " / ".join(value for value in (self.browser, self.source_profile) if value)
        if self.account_name and self.account_email:
            account = f"{self.account_name} ({self.account_email})"
        else:
            account = self.account_name or self.account_email
        return " — ".join(value for value in (context, account) if value) or fallback

    def suggested_name(self, fallback: str = "") -> str:
        return " - ".join(
            value for value in (self.browser, self.source_profile, self.account_name) if value
        ) or fallback


class ProfileIdentityField(IntFlag):
    """Refreshable browser-profile identity fields."""

    SOURCE_PROFILE = 1
    ACCOUNT_NAME = 2
    ACCOUNT_EMAIL = 4
    ALL = SOURCE_PROFILE | ACCOUNT_NAME | ACCOUNT_EMAIL


@dataclass(frozen=True, slots=True)
class ProfileIdentityObservation:
    """One validated, field-scoped profile identity refresh result."""

    checked_at: str
    attempted_mask: ProfileIdentityField
    update_mask: ProfileIdentityField
    verified_mask: ProfileIdentityField
    identity: BrowserProfileIdentity
    detail_code: str = ""

    def __post_init__(self) -> None:
        _validated_identity_checked_at(self.checked_at, allow_empty=False)
        if not isinstance(self.identity, BrowserProfileIdentity):
            raise TypeError(
                "Profile identity observation identity must be BrowserProfileIdentity."
            )
        for field_name in (
            "browser",
            "source_profile",
            "account_name",
            "account_email",
        ):
            _validated_identity_text(
                getattr(self.identity, field_name),
                field_name,
            )
        if not isinstance(self.detail_code, str):
            raise TypeError("Profile identity observation detail_code must be text.")

        all_bits = int(ProfileIdentityField.ALL)
        normalized: dict[str, ProfileIdentityField] = {}
        for name in ("attempted_mask", "update_mask", "verified_mask"):
            value = getattr(self, name)
            if type(value) not in (int, ProfileIdentityField):
                raise TypeError(f"Profile identity observation {name} must be a mask.")
            integer = int(value)
            if integer < 0 or integer & ~all_bits:
                raise ValueError(
                    f"Profile identity observation {name} contains invalid bits."
                )
            normalized[name] = ProfileIdentityField(integer)
            object.__setattr__(self, name, normalized[name])

        attempted = normalized["attempted_mask"]
        update = normalized["update_mask"]
        verified = normalized["verified_mask"]
        if update & ~attempted:
            raise ValueError(
                "Profile identity observation update_mask must be a subset of "
                "attempted_mask."
            )
        if verified & ~update:
            raise ValueError(
                "Profile identity observation verified_mask must be a subset of "
                "update_mask."
            )


@dataclass(frozen=True)
class StoredBrowserProfile:
    profile_id: str
    name: str
    created_at: str
    identity: BrowserProfileIdentity
    engine: str = ""
    legacy_storage_name: str = ""
    identity_checked_at: str = ""
    identity_stale_mask: int = int(ProfileIdentityField.ALL)

    @property
    def display_label(self) -> str:
        details = self.identity.friendly_label()
        return f"{self.name} — {details}" if details else self.name


@dataclass(frozen=True)
class BrowserProfileTarget:
    """A manifest-verified profile that may be offered for browser discovery."""

    profile_id: str
    name: str
    engine: str


def compatible_engine_for_browser(browser: str) -> str:
    return {
        "chromium": "chromium",
        "chrome": "chrome",
        "edge": "msedge",
        "firefox": "firefox",
    }.get(browser.strip().casefold(), "")


def _is_real_directory(path: Path) -> bool:
    """Reject missing directories and filesystem redirects such as junctions."""
    lexical = Path(os.path.abspath(path.expanduser()))
    if path_has_redirect(lexical):
        return False
    try:
        metadata = os.lstat(lexical)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode)


def profile_layout_ready(path: str | Path, engine: str) -> bool:
    root = Path(path)
    if not _is_real_directory(root):
        return False
    if engine in {"chrome", "msedge", "chromium"}:
        return _is_real_directory(root / "Default")
    return engine == "firefox"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _browser_from_path(path: Path) -> str:
    normalized = "/".join(part.casefold() for part in path.parts)
    if "bravesoftware/brave-browser" in normalized:
        return "Brave"
    if "microsoft/edge" in normalized:
        return "Edge"
    if "google/chrome" in normalized:
        return "Chrome"
    if "mozilla/firefox" in normalized:
        return "Firefox"
    return ""


def _first_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def detect_browser_profile(
    source: str | Path,
    browser_hint: str = "",
) -> BrowserProfileIdentity:
    """Read non-secret profile/account identity. Malformed files are ignored."""
    path = Path(source)
    browser = browser_hint.strip() or _browser_from_path(path)
    if browser.casefold() == "firefox" or (path / "signedInUser.json").exists():
        signed_in = _read_json(path / "signedInUser.json")
        account = signed_in.get("accountData", {})
        if not isinstance(account, dict):
            account = {}
        return BrowserProfileIdentity(
            browser=browser or "Firefox",
            source_profile=path.name,
            account_name=_first_text(
                account.get("displayName"), account.get("display_name"), account.get("name")
            ),
            account_email=_first_text(account.get("email")),
        )

    preferences = _read_json(path / "Preferences")
    profile = preferences.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}
    accounts = preferences.get("account_info", [])
    account = next((item for item in accounts if isinstance(item, dict)), {}) if isinstance(accounts, list) else {}

    local_state = _read_json(path.parent / "Local State")
    state_profile = local_state.get("profile", {})
    info_cache = state_profile.get("info_cache", {}) if isinstance(state_profile, dict) else {}
    cached = info_cache.get(path.name, {}) if isinstance(info_cache, dict) else {}
    if not isinstance(cached, dict):
        cached = {}

    return BrowserProfileIdentity(
        browser=browser,
        source_profile=_first_text(profile.get("name"), cached.get("name"), path.name),
        account_name=_first_text(
            account.get("full_name"), cached.get("gaia_name"), account.get("given_name")
        ),
        account_email=_first_text(account.get("email"), cached.get("user_name")),
    )


def profile_aware_device_name(device: object, labels: dict[str, str]) -> str:
    """Present browser capability separately from its profile identity."""
    from automadin.models.device import BrowserDevice, BrowserDeviceConfig

    if not isinstance(device, BrowserDevice):
        return getattr(device, "name", getattr(device, "id", "Unknown device"))
    if device.config.cdp_endpoint:
        return device.config.to_display_name()
    base_config = BrowserDeviceConfig(
        engine=device.config.engine,
        headless=device.config.headless,
        screen_mode=device.config.screen_mode,
    )
    base = base_config.to_display_name()
    if device.config.profile_id:
        identity = labels.get(
            device.config.profile_id,
            device.config.profile_name or f"profile {device.config.profile_id[:8]}",
        )
        return f"{base} · Profile: {identity}"
    if device.config.profile_name:
        return f"{base} · Profile: {device.config.profile_name} (needs setup)"
    return f"{base} · Temporary profile"
