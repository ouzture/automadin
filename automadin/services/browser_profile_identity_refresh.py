"""Strict, read-only observation and persistence of browser profile identity.

This module deliberately has no Qt or browser-runtime dependency.  It reads a
small allow-list of non-secret browser files and never launches a browser,
follows a filesystem redirect, or chooses an arbitrary account.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseRegistry,
    ProfileUse,
    default_browser_profile_lease_registry,
)
from automadin.services.browser_profile_metadata import (
    BrowserProfileIdentity,
    ProfileIdentityField,
    ProfileIdentityObservation,
)
from automadin.services.browser_profile_paths import canonical_profile_id, path_has_redirect
from automadin.services.browser_profile_storage import (
    ProfileStorageStatus,
    inspect_profile_storage,
)

_CHROMIUM_ENGINES = frozenset({"chromium", "chrome", "msedge"})
_ACCOUNT_FIELDS = (
    ProfileIdentityField.ACCOUNT_NAME | ProfileIdentityField.ACCOUNT_EMAIL
)
_FIELD_NAMES = (
    (ProfileIdentityField.SOURCE_PROFILE, "source_profile"),
    (ProfileIdentityField.ACCOUNT_NAME, "account_name"),
    (ProfileIdentityField.ACCOUNT_EMAIL, "account_email"),
)
_MAX_PREFERENCES_BYTES = 4 * 1024 * 1024
_MAX_LOCAL_STATE_BYTES = 2 * 1024 * 1024
_MAX_FIREFOX_USER_BYTES = 512 * 1024
_MAX_TEXT_CHARS = 320
_LOGGER = logging.getLogger(__name__)


class IdentityRefreshStatus(str, Enum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    PARTIAL = "partial"
    BUSY = "busy"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"
    NOT_READY = "not_ready"
    CLEANUP_FAILED = "cleanup_failed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProfileIdentityRefreshOutcome:
    """Public refresh result containing no observed identity values."""

    profile_id: str
    status: IdentityRefreshStatus
    checked_at: str
    changed_fields: tuple[str, ...] = ()
    stale_fields: tuple[str, ...] = ()
    message: str = ""


class _ReadState(str, Enum):
    OK = "ok"
    ABSENT = "absent"
    UNSAFE = "unsafe"
    OVERSIZED = "oversized"
    CHANGED = "changed"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class _JsonRead:
    state: _ReadState
    value: dict[str, Any] | None = None


def _checked_at(value: str | None = None) -> str:
    if value is not None:
        if not isinstance(value, str) or not value:
            raise ValueError("checked_at must be non-empty text.")
        return value
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_root(path: str | Path) -> Path | None:
    raw = Path(path).expanduser()
    lexical = Path(os.path.abspath(raw))
    if path_has_redirect(lexical):
        return None
    try:
        metadata = os.lstat(lexical)
    except OSError:
        return None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        return None
    return lexical


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_size,
        first.st_mtime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    )


def _open_readonly_no_follow(path: Path, root: Path) -> int:
    """Open one file without following a final Windows reparse point."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), "Could not open identity metadata.")
    try:
        information = _FileAttributeTagInfo()
        if not get_information(
            handle,
            9,  # FileAttributeTagInfo
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise OSError(
                ctypes.get_last_error(),
                "Could not inspect identity metadata.",
            )
        if information.file_attributes & 0x00000400:  # REPARSE_POINT
            raise OSError("Identity metadata is a filesystem redirect.")
        required = get_final_path(handle, None, 0, 0)
        if not required:
            raise OSError(
                ctypes.get_last_error(),
                "Could not resolve identity metadata.",
            )
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = get_final_path(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            raise OSError(
                ctypes.get_last_error(),
                "Could not resolve identity metadata.",
            )
        final_name = buffer.value
        if final_name.startswith("\\\\?\\UNC\\"):
            final_name = "\\\\" + final_name[8:]
        elif final_name.startswith("\\\\?\\"):
            final_name = final_name[4:]
        root_name = os.path.normcase(os.path.abspath(root))
        final_name = os.path.normcase(os.path.abspath(final_name))
        try:
            contained = os.path.commonpath((root_name, final_name)) == root_name
        except ValueError:
            contained = False
        if not contained:
            raise OSError("Identity metadata escaped managed profile storage.")
        descriptor = msvcrt.open_osfhandle(int(handle), flags)
        handle = None
        return descriptor
    finally:
        if handle is not None:
            close_handle(handle)


def _read_once(root: Path, parts: tuple[str, ...], limit: int) -> tuple[_ReadState, bytes]:
    current = root
    for component in parts[:-1]:
        current = current / component
        try:
            metadata = os.lstat(current)
            resolved = current.resolve(strict=True)
        except FileNotFoundError:
            return _ReadState.ABSENT, b""
        except (OSError, RuntimeError):
            return _ReadState.UNSAFE, b""
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != current
        ):
            return _ReadState.UNSAFE, b""

    path = current / parts[-1]
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return _ReadState.ABSENT, b""
    except OSError:
        return _ReadState.UNSAFE, b""
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return _ReadState.UNSAFE, b""
    if before.st_size > limit:
        return _ReadState.OVERSIZED, b""

    descriptor: int | None = None
    try:
        descriptor = _open_readonly_no_follow(path, root)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
            return _ReadState.CHANGED, b""
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            return _ReadState.OVERSIZED, b""
        after_handle = os.fstat(descriptor)
        after_path = os.lstat(path)
        if (
            stat.S_ISLNK(after_path.st_mode)
            or not _same_file(opened, after_handle)
            or not _same_file(opened, after_path)
        ):
            return _ReadState.CHANGED, b""
        return _ReadState.OK, payload
    except FileNotFoundError:
        return _ReadState.CHANGED, b""
    except OSError:
        return _ReadState.UNSAFE, b""
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(root: Path, parts: tuple[str, ...], limit: int) -> _JsonRead:
    state, payload = _read_once(root, parts, limit)
    if state is _ReadState.CHANGED:
        state, payload = _read_once(root, parts, limit)
    if state is not _ReadState.OK:
        return _JsonRead(state)
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        return _JsonRead(_ReadState.INVALID)
    if not isinstance(value, dict):
        return _JsonRead(_ReadState.INVALID)
    return _JsonRead(_ReadState.OK, value)


def _clean_text(value: object) -> str | None:
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    # Browser display values do not need embedded control characters.  Collapse
    # whitespace so persisted labels stay one line and have a strict bound.
    cleaned = " ".join(value.split())
    if len(cleaned) > _MAX_TEXT_CHARS:
        return None
    return cleaned


def _first_clean(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    saw_empty = False
    for key in keys:
        if key not in mapping:
            continue
        value = _clean_text(mapping[key])
        if value is None:
            return None
        if value:
            return value
        saw_empty = True
    return "" if saw_empty else ""


def _chromium_account(
    value: dict[str, Any],
) -> tuple[ProfileIdentityField, ProfileIdentityField, str, str, str]:
    raw_accounts = value.get("account_info")
    if not isinstance(raw_accounts, list):
        return (
            ProfileIdentityField(0),
            ProfileIdentityField(0),
            "",
            "",
            "chromium_account_unknown",
        )
    raw_primary = [
        item
        for item in raw_accounts
        if isinstance(item, dict)
        and (
            item.get("is_primary_account") is True
            or item.get("is_primary") is True
        )
    ]
    if len(raw_primary) > 1:
        return (
            ProfileIdentityField(0),
            ProfileIdentityField(0),
            "",
            "",
            "chromium_account_ambiguous",
        )

    def parsed_account(
        item: object,
        *,
        strict: bool = False,
    ) -> tuple[dict[str, Any], str, str] | None:
        if not isinstance(item, dict):
            return None
        name = _first_clean(item, ("full_name", "given_name"))
        email = _first_clean(item, ("email",))
        if strict and (name is None or email is None):
            return None
        name = "" if name is None else name
        email = "" if email is None else email
        if not (name or email):
            return None
        return item, name, email

    if raw_primary:
        selected = parsed_account(raw_primary[0], strict=True)
        if selected is None:
            return (
                ProfileIdentityField(0),
                ProfileIdentityField(0),
                "",
                "",
                "chromium_primary_account_invalid",
            )
        valid = [selected]
    else:
        valid = [
            parsed
            for item in raw_accounts
            if (parsed := parsed_account(item)) is not None
        ]
    if not valid:
        if raw_accounts:
            return (
                ProfileIdentityField(0),
                ProfileIdentityField(0),
                "",
                "",
                "chromium_account_unknown",
            )
        return _ACCOUNT_FIELDS, _ACCOUNT_FIELDS, "", "", "chromium_signed_out"
    selected: tuple[dict[str, Any], str, str] | None = None
    if len(valid) == 1:
        selected = valid[0]
    else:
        return (
            ProfileIdentityField(0),
            ProfileIdentityField(0),
            "",
            "",
            "chromium_account_ambiguous",
        )
    verified = ProfileIdentityField(0)
    if selected[1]:
        verified |= ProfileIdentityField.ACCOUNT_NAME
    if selected[2]:
        verified |= ProfileIdentityField.ACCOUNT_EMAIL
    return (
        _ACCOUNT_FIELDS,
        verified,
        selected[1],
        selected[2],
        "chromium_account_verified",
    )


def _chromium_observation(root: Path, checked_at: str) -> ProfileIdentityObservation:
    attempted = ProfileIdentityField.ALL
    update = ProfileIdentityField(0)
    verified = ProfileIdentityField(0)
    source_profile = account_name = account_email = ""
    codes: list[str] = []

    # Local State is inspected only at the exact info_cache.Default entry.  It
    # is a cache and therefore never authoritative enough to overwrite a
    # persisted source name.
    local_state = _read_json(root, ("Local State",), _MAX_LOCAL_STATE_BYTES)
    if local_state.state is _ReadState.OK:
        state_profile = local_state.value.get("profile")
        info_cache = (
            state_profile.get("info_cache")
            if isinstance(state_profile, dict)
            else None
        )
        cached_default = (
            info_cache.get("Default") if isinstance(info_cache, dict) else None
        )
        codes.append(
            "chromium_default_cache_seen"
            if isinstance(cached_default, dict)
            else "chromium_default_cache_unknown"
        )
    else:
        codes.append(f"local_state_{local_state.state.value}")

    preferences = _read_json(
        root,
        ("Default", "Preferences"),
        _MAX_PREFERENCES_BYTES,
    )
    if preferences.state is _ReadState.OK:
        profile = preferences.value.get("profile")
        if isinstance(profile, dict) and "name" in profile:
            cleaned_source = _clean_text(profile["name"])
            if cleaned_source is not None:
                source_profile = cleaned_source
                update |= ProfileIdentityField.SOURCE_PROFILE
                verified |= ProfileIdentityField.SOURCE_PROFILE
            else:
                codes.append("chromium_source_invalid")
        else:
            codes.append("chromium_source_unknown")
        (
            account_update,
            account_verified,
            account_name,
            account_email,
            code,
        ) = _chromium_account(preferences.value)
        codes.append(code)
        update |= account_update
        verified |= account_verified
    else:
        codes.append(f"preferences_{preferences.state.value}")

    return ProfileIdentityObservation(
        checked_at=checked_at,
        attempted_mask=attempted,
        update_mask=update,
        verified_mask=verified,
        identity=BrowserProfileIdentity(
            source_profile=source_profile,
            account_name=account_name,
            account_email=account_email,
        ),
        detail_code=";".join(codes),
    )


def _firefox_observation(root: Path, checked_at: str) -> ProfileIdentityObservation:
    attempted = _ACCOUNT_FIELDS
    try:
        root_before = os.stat(root)
    except OSError:
        root_before = None
    read = _read_json(root, ("signedInUser.json",), _MAX_FIREFOX_USER_BYTES)
    update = ProfileIdentityField(0)
    account_name = account_email = ""
    detail_code = f"firefox_user_{read.state.value}"
    if read.state is _ReadState.ABSENT:
        confirmed = _read_json(
            root,
            ("signedInUser.json",),
            _MAX_FIREFOX_USER_BYTES,
        )
        try:
            root_after = os.stat(root)
        except OSError:
            root_after = None
        if (
            confirmed.state is _ReadState.ABSENT
            and root_before is not None
            and root_after is not None
            and _same_file(root_before, root_after)
        ):
            update = _ACCOUNT_FIELDS
            detail_code = "firefox_signed_out"
        else:
            detail_code = "firefox_user_changed"
    elif read.state is _ReadState.OK:
        account = read.value.get("accountData")
        if isinstance(account, dict):
            account_name = _first_clean(
                account,
                ("displayName", "display_name", "name"),
            )
            account_email = _first_clean(account, ("email",))
            if account_name is None:
                account_name = ""
            if account_email is None:
                account_email = ""
            if (
                account_name or account_email
            ):
                update = _ACCOUNT_FIELDS
                verified = ProfileIdentityField(0)
                if account_name:
                    verified |= ProfileIdentityField.ACCOUNT_NAME
                if account_email:
                    verified |= ProfileIdentityField.ACCOUNT_EMAIL
                detail_code = "firefox_account_verified"
            else:
                account_name = account_email = ""
                detail_code = "firefox_account_invalid"
        else:
            detail_code = "firefox_account_unknown"
    return ProfileIdentityObservation(
        checked_at=checked_at,
        attempted_mask=attempted,
        update_mask=update,
        verified_mask=(
            verified
            if read.state is _ReadState.OK and update
            else update
        ),
        identity=BrowserProfileIdentity(
            account_name=account_name,
            account_email=account_email,
        ),
        detail_code=detail_code,
    )


def observe_browser_profile_identity(
    profile_path: str | Path,
    engine: str,
    *,
    checked_at: str | None = None,
) -> ProfileIdentityObservation:
    """Observe identity from a verified managed root without changing it."""

    timestamp = _checked_at(checked_at)
    root = _safe_root(profile_path)
    if engine in _CHROMIUM_ENGINES:
        attempted = ProfileIdentityField.ALL
    elif engine == "firefox":
        attempted = _ACCOUNT_FIELDS
    else:
        attempted = ProfileIdentityField(0)
    if root is None:
        return ProfileIdentityObservation(
            checked_at=timestamp,
            attempted_mask=attempted,
            update_mask=ProfileIdentityField(0),
            verified_mask=ProfileIdentityField(0),
            identity=BrowserProfileIdentity(),
            detail_code="unsafe_profile_root",
        )
    if engine in _CHROMIUM_ENGINES:
        return _chromium_observation(root, timestamp)
    if engine == "firefox":
        return _firefox_observation(root, timestamp)
    return ProfileIdentityObservation(
        checked_at=timestamp,
        attempted_mask=ProfileIdentityField(0),
        update_mask=ProfileIdentityField(0),
        verified_mask=ProfileIdentityField(0),
        identity=BrowserProfileIdentity(),
        detail_code="unsupported_engine",
    )


def _field_tuple(mask: int | ProfileIdentityField) -> tuple[str, ...]:
    normalized = ProfileIdentityField(int(mask))
    return tuple(name for bit, name in _FIELD_NAMES if normalized & bit)


def _identity_values(record: object) -> dict[str, str]:
    identity = getattr(record, "identity", None)
    return {
        name: str(getattr(identity, name, ""))
        for _, name in _FIELD_NAMES
    }


def _reconcile_partial_account(
    observation: ProfileIdentityObservation,
    record: object,
) -> ProfileIdentityObservation:
    """Preserve a matching account counterpart while still marking it stale."""

    account_update = observation.update_mask & _ACCOUNT_FIELDS
    account_verified = observation.verified_mask & _ACCOUNT_FIELDS
    if account_update != _ACCOUNT_FIELDS or account_verified not in (
        ProfileIdentityField.ACCOUNT_NAME,
        ProfileIdentityField.ACCOUNT_EMAIL,
    ):
        return observation
    before = _identity_values(record)
    if account_verified == ProfileIdentityField.ACCOUNT_EMAIL:
        if observation.identity.account_email != before["account_email"]:
            return observation
        update_mask = observation.update_mask & ~ProfileIdentityField.ACCOUNT_NAME
        identity = replace(
            observation.identity,
            account_name=before["account_name"],
        )
    else:
        if observation.identity.account_name != before["account_name"]:
            return observation
        update_mask = observation.update_mask & ~ProfileIdentityField.ACCOUNT_EMAIL
        identity = replace(
            observation.identity,
            account_email=before["account_email"],
        )
    return replace(
        observation,
        update_mask=update_mask,
        identity=identity,
    )


def _cancelled(
    profile_id: str,
    checked_at: str,
) -> ProfileIdentityRefreshOutcome:
    return ProfileIdentityRefreshOutcome(
        profile_id,
        IdentityRefreshStatus.CANCELLED,
        checked_at,
        message="The profile identity check was cancelled.",
    )


def _refresh_with_lease(
    canonical_id: str,
    database: object,
    profiles_root: str | Path | None,
    interrupted: Callable[[], bool] | None,
    started_at: str,
) -> ProfileIdentityRefreshOutcome:
    record = database.get_profile(canonical_id)
    if record is None:
        return ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.NOT_FOUND,
            started_at,
            message="The browser profile no longer exists.",
        )
    if interrupted is not None and interrupted():
        return _cancelled(canonical_id, started_at)

    expected_engine = str(getattr(record, "engine", ""))
    legacy_name = str(getattr(record, "legacy_storage_name", ""))
    inspection = inspect_profile_storage(
        canonical_id,
        expected_engine or None,
        legacy_storage_name=legacy_name,
        profiles_root=profiles_root,
    )
    if (
        inspection.status is not ProfileStorageStatus.READY
        or inspection.manifest is None
        or inspection.manifest.profile_id != canonical_id
        or inspection.manifest.engine != expected_engine
    ):
        return ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.NOT_READY,
            started_at,
            stale_fields=_field_tuple(ProfileIdentityField.ALL),
            message=(
                "The profile storage could not be verified. Repair the "
                "profile data before checking its identity."
            ),
        )

    try:
        storage_before = os.stat(inspection.path)
    except OSError:
        storage_before = None
    observation = observe_browser_profile_identity(
        inspection.path,
        inspection.manifest.engine,
    )
    if interrupted is not None and interrupted():
        return _cancelled(canonical_id, observation.checked_at)
    observation = _reconcile_partial_account(observation, record)
    final_inspection = inspect_profile_storage(
        canonical_id,
        expected_engine,
        legacy_storage_name=legacy_name,
        profiles_root=profiles_root,
    )
    if (
        final_inspection.status is not ProfileStorageStatus.READY
        or final_inspection.manifest is None
        or final_inspection.path != inspection.path
        or final_inspection.manifest != inspection.manifest
        or storage_before is None
    ):
        return ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.NOT_READY,
            observation.checked_at,
            stale_fields=_field_tuple(ProfileIdentityField.ALL),
            message=(
                "The profile storage changed while its identity was being checked. "
                "Check it again."
            ),
        )
    try:
        storage_after = os.stat(final_inspection.path)
    except OSError:
        storage_after = None
    if storage_after is None or not _same_file(storage_before, storage_after):
        return ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.NOT_READY,
            observation.checked_at,
            stale_fields=_field_tuple(ProfileIdentityField.ALL),
            message=(
                "The profile storage changed while its identity was being checked. "
                "Check it again."
            ),
        )

    if interrupted is not None and interrupted():
        return _cancelled(canonical_id, observation.checked_at)

    before_values = _identity_values(record)
    after = database.apply_profile_identity_observation(
        canonical_id,
        observation,
        expected_engine=expected_engine,
    )
    if after is None:
        return ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.NOT_FOUND,
            observation.checked_at,
            message=(
                "The profile changed while its identity was being checked. "
                "Check it again."
            ),
        )
    after_values = _identity_values(after)
    changed = tuple(
        name
        for bit, name in _FIELD_NAMES
        if observation.update_mask & bit
        and before_values[name] != after_values[name]
    )
    stale_mask = int(
        getattr(
            after,
            "identity_stale_mask",
            int(ProfileIdentityField.ALL & ~observation.verified_mask),
        )
    )
    stale = _field_tuple(stale_mask)
    if stale:
        return ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.PARTIAL,
            observation.checked_at,
            changed_fields=changed,
            stale_fields=stale,
            message=(
                "The profile identity was checked, but some fields remain "
                "unverified. Stored values were retained when they still matched "
                "the verified account."
            ),
        )
    if changed:
        return ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.UPDATED,
            observation.checked_at,
            changed_fields=changed,
            message="The profile identity was updated.",
        )
    return ProfileIdentityRefreshOutcome(
        canonical_id,
        IdentityRefreshStatus.UNCHANGED,
        observation.checked_at,
        message="The profile identity is already up to date.",
    )


def refresh_browser_profile_identity(
    profile_id: str | UUID,
    database: object,
    *,
    lease_registry: BrowserProfileLeaseRegistry = (
        default_browser_profile_lease_registry
    ),
    profiles_root: str | Path | None = None,
    interrupted: Callable[[], bool] | None = None,
) -> ProfileIdentityRefreshOutcome:
    """Refresh one profile under an exclusive maintenance lease."""

    canonical_id = canonical_profile_id(profile_id)
    started_at = _checked_at()
    lease = None
    database_cleanup_failed = False
    outcome: ProfileIdentityRefreshOutcome
    try:
        try:
            lease = lease_registry.acquire_maintenance(
                canonical_id,
                ProfileUse(
                    kind="identity_refresh",
                    label="Profile identity check",
                    operation_id=str(uuid4()),
                ),
            )
        except BrowserProfileInUseError as exc:
            label = _clean_text(exc.current.use.label)
            if not label or len(label) > 80:
                label = "another operation"
            return ProfileIdentityRefreshOutcome(
                canonical_id,
                IdentityRefreshStatus.BUSY,
                started_at,
                message=(
                    f"This profile is active in {label}. Close that "
                    "operation and check the profile identity again."
                ),
            )
        outcome = _refresh_with_lease(
            canonical_id,
            database,
            profiles_root,
            interrupted,
            started_at,
        )
    except Exception as exc:
        _LOGGER.warning(
            "Browser profile identity refresh failed for %s (%s).",
            canonical_id,
            type(exc).__name__,
        )
        outcome = ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.FAILED,
            started_at,
            message="The profile identity could not be checked.",
        )
    finally:
        release_connection = getattr(
            database,
            "release_current_thread_connection",
            None,
        )
        if callable(release_connection):
            try:
                released_connection = release_connection()
                if lease is not None and released_connection is False:
                    database_cleanup_failed = True
            except Exception as exc:
                if lease is not None:
                    database_cleanup_failed = True
                _LOGGER.warning(
                    "Browser profile identity database cleanup failed for %s (%s).",
                    canonical_id,
                    type(exc).__name__,
                )

    if lease is not None:
        try:
            released = lease.release()
        except Exception as exc:
            released = False
            _LOGGER.warning(
                "Browser profile identity lease cleanup failed for %s (%s).",
                canonical_id,
                type(exc).__name__,
            )
        if not released:
            database_cleanup_failed = True
    if database_cleanup_failed:
        if outcome.status in {
            IdentityRefreshStatus.UPDATED,
            IdentityRefreshStatus.UNCHANGED,
            IdentityRefreshStatus.PARTIAL,
        }:
            return ProfileIdentityRefreshOutcome(
                canonical_id,
                IdentityRefreshStatus.CLEANUP_FAILED,
                outcome.checked_at,
                changed_fields=outcome.changed_fields,
                stale_fields=outcome.stale_fields,
                message=(
                    "The profile identity was saved, but worker cleanup could not "
                    "be confirmed. Restart Automadin before using this profile."
                ),
            )
        return ProfileIdentityRefreshOutcome(
            canonical_id,
            IdentityRefreshStatus.FAILED,
            outcome.checked_at,
            message=(
                "The profile identity was not updated, and worker cleanup could "
                "not be confirmed. Restart Automadin before using this profile."
            ),
        )
    return outcome
