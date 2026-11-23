"""Versioned, checksummed Automadin workspace bundle export and inspection.

This module deliberately stops at the read-only inspection boundary.  It never
extracts a browser profile and never changes the active database or profile root.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import struct
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseCleanupError,
    BrowserProfileLeaseRegistry,
    MaintenanceProfileScope,
    ProfileUse,
    browser_profile_leases,
)
from automadin.services.browser_profile_manifest import (
    PROFILE_MANIFEST_FILENAME,
    SUPPORTED_PROFILE_ENGINES,
    BrowserProfileManifest,
    BrowserProfileManifestError,
)
from automadin.services.browser_profile_paths import (
    canonical_profile_id,
    managed_profiles_root,
    resolve_profile_storage_path,
)
from automadin.services.browser_profile_storage import (
    ProfileStorageStatus,
    inspect_profile_storage,
    profile_tree_lstat_fingerprint,
)
from automadin.services.database import CURRENT_SCHEMA_VERSION, DatabaseService
from automadin.services.database_transfer import (
    DatabaseInspection,
    DatabaseInspectionCancelledError,
    inspect_database,
)

BUNDLE_FORMAT = "automadin.portability-bundle"
BUNDLE_VERSION = 1
BUNDLE_MANIFEST_PATH = "bundle.json"
BUNDLE_DATABASE_PATH = "database/automadin.db"
PORTABILITY_WARNING = (
    "This backup may contain cookies, tokens, history, and signed-in browser state. "
    "It is checksummed but not encrypted or authenticated. Store and share it like "
    "credentials. Browser sign-in may not transfer to another Windows account or machine."
)
_CHUNK_SIZE = 1024 * 1024
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_ALLOWED_COMPRESSIONS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_CENTRAL_FILE_SIGNATURE = b"PK\x01\x02"
_EOCD_FIXED_BYTES = 22
_MAX_ZIP_COMMENT_BYTES = 65_535
_ZIP64_LOCATOR_BYTES = 20
_ZIP64_EOCD_MIN_BODY_BYTES = 44
_MAX_PROFILE_DISPLAY_NAME_CHARS = 200
_MAX_PROFILE_ENGINE_CHARS = 32
_MAX_OMISSION_REASON_CHARS = 200
_MAX_SOURCE_APP_VERSION_CHARS = 200
_MAX_TIMESTAMP_CHARS = 64


class PortabilityBundleError(RuntimeError):
    """Base class for bundle validation and export errors."""


class PortabilityBundleValidationError(PortabilityBundleError, ValueError):
    """Raised when an untrusted bundle violates the versioned contract."""


class PortabilityBundleCancelledError(PortabilityBundleError):
    """Raised after a cooperative export cancellation."""


@dataclass(frozen=True, slots=True)
class BundleLimits:
    max_archive_bytes: int = 32 * 1024**3
    max_members: int = 250_000
    max_profiles: int = 10_000
    max_file_bytes: int = 8 * 1024**3
    max_total_expanded_bytes: int = 64 * 1024**3
    max_compression_ratio: int = 1_000
    max_path_length: int = 1_024
    max_manifest_bytes: int = 4 * 1024**2
    max_central_directory_bytes: int = 64 * 1024**2

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class BundleFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BundleDatabase:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BundleProfile:
    profile_id: str
    display_name: str
    engine: str
    created_at: str
    total_size: int
    files: tuple[BundleFile, ...]


@dataclass(frozen=True, slots=True)
class OmittedBundleProfile:
    profile_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PortabilityBundleManifest:
    format: str
    bundle_version: int
    bundle_id: str
    created_at: str
    source_app_version: str
    source_schema_version: int
    database: BundleDatabase
    profiles: tuple[BundleProfile, ...]
    omitted_profiles: tuple[OmittedBundleProfile, ...]


class LocalProfileCollision(str, Enum):
    ABSENT = "absent"
    IDENTICAL = "identical"
    DIFFERENT = "different"
    ENGINE_MISMATCH = "engine_mismatch"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class ProfileCollisionInspection:
    profile_id: str
    outcome: LocalProfileCollision
    local_path: Path
    detail: str = ""


class OmittedProfileCollision(str, Enum):
    ABSENT = "absent"
    READY = "ready"
    ENGINE_MISMATCH = "engine_mismatch"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class OmittedProfileCollisionInspection:
    profile_id: str
    outcome: OmittedProfileCollision
    local_path: Path
    engine: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BundleDatabaseProfile:
    profile_id: str
    display_name: str
    engine: str
    included: bool


@dataclass(frozen=True, slots=True)
class PortabilityBundleInspection:
    path: Path
    manifest: PortabilityBundleManifest
    database: DatabaseInspection
    database_profiles: tuple[BundleDatabaseProfile, ...]
    collisions: tuple[ProfileCollisionInspection, ...]
    omitted_collisions: tuple[OmittedProfileCollisionInspection, ...]
    warning: str = PORTABILITY_WARNING


@dataclass(frozen=True, slots=True)
class PortabilityBundleExportResult:
    path: Path
    manifest: PortabilityBundleManifest
    warning: str = PORTABILITY_WARNING
    cleanup_error: str | None = None
    maintenance_cleanup_scope: MaintenanceProfileScope | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class BundleProgress:
    phase: str
    completed_bytes: int
    total_bytes: int


ProgressCallback = Callable[[BundleProgress], None]


@dataclass(frozen=True, slots=True)
class ZipCentralDirectoryPreflight:
    member_count: int
    central_directory_offset: int
    central_directory_size: int
    eocd_offset: int
    zip64: bool


def _notify(
    progress: ProgressCallback | None,
    phase: str,
    completed_bytes: int,
    total_bytes: int,
) -> None:
    if progress is not None:
        try:
            progress(BundleProgress(phase, completed_bytes, total_bytes))
        except Exception:
            # Progress is observational.  In particular, it may not turn an
            # already published backup into an apparent failed operation.
            pass


def _cancelled(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise PortabilityBundleCancelledError("Portable backup export was cancelled.")


def _iso_utc(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TIMESTAMP_CHARS:
        raise PortabilityBundleValidationError(f"{field} must be an ISO-8601 timestamp.")
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise PortabilityBundleValidationError(f"{field} is not valid ISO-8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PortabilityBundleValidationError(f"{field} must include a timezone.")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _strict_mapping(value: object, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise PortabilityBundleValidationError(f"{label} has an invalid schema.")
    return value


def _text(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
    maximum: int | None = None,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise PortabilityBundleValidationError(f"{field} must be text.")
    if maximum is not None and len(value) > maximum:
        raise PortabilityBundleValidationError(f"{field} exceeds {maximum} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PortabilityBundleValidationError(f"{field} contains control characters.")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PortabilityBundleValidationError(f"{field} must be an integer >= {minimum}.")
    return value


def _digest(value: object, field: str) -> str:
    text = _text(value, field, maximum=64)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PortabilityBundleValidationError(f"{field} must be a lowercase SHA-256.")
    return text


def _canonical_uuid(value: object, field: str) -> str:
    try:
        return canonical_profile_id(_text(value, field, maximum=36))
    except ValueError as exc:
        raise PortabilityBundleValidationError(f"{field} must be a canonical UUID.") from exc


def _safe_archive_path(value: object, *, limits: BundleLimits) -> str:
    path = _text(value, "archive member path")
    if len(path) > limits.max_path_length or "\\" in path or "\x00" in path:
        raise PortabilityBundleValidationError(f"Unsafe archive member path: {path!r}.")
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    parts = path.split("/")
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or any(not part or part in {".", ".."} for part in parts)
    ):
        raise PortabilityBundleValidationError(f"Unsafe archive member path: {path!r}.")
    for part in parts:
        if len(part) > 255 or part.endswith((" ", ".")) or ":" in part:
            raise PortabilityBundleValidationError(f"Unsafe archive member path: {path!r}.")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise PortabilityBundleValidationError(f"Unsafe archive member path: {path!r}.")
    return "/".join(parts)


def _register_archive_path(
    path: str,
    nodes: dict[str, tuple[str, bool]],
) -> None:
    """Reject Windows-ambiguous component spellings and file/dir collisions."""
    parts = path.split("/")
    for index in range(len(parts)):
        prefix = "/".join(parts[: index + 1])
        folded = prefix.casefold()
        is_file = index == len(parts) - 1
        previous = nodes.get(folded)
        if previous is not None and (previous[0] != prefix or previous[1] != is_file):
            raise PortabilityBundleValidationError(
                "Bundle contains case-equivalent or file/directory-conflicting "
                "archive path prefixes."
            )
        nodes[folded] = (prefix, is_file)


def _read_exact_at(stream: BinaryIO, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        raise PortabilityBundleValidationError("ZIP record has an invalid offset or size.")
    stream.seek(offset)
    value = stream.read(size)
    if len(value) != size:
        raise PortabilityBundleValidationError("ZIP metadata is truncated.")
    return value


def _count_central_directory_entries(
    stream: BinaryIO,
    offset: int,
    size: int,
    maximum: int,
) -> int:
    """Count central headers sequentially without retaining entry objects."""
    stream.seek(offset)
    consumed = 0
    count = 0
    while consumed < size:
        remaining = size - consumed
        if remaining < 46:
            raise PortabilityBundleValidationError(
                "ZIP central directory contains a truncated entry."
            )
        header = stream.read(46)
        if len(header) != 46 or header[:4] != _CENTRAL_FILE_SIGNATURE:
            raise PortabilityBundleValidationError(
                "ZIP central directory contains an invalid entry signature."
            )
        name_size, extra_size, comment_size = struct.unpack_from("<HHH", header, 28)
        variable_size = name_size + extra_size + comment_size
        entry_size = 46 + variable_size
        if entry_size > remaining:
            raise PortabilityBundleValidationError(
                "ZIP central directory entry exceeds its declared boundary."
            )
        if variable_size:
            stream.seek(variable_size, os.SEEK_CUR)
        consumed += entry_size
        count += 1
        if count > maximum:
            raise PortabilityBundleValidationError(
                "Bundle member count exceeds the configured limit."
            )
    return count


def preflight_zip_central_directory(
    path: str | Path,
    *,
    limits: BundleLimits = BundleLimits(),
) -> ZipCentralDirectoryPreflight:
    """Bound ZIP central-directory allocation before constructing ``ZipFile``.

    Only the bounded EOCD tail and fixed ZIP64 metadata fields are read.  No
    central-directory entry is materialized by this preflight.
    """
    archive_path = Path(path).expanduser().resolve()
    try:
        file_size = archive_path.stat().st_size
    except OSError as exc:
        raise PortabilityBundleValidationError("Bundle archive is unavailable.") from exc
    if file_size < _EOCD_FIXED_BYTES or file_size > limits.max_archive_bytes:
        raise PortabilityBundleValidationError(
            "Bundle is too small to be ZIP or exceeds the archive limit."
        )
    tail_size = min(
        file_size,
        _EOCD_FIXED_BYTES + _MAX_ZIP_COMMENT_BYTES + _ZIP64_LOCATOR_BYTES,
    )
    tail_offset = file_size - tail_size
    with archive_path.open("rb") as stream:
        tail = _read_exact_at(stream, tail_offset, tail_size)
        search_end = len(tail)
        eocd_index = -1
        while search_end:
            candidate = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
            if candidate < 0:
                break
            if candidate + _EOCD_FIXED_BYTES <= len(tail):
                comment_size = struct.unpack_from("<H", tail, candidate + 20)[0]
                absolute_end = tail_offset + candidate + _EOCD_FIXED_BYTES + comment_size
                if absolute_end == file_size:
                    eocd_index = candidate
                    break
            search_end = candidate
        if eocd_index < 0:
            raise PortabilityBundleValidationError(
                "ZIP end-of-central-directory record is missing or malformed."
            )
        eocd_offset = tail_offset + eocd_index
        (
            signature,
            disk_number,
            central_disk,
            entries_on_disk,
            total_entries,
            central_size,
            central_offset,
            _comment_size,
        ) = struct.unpack_from("<4sHHHHIIH", tail, eocd_index)
        if signature != _EOCD_SIGNATURE:
            raise PortabilityBundleValidationError("ZIP EOCD signature is invalid.")

        uses_zip64 = any(
            (
                disk_number == 0xFFFF,
                central_disk == 0xFFFF,
                entries_on_disk == 0xFFFF,
                total_entries == 0xFFFF,
                central_size == 0xFFFFFFFF,
                central_offset == 0xFFFFFFFF,
            )
        )
        boundary = eocd_offset
        if uses_zip64:
            locator_offset = eocd_offset - _ZIP64_LOCATOR_BYTES
            if locator_offset < 0:
                raise PortabilityBundleValidationError(
                    "ZIP64 locator is missing, malformed, or multi-disk."
                )
            locator = _read_exact_at(stream, locator_offset, _ZIP64_LOCATOR_BYTES)
            (
                locator_signature,
                zip64_disk,
                zip64_offset,
                total_disks,
            ) = struct.unpack("<4sIQI", locator)
            if locator_signature != _ZIP64_LOCATOR_SIGNATURE or zip64_disk != 0 or total_disks != 1:
                raise PortabilityBundleValidationError(
                    "ZIP64 locator is missing, malformed, or multi-disk."
                )
            record_header = _read_exact_at(stream, zip64_offset, 12)
            record_signature, record_body_size = struct.unpack("<4sQ", record_header)
            if (
                record_signature != _ZIP64_EOCD_SIGNATURE
                or record_body_size < _ZIP64_EOCD_MIN_BODY_BYTES
                or zip64_offset + 12 + record_body_size != locator_offset
            ):
                raise PortabilityBundleValidationError(
                    "ZIP64 end-of-central-directory record is malformed."
                )
            fixed_body = _read_exact_at(
                stream,
                zip64_offset + 12,
                _ZIP64_EOCD_MIN_BODY_BYTES,
            )
            (
                _version_made,
                _version_needed,
                actual_disk,
                actual_central_disk,
                actual_entries_on_disk,
                actual_total_entries,
                actual_central_size,
                actual_central_offset,
            ) = struct.unpack("<HHIIQQQQ", fixed_body)
            if (
                actual_disk != 0
                or actual_central_disk != 0
                or actual_entries_on_disk != actual_total_entries
            ):
                raise PortabilityBundleValidationError(
                    "Multi-disk ZIP64 archives are not supported."
                )
            standard_pairs = (
                (disk_number, 0xFFFF, actual_disk),
                (central_disk, 0xFFFF, actual_central_disk),
                (entries_on_disk, 0xFFFF, actual_entries_on_disk),
                (total_entries, 0xFFFF, actual_total_entries),
                (central_size, 0xFFFFFFFF, actual_central_size),
                (central_offset, 0xFFFFFFFF, actual_central_offset),
            )
            if any(
                standard != sentinel and standard != actual
                for standard, sentinel, actual in standard_pairs
            ):
                raise PortabilityBundleValidationError(
                    "ZIP64 metadata disagrees with its standard EOCD fields."
                )
            total_entries = actual_total_entries
            central_size = actual_central_size
            central_offset = actual_central_offset
            boundary = zip64_offset
        elif disk_number != 0 or central_disk != 0 or entries_on_disk != total_entries:
            raise PortabilityBundleValidationError("Multi-disk ZIP archives are not supported.")

        if total_entries > limits.max_members:
            raise PortabilityBundleValidationError(
                "Bundle member count exceeds the configured limit."
            )
        if central_size > limits.max_central_directory_bytes:
            raise PortabilityBundleValidationError(
                "ZIP central directory exceeds the configured byte limit."
            )
        if (
            central_offset > boundary
            or central_size > boundary - central_offset
            or (total_entries and central_size == 0)
            or (not total_entries and central_size != 0)
        ):
            raise PortabilityBundleValidationError(
                "ZIP central-directory offset, size, or entry count is inconsistent."
            )
        actual_entry_count = _count_central_directory_entries(
            stream,
            int(central_offset),
            int(central_size),
            limits.max_members,
        )
        if actual_entry_count != total_entries:
            raise PortabilityBundleValidationError(
                "ZIP central-directory entry count disagrees with EOCD metadata."
            )
        return ZipCentralDirectoryPreflight(
            member_count=actual_entry_count,
            central_directory_offset=int(central_offset),
            central_directory_size=int(central_size),
            eocd_offset=eocd_offset,
            zip64=uses_zip64,
        )


def _parse_file(value: object, limits: BundleLimits) -> BundleFile:
    data = _strict_mapping(value, frozenset({"path", "size", "sha256"}), "Bundle file")
    path = _safe_archive_path(data["path"], limits=limits)
    if "/" == path[:1]:
        raise PortabilityBundleValidationError("Bundle file path must be relative.")
    return BundleFile(
        path=path,
        size=_integer(data["size"], "Bundle file size"),
        sha256=_digest(data["sha256"], "Bundle file hash"),
    )


def _parse_manifest(payload: bytes, limits: BundleLimits) -> PortabilityBundleManifest:
    if len(payload) > limits.max_manifest_bytes:
        raise PortabilityBundleValidationError("bundle.json exceeds the manifest limit.")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PortabilityBundleValidationError(f"bundle.json repeats field {key!r}.")
            result[key] = value
        return result

    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PortabilityBundleValidationError("bundle.json is not valid UTF-8 JSON.") from exc
    data = _strict_mapping(
        decoded,
        frozenset(
            {
                "format",
                "bundle_version",
                "bundle_id",
                "created_at",
                "source_app_version",
                "source_schema_version",
                "database",
                "profiles",
                "omitted_profiles",
            }
        ),
        "Bundle manifest",
    )
    if data["format"] != BUNDLE_FORMAT or data["bundle_version"] != BUNDLE_VERSION:
        raise PortabilityBundleValidationError("Unsupported portability bundle format/version.")
    bundle_id = _canonical_uuid(data["bundle_id"], "Bundle ID")
    database_data = _strict_mapping(
        data["database"], frozenset({"path", "size", "sha256"}), "Bundle database"
    )
    database = BundleDatabase(
        path=_safe_archive_path(database_data["path"], limits=limits),
        size=_integer(database_data["size"], "Database size", minimum=1),
        sha256=_digest(database_data["sha256"], "Database hash"),
    )
    if database.path != BUNDLE_DATABASE_PATH:
        raise PortabilityBundleValidationError("Bundle database path is not canonical.")
    raw_profiles = data["profiles"]
    raw_omitted = data["omitted_profiles"]
    if not isinstance(raw_profiles, list) or not isinstance(raw_omitted, list):
        raise PortabilityBundleValidationError("Bundle profile lists must be arrays.")
    if len(raw_profiles) + len(raw_omitted) > limits.max_profiles:
        raise PortabilityBundleValidationError("Bundle exceeds the configured profile-count limit.")
    profiles: list[BundleProfile] = []
    profile_ids: set[str] = set()
    for raw_profile in raw_profiles:
        profile_data = _strict_mapping(
            raw_profile,
            frozenset(
                {"profile_id", "display_name", "engine", "created_at", "total_size", "files"}
            ),
            "Bundle profile",
        )
        profile_id = _canonical_uuid(profile_data["profile_id"], "Profile ID")
        if profile_id in profile_ids:
            raise PortabilityBundleValidationError("Bundle repeats a profile UUID.")
        profile_ids.add(profile_id)
        raw_files = profile_data["files"]
        if not isinstance(raw_files, list) or not raw_files:
            raise PortabilityBundleValidationError("Each included profile needs files.")
        files = tuple(_parse_file(item, limits) for item in raw_files)
        file_names = [item.path for item in files]
        if len(set(file_names)) != len(file_names) or len(
            {item.casefold() for item in file_names}
        ) != len(file_names):
            raise PortabilityBundleValidationError("Profile file paths are duplicated.")
        if PROFILE_MANIFEST_FILENAME not in file_names:
            raise PortabilityBundleValidationError("Profile payload omits profile.json.")
        total_size = _integer(profile_data["total_size"], "Profile total size")
        if total_size != sum(item.size for item in files):
            raise PortabilityBundleValidationError("Profile total size does not match files.")
        profiles.append(
            BundleProfile(
                profile_id=profile_id,
                display_name=_text(
                    profile_data["display_name"],
                    "Profile display name",
                    maximum=_MAX_PROFILE_DISPLAY_NAME_CHARS,
                ),
                engine=_text(
                    profile_data["engine"],
                    "Profile engine",
                    maximum=_MAX_PROFILE_ENGINE_CHARS,
                ),
                created_at=_iso_utc(profile_data["created_at"], "Profile created_at"),
                total_size=total_size,
                files=files,
            )
        )
    omitted: list[OmittedBundleProfile] = []
    for raw_item in raw_omitted:
        item = _strict_mapping(raw_item, frozenset({"profile_id", "reason"}), "Omitted profile")
        profile_id = _canonical_uuid(item["profile_id"], "Omitted profile ID")
        if profile_id in profile_ids:
            raise PortabilityBundleValidationError("A profile cannot be included and omitted.")
        profile_ids.add(profile_id)
        omitted.append(
            OmittedBundleProfile(
                profile_id=profile_id,
                reason=_text(
                    item["reason"],
                    "Omission reason",
                    maximum=_MAX_OMISSION_REASON_CHARS,
                ),
            )
        )
    return PortabilityBundleManifest(
        format=BUNDLE_FORMAT,
        bundle_version=BUNDLE_VERSION,
        bundle_id=bundle_id,
        created_at=_iso_utc(data["created_at"], "Bundle created_at"),
        source_app_version=_text(
            data["source_app_version"],
            "Source app version",
            maximum=_MAX_SOURCE_APP_VERSION_CHARS,
        ),
        source_schema_version=_integer(data["source_schema_version"], "Source schema version"),
        database=database,
        profiles=tuple(profiles),
        omitted_profiles=tuple(omitted),
    )


def _manifest_mapping(manifest: PortabilityBundleManifest) -> dict[str, object]:
    return {
        "format": manifest.format,
        "bundle_version": manifest.bundle_version,
        "bundle_id": manifest.bundle_id,
        "created_at": manifest.created_at,
        "source_app_version": manifest.source_app_version,
        "source_schema_version": manifest.source_schema_version,
        "database": {
            "path": manifest.database.path,
            "size": manifest.database.size,
            "sha256": manifest.database.sha256,
        },
        "profiles": [
            {
                "profile_id": profile.profile_id,
                "display_name": profile.display_name,
                "engine": profile.engine,
                "created_at": profile.created_at,
                "total_size": profile.total_size,
                "files": [
                    {"path": item.path, "size": item.size, "sha256": item.sha256}
                    for item in profile.files
                ],
            }
            for profile in manifest.profiles
        ],
        "omitted_profiles": [
            {"profile_id": item.profile_id, "reason": item.reason}
            for item in manifest.omitted_profiles
        ],
    }


def _lstat_signature(path: Path) -> tuple[int, int, int, int, int, int]:
    metadata = os.lstat(path)
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if stat.S_ISLNK(metadata.st_mode) or (reparse and attributes & reparse):
        raise PortabilityBundleValidationError(f"Redirected profile entry was blocked: {path}")
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise PortabilityBundleValidationError(f"Hard-linked profile entry was blocked: {path}")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _profile_files(
    root: Path,
    limits: BundleLimits,
) -> list[tuple[str, Path, tuple[int, ...]]]:
    files: list[tuple[str, Path, tuple[int, ...]]] = []

    def walk(directory: Path, relative: PurePosixPath) -> None:
        before = _lstat_signature(directory)
        if not stat.S_ISDIR(before[2]):
            raise PortabilityBundleValidationError(f"Profile entry is not a directory: {directory}")
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda item: item.name.casefold())
        seen: set[str] = set()
        for child in children:
            if child.name.casefold() in seen:
                raise PortabilityBundleValidationError(
                    f"Profile contains case-equivalent names: {directory}"
                )
            seen.add(child.name.casefold())
            relative_child = relative / child.name
            safe_name = _safe_archive_path(relative_child.as_posix(), limits=limits)
            path = Path(child.path)
            signature = _lstat_signature(path)
            if stat.S_ISDIR(signature[2]):
                walk(path, PurePosixPath(safe_name))
            elif stat.S_ISREG(signature[2]):
                files.append((safe_name, path, signature))
            else:
                raise PortabilityBundleValidationError(f"Special profile entry was blocked: {path}")
        if _lstat_signature(directory) != before:
            raise PortabilityBundleValidationError(
                f"Profile directory changed while it was inspected: {directory}"
            )

    walk(root, PurePosixPath())
    return files


def _stream_file(
    source: BinaryIO,
    destination: BinaryIO | None,
    *,
    cancel: Callable[[], bool] | None = None,
    maximum: int | None = None,
    on_chunk: Callable[[int], None] | None = None,
) -> tuple[int, str, bytes | None]:
    digest = hashlib.sha256()
    total = 0
    captured = bytearray() if destination is None else None
    while True:
        _cancelled(cancel)
        chunk = source.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if maximum is not None and total > maximum:
            raise PortabilityBundleValidationError("Archive member exceeded its size limit.")
        digest.update(chunk)
        if on_chunk is not None:
            on_chunk(len(chunk))
        if destination is not None:
            destination.write(chunk)
        else:
            assert captured is not None
            captured.extend(chunk)
    return total, digest.hexdigest(), bytes(captured) if captured is not None else None


def _write_source_member(
    archive: zipfile.ZipFile,
    archive_name: str,
    source_path: Path,
    signature: tuple[int, ...],
    cancel: Callable[[], bool] | None,
    on_chunk: Callable[[int], None] | None = None,
    maximum: int | None = None,
) -> BundleFile:
    if _lstat_signature(source_path) != signature:
        raise PortabilityBundleValidationError(f"Source changed before export: {source_path}")
    with (
        source_path.open("rb") as source,
        archive.open(archive_name, "w", force_zip64=True) as destination,
    ):
        size, sha256, _ = _stream_file(
            source,
            destination,
            cancel=cancel,
            on_chunk=on_chunk,
            maximum=maximum,
        )
    if _lstat_signature(source_path) != signature:
        raise PortabilityBundleValidationError(f"Source changed during export: {source_path}")
    return BundleFile(path="", size=size, sha256=sha256)


def _sqlite_snapshot(source_path: Path, destination: Path) -> None:
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30.0)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            target.commit()


def _publish_no_replace(temporary: Path, destination: Path) -> None:
    if os.path.lexists(destination):
        raise FileExistsError(f"Portable backup already exists: {destination}")
    if os.name == "nt":
        os.rename(temporary, destination)
        return
    os.link(temporary, destination)
    temporary.unlink()


def _sync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_portability_bundle(
    database: DatabaseService,
    destination: str | Path,
    profile_ids: list[str] | tuple[str, ...] = (),
    *,
    profiles_root: str | Path | None = None,
    lease_registry: BrowserProfileLeaseRegistry = browser_profile_leases,
    cancel: Callable[[], bool] | None = None,
    progress: ProgressCallback | None = None,
    source_app_version: str = "development",
    limits: BundleLimits = BundleLimits(),
) -> PortabilityBundleExportResult:
    """Export a WAL-consistent database and selected ready profiles."""
    final_path = Path(destination).expanduser().resolve()
    profile_boundary = managed_profiles_root(profiles_root)
    if final_path == profile_boundary or profile_boundary in final_path.parents:
        raise PortabilityBundleValidationError(
            "Portable backups cannot be written inside managed browser-profile storage."
        )
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(final_path):
        raise FileExistsError(f"Portable backup already exists: {final_path}")
    selected = sorted({canonical_profile_id(item) for item in profile_ids})
    records = {record.profile_id: record for record in database.list_profile_records()}
    if len(records) > limits.max_profiles:
        raise PortabilityBundleValidationError(
            "Portable backup exceeds the configured profile-count limit."
        )
    missing = [profile_id for profile_id in selected if profile_id not in records]
    if missing:
        raise PortabilityBundleValidationError(
            f"Selected profile is not in the database: {missing[0]}"
        )
    maintenance_scope = lease_registry.acquire_maintenance_many(
        selected,
        ProfileUse(
            "maintenance",
            "exporting a portable browser-profile backup",
            str(uuid.uuid4()),
        ),
    )
    temporary_archive = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.tmp")
    temporary_database = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.db.tmp")
    result: PortabilityBundleExportResult | None = None
    operation_error: BaseException | None = None
    try:
        profile_sources: list[
            tuple[Any, BrowserProfileManifest, Path, list[tuple[str, Path, tuple[int, ...]]]]
        ] = []
        for profile_id in selected:
            record = records[profile_id]
            inspection = inspect_profile_storage(
                profile_id,
                record.engine or None,
                legacy_storage_name=record.legacy_storage_name,
                profiles_root=profiles_root,
            )
            if inspection.status is not ProfileStorageStatus.READY or inspection.manifest is None:
                raise PortabilityBundleValidationError(
                    f"Selected profile '{record.name}' is not ready: {inspection.detail}"
                )
            profile_sources.append(
                (
                    record,
                    inspection.manifest,
                    inspection.path,
                    _profile_files(inspection.path, limits),
                )
            )
        _cancelled(cancel)
        _sqlite_snapshot(Path(database.db_path), temporary_database)
        try:
            inspect_database(temporary_database, cancel=cancel)
        except DatabaseInspectionCancelledError as exc:
            raise PortabilityBundleCancelledError("Portable backup export was cancelled.") from exc
        database_signature = _lstat_signature(temporary_database)
        total_bytes = database_signature[3] + sum(
            signature[3]
            for _record, _manifest, _path, files in profile_sources
            for _relative, _source, signature in files
        )
        member_count = 2 + sum(len(files) for *_prefix, files in profile_sources)
        if member_count > limits.max_members:
            raise PortabilityBundleValidationError(
                "Portable backup exceeds the member-count limit."
            )
        if total_bytes > limits.max_total_expanded_bytes:
            raise PortabilityBundleValidationError(
                "Portable backup exceeds the expanded-size limit."
            )
        if database_signature[3] > limits.max_file_bytes or any(
            signature[3] > limits.max_file_bytes
            for _record, _manifest, _path, files in profile_sources
            for _relative, _source, signature in files
        ):
            raise PortabilityBundleValidationError(
                "Portable backup contains a file above the size limit."
            )
        required_free = total_bytes + max(64 * 1024**2, total_bytes // 20)
        if shutil.disk_usage(final_path.parent).free < required_free:
            raise PortabilityBundleValidationError(
                "There is not enough free space to create the portable backup safely."
            )
        completed_bytes = 0
        current_phase = "exporting_database"

        def advance(byte_count: int) -> None:
            nonlocal completed_bytes
            completed_bytes += byte_count
            _notify(progress, current_phase, completed_bytes, total_bytes)

        _notify(progress, current_phase, completed_bytes, total_bytes)
        with zipfile.ZipFile(
            temporary_archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            written_database = _write_source_member(
                archive,
                BUNDLE_DATABASE_PATH,
                temporary_database,
                database_signature,
                cancel,
                advance,
                limits.max_file_bytes,
            )
            database_entry = BundleDatabase(
                BUNDLE_DATABASE_PATH,
                written_database.size,
                written_database.sha256,
            )
            bundled_profiles: list[BundleProfile] = []
            current_phase = "exporting_profiles"
            _notify(progress, current_phase, completed_bytes, total_bytes)
            for record, profile_manifest, profile_path, files in profile_sources:
                bundled_files: list[BundleFile] = []
                for relative, source_path, signature in files:
                    archive_name = f"profiles/{record.profile_id}/{relative}"
                    _safe_archive_path(archive_name, limits=limits)
                    written = _write_source_member(
                        archive,
                        archive_name,
                        source_path,
                        signature,
                        cancel,
                        advance,
                        limits.max_file_bytes,
                    )
                    bundled_files.append(BundleFile(relative, written.size, written.sha256))
                if _profile_files(profile_path, limits) != files:
                    raise PortabilityBundleValidationError(
                        f"Profile changed during export: {record.name}"
                    )
                bundled_profiles.append(
                    BundleProfile(
                        profile_id=record.profile_id,
                        display_name=record.name,
                        engine=record.engine,
                        created_at=profile_manifest.created_at,
                        total_size=sum(item.size for item in bundled_files),
                        files=tuple(bundled_files),
                    )
                )
            omitted = tuple(
                OmittedBundleProfile(profile_id, "not selected")
                for profile_id in sorted(set(records) - set(selected))
            )
            manifest = PortabilityBundleManifest(
                format=BUNDLE_FORMAT,
                bundle_version=BUNDLE_VERSION,
                bundle_id=str(uuid.uuid4()),
                created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                source_app_version=_text(
                    source_app_version,
                    "Source app version",
                    maximum=_MAX_SOURCE_APP_VERSION_CHARS,
                ),
                source_schema_version=CURRENT_SCHEMA_VERSION,
                database=database_entry,
                profiles=tuple(bundled_profiles),
                omitted_profiles=omitted,
            )
            archive.writestr(
                BUNDLE_MANIFEST_PATH,
                json.dumps(
                    _manifest_mapping(manifest),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
        with temporary_archive.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        _notify(progress, "validating_bundle", completed_bytes, total_bytes)
        inspection = inspect_portability_bundle(
            temporary_archive,
            profiles_root=profiles_root,
            limits=limits,
            cancel=cancel,
            classify_local_profiles=False,
        )
        _cancelled(cancel)
        _publish_no_replace(temporary_archive, final_path)
        _sync_parent(final_path)
        _notify(progress, "complete", total_bytes, total_bytes)
        result = PortabilityBundleExportResult(final_path, inspection.manifest)
    except BaseException as exc:
        operation_error = exc

    cleanup_errors: list[Exception] = []
    for temporary in (temporary_archive, temporary_database):
        try:
            temporary.unlink(missing_ok=True)
        except Exception as exc:
            cleanup_errors.append(exc)
    cleanup_scope: MaintenanceProfileScope | None = None
    try:
        maintenance_scope.release()
    except Exception as exc:
        cleanup_errors.append(exc)
        if isinstance(exc, BrowserProfileLeaseCleanupError):
            cleanup_scope = exc.scope

    if operation_error is not None:
        if cleanup_errors:
            cleanup_cause = next(
                (
                    error
                    for error in cleanup_errors
                    if isinstance(error, BrowserProfileLeaseCleanupError)
                ),
                cleanup_errors[-1],
            )
            for extra in cleanup_errors:
                if extra is not cleanup_cause:
                    operation_error.add_note(f"Additional cleanup failure: {extra}")
            raise operation_error.with_traceback(
                operation_error.__traceback__
            ) from cleanup_cause
        raise operation_error.with_traceback(operation_error.__traceback__)
    if result is None:
        raise RuntimeError("Portable backup export ended without an outcome.")
    if cleanup_errors:
        detail = " | ".join(str(error) for error in cleanup_errors)
        return replace(
            result,
            warning=(
                f"{result.warning}\n\nThe backup was created, but maintenance cleanup "
                f"needs attention: {detail}"
            ),
            cleanup_error=detail,
            maintenance_cleanup_scope=cleanup_scope,
        )
    return result


def _validate_zip_info(info: zipfile.ZipInfo, limits: BundleLimits) -> str:
    name = _safe_archive_path(info.filename, limits=limits)
    if info.is_dir():
        raise PortabilityBundleValidationError("Directory archive entries are not allowed.")
    if info.flag_bits & 0x1:
        raise PortabilityBundleValidationError("Encrypted archive entries are not supported.")
    if info.compress_type not in _ALLOWED_COMPRESSIONS:
        raise PortabilityBundleValidationError("Unsupported archive compression method.")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type and file_type != stat.S_IFREG:
        raise PortabilityBundleValidationError("Archive links and special files are blocked.")
    dos_attributes = info.external_attr & 0xFFFF
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse and dos_attributes & reparse:
        raise PortabilityBundleValidationError("Archive reparse entries are blocked.")
    if info.file_size > limits.max_file_bytes:
        raise PortabilityBundleValidationError("Archive member exceeds the file-size limit.")
    if info.file_size and info.compress_size == 0:
        raise PortabilityBundleValidationError("Archive member has an invalid compression ratio.")
    if info.compress_size and info.file_size / info.compress_size > limits.max_compression_ratio:
        raise PortabilityBundleValidationError(
            "Archive member exceeds the compression-ratio limit."
        )
    return name


def _local_hashes(
    profile_path: Path,
    limits: BundleLimits,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for relative, path, signature in _profile_files(profile_path, limits):
        with path.open("rb") as source:
            size, sha256, _ = _stream_file(
                source,
                _NullWriter(),
                cancel=cancel,
                maximum=limits.max_file_bytes,
            )
        if _lstat_signature(path) != signature:
            raise PortabilityBundleValidationError("Local profile changed during inspection.")
        result[relative] = (size, sha256)
    return result


def inspect_portability_bundle(
    path: str | Path,
    *,
    profiles_root: str | Path | None = None,
    limits: BundleLimits = BundleLimits(),
    progress: ProgressCallback | None = None,
    cancel: Callable[[], bool] | None = None,
    classify_local_profiles: bool = True,
) -> PortabilityBundleInspection:
    """Fully validate an untrusted bundle without changing managed state."""
    archive_path = Path(path).expanduser().resolve()
    if not archive_path.is_file() or archive_path.stat().st_size > limits.max_archive_bytes:
        raise PortabilityBundleValidationError("Bundle is missing or exceeds the archive limit.")
    database_temp: Path | None = None
    try:
        _cancelled(cancel)
        preflight_zip_central_directory(archive_path, limits=limits)
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > limits.max_members:
                raise PortabilityBundleValidationError("Bundle member count is invalid.")
            names: dict[str, zipfile.ZipInfo] = {}
            folded: set[str] = set()
            path_nodes: dict[str, tuple[str, bool]] = {}
            declared_total = 0
            compressed_total = 0
            for info in infos:
                _cancelled(cancel)
                name = _validate_zip_info(info, limits)
                _register_archive_path(name, path_nodes)
                if name in names or name.casefold() in folded:
                    raise PortabilityBundleValidationError("Bundle repeats an archive path.")
                names[name] = info
                folded.add(name.casefold())
                declared_total += info.file_size
                compressed_total += info.compress_size
                if declared_total > limits.max_total_expanded_bytes:
                    raise PortabilityBundleValidationError(
                        "Bundle exceeds the expanded-size limit."
                    )
            if (
                declared_total
                and compressed_total
                and declared_total / compressed_total > limits.max_compression_ratio
            ):
                raise PortabilityBundleValidationError(
                    "Bundle exceeds the aggregate compression-ratio limit."
                )
            manifest_info = names.get(BUNDLE_MANIFEST_PATH)
            if manifest_info is None or manifest_info.file_size > limits.max_manifest_bytes:
                raise PortabilityBundleValidationError("Bundle has no valid bundle.json.")
            with archive.open(manifest_info) as source:
                _, _, manifest_bytes = _stream_file(
                    source,
                    None,
                    cancel=cancel,
                    maximum=limits.max_manifest_bytes,
                )
            completed_progress = manifest_info.file_size
            _notify(
                progress,
                "validating_payloads",
                completed_progress,
                declared_total,
            )
            assert manifest_bytes is not None
            manifest = _parse_manifest(manifest_bytes, limits)
            declared: dict[str, BundleFile | BundleDatabase] = {
                manifest.database.path: manifest.database
            }
            for profile in manifest.profiles:
                for item in profile.files:
                    member = f"profiles/{profile.profile_id}/{item.path}"
                    if member in declared:
                        raise PortabilityBundleValidationError(
                            "Bundle declares an archive member more than once."
                        )
                    declared[member] = item
            if set(names) != set(declared) | {BUNDLE_MANIFEST_PATH}:
                raise PortabilityBundleValidationError(
                    "Bundle member set does not match bundle.json."
                )
            required_free = manifest.database.size + max(16 * 1024**2, manifest.database.size // 20)
            if shutil.disk_usage(tempfile.gettempdir()).free < required_free:
                raise PortabilityBundleValidationError(
                    "There is not enough temporary space to inspect the bundled database."
                )
            descriptor, temp_name = tempfile.mkstemp(suffix=".automadin-inspect.db")
            os.close(descriptor)
            database_temp = Path(temp_name)
            profiles_by_id = {profile.profile_id: profile for profile in manifest.profiles}
            validated_profile_manifests: set[str] = set()
            streamed_total = 0

            def advance_inspection(byte_count: int) -> None:
                nonlocal completed_progress
                completed_progress += byte_count
                _notify(
                    progress,
                    "validating_payloads",
                    completed_progress,
                    declared_total,
                )

            for member_name, expected in declared.items():
                _cancelled(cancel)
                info = names[member_name]
                profile_manifest: tuple[BundleProfile, bytes] | None = None
                if info.file_size != expected.size:
                    raise PortabilityBundleValidationError(
                        f"Archive size does not match bundle.json: {member_name}"
                    )
                with archive.open(info) as source:
                    if member_name == manifest.database.path:
                        with database_temp.open("wb") as destination:
                            size, sha256, _ = _stream_file(
                                source,
                                destination,
                                cancel=cancel,
                                maximum=limits.max_file_bytes,
                                on_chunk=advance_inspection,
                            )
                    else:
                        member_parts = member_name.split("/")
                        capture = (
                            len(member_parts) == 3
                            and member_parts[0] == "profiles"
                            and member_parts[2] == PROFILE_MANIFEST_FILENAME
                        )
                        if capture:
                            if expected.size > 64 * 1024:
                                raise PortabilityBundleValidationError(
                                    "Embedded profile.json is too large."
                                )
                            size, sha256, payload = _stream_file(
                                source,
                                None,
                                cancel=cancel,
                                maximum=64 * 1024,
                                on_chunk=advance_inspection,
                            )
                            profile_id = member_parts[1]
                            profile = profiles_by_id.get(profile_id)
                            if profile is None:
                                raise PortabilityBundleValidationError(
                                    "Canonical profile.json has no declared profile."
                                )
                            assert payload is not None
                            profile_manifest = (profile, payload)
                        else:
                            size, sha256, _ = _stream_file(
                                source,
                                _NullWriter(),
                                cancel=cancel,
                                maximum=limits.max_file_bytes,
                                on_chunk=advance_inspection,
                            )
                streamed_total += size
                if streamed_total > limits.max_total_expanded_bytes:
                    raise PortabilityBundleValidationError(
                        "Bundle exceeded the streamed expanded-size limit."
                    )
                if size != expected.size or sha256 != expected.sha256:
                    raise PortabilityBundleValidationError(
                        f"Archive content hash/size mismatch: {member_name}"
                    )
                if profile_manifest is not None:
                    profile, payload = profile_manifest
                    _validate_embedded_profile_manifest(profile, payload)
                    validated_profile_manifests.add(profile.profile_id)
            if validated_profile_manifests != set(profiles_by_id):
                raise PortabilityBundleValidationError(
                    "Every included profile must contain one canonical profile.json."
                )
            try:
                database_inspection = inspect_database(database_temp, cancel=cancel)
            except DatabaseInspectionCancelledError as exc:
                raise PortabilityBundleCancelledError(
                    "Portable backup inspection was cancelled."
                ) from exc
            if database_inspection.schema_version != manifest.source_schema_version:
                raise PortabilityBundleValidationError(
                    "Database schema does not match bundle.json."
                )
            if database_inspection.schema_version < 2:
                raise PortabilityBundleValidationError(
                    "Portability bundle databases require UUID-backed profile schema."
                )
            expected_profile_count = len(manifest.profiles) + len(manifest.omitted_profiles)
            if database_inspection.profile_count != expected_profile_count:
                raise PortabilityBundleValidationError(
                    "Bundled database profile count does not match bundle.json."
                )
            database_profiles = _database_profiles(database_temp, manifest, limits)
            database_ids = {item.profile_id for item in database_profiles}
            manifest_ids = {item.profile_id for item in manifest.profiles}
            omitted_ids = {item.profile_id for item in manifest.omitted_profiles}
            if database_ids != manifest_ids | omitted_ids:
                raise PortabilityBundleValidationError(
                    "Included/omitted profile IDs do not match the bundled database."
                )
            by_database_id = {item.profile_id: item for item in database_profiles}
            for profile in manifest.profiles:
                database_profile = by_database_id[profile.profile_id]
                if (
                    database_profile.display_name != profile.display_name
                    or database_profile.engine != profile.engine
                ):
                    raise PortabilityBundleValidationError(
                        "Profile metadata does not match the bundled database."
                    )
        collisions: tuple[ProfileCollisionInspection, ...] = ()
        omitted_collisions: tuple[OmittedProfileCollisionInspection, ...] = ()
        if classify_local_profiles:
            _notify(
                progress,
                "classifying_local_profiles",
                declared_total,
                declared_total,
            )
            collisions = tuple(
                _classify_collision(profile, profiles_root, limits, cancel)
                for profile in manifest.profiles
            )
            omitted_collisions = tuple(
                _classify_omitted_collision(profile, profiles_root, cancel)
                for profile in database_profiles
                if not profile.included
            )
        _notify(progress, "complete", declared_total, declared_total)
        return PortabilityBundleInspection(
            path=archive_path,
            manifest=manifest,
            database=database_inspection,
            database_profiles=database_profiles,
            collisions=collisions,
            omitted_collisions=omitted_collisions,
        )
    except (zipfile.BadZipFile, OSError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, PortabilityBundleError):
            raise
        raise PortabilityBundleValidationError(f"Bundle could not be inspected: {exc}") from exc
    finally:
        if database_temp is not None:
            database_temp.unlink(missing_ok=True)


class _NullWriter:
    def write(self, value: bytes) -> int:
        return len(value)


def _database_profiles(
    database_path: Path,
    manifest: PortabilityBundleManifest,
    limits: BundleLimits = BundleLimits(),
) -> tuple[BundleDatabaseProfile, ...]:
    included = {profile.profile_id for profile in manifest.profiles}
    maximum_rows = len(manifest.profiles) + len(manifest.omitted_profiles)
    if maximum_rows > limits.max_profiles:
        raise PortabilityBundleValidationError(
            "Bundled database exceeds the configured profile-count limit."
        )
    profiles: list[BundleDatabaseProfile] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024)
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(profiles)")}
            if not {"profile_id", "name", "engine"} <= columns:
                raise PortabilityBundleValidationError(
                    "Bundled database has no valid UUID profile schema."
                )
            # Never materialize an attacker-controlled SQLite TEXT value before
            # its logical length is known to fit the public profile contract.
            rows = connection.execute(
                "SELECT "
                "typeof(profile_id), length(profile_id), substr(profile_id, 1, 37), "
                "typeof(name), length(name), substr(name, 1, 201), "
                "typeof(engine), length(engine), substr(engine, 1, 33) "
                "FROM profiles"
            )
            for row in rows:
                if len(profiles) >= maximum_rows:
                    raise PortabilityBundleValidationError(
                        "Bundled database profile count exceeds bundle.json."
                    )
                if (
                    len(row) != 9
                    or row[0] != "text"
                    or type(row[1]) is not int
                    or row[1] > 36
                    or row[3] != "text"
                    or type(row[4]) is not int
                    or row[4] > 200
                    or row[6] != "text"
                    or type(row[7]) is not int
                    or row[7] > 32
                ):
                    raise PortabilityBundleValidationError(
                        "Bundled database profile text exceeds its safe length or type limit."
                    )
                profile_id = _canonical_uuid(row[2], "Database profile ID")
                display_name = _text(
                    row[5],
                    "Database profile display name",
                    maximum=_MAX_PROFILE_DISPLAY_NAME_CHARS,
                )
                engine = _text(
                    row[8],
                    "Database profile engine",
                    allow_empty=True,
                    maximum=_MAX_PROFILE_ENGINE_CHARS,
                )
                if engine and engine not in SUPPORTED_PROFILE_ENGINES:
                    raise PortabilityBundleValidationError(
                        "Bundled database contains an unsupported profile engine."
                    )
                if profile_id in seen_ids or display_name.casefold() in seen_names:
                    raise PortabilityBundleValidationError(
                        "Bundled database repeats a profile UUID or case-equivalent name."
                    )
                seen_ids.add(profile_id)
                seen_names.add(display_name.casefold())
                profiles.append(
                    BundleDatabaseProfile(
                        profile_id=profile_id,
                        display_name=display_name,
                        engine=engine,
                        included=profile_id in included,
                    )
                )
    except sqlite3.Error as exc:
        raise PortabilityBundleValidationError(
            "Bundled database profile data exceeds safe SQLite limits."
        ) from exc
    if len(profiles) != maximum_rows:
        raise PortabilityBundleValidationError(
            "Bundled database profile count does not match bundle.json."
        )
    profiles.sort(key=lambda profile: profile.profile_id)
    return tuple(profiles)


def _validate_embedded_profile_manifest(
    profile: BundleProfile,
    payload: bytes,
) -> None:
    if len(payload) > 64 * 1024:
        raise PortabilityBundleValidationError("Embedded profile.json is too large.")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PortabilityBundleValidationError(
                    f"Embedded profile.json repeats field {key!r}."
                )
            result[key] = value
        return result

    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
        if not isinstance(decoded, dict):
            raise ValueError
        embedded = BrowserProfileManifest.from_mapping(decoded)
    except (
        BrowserProfileManifestError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise PortabilityBundleValidationError("Embedded profile.json is invalid.") from exc
    if embedded.profile_id != profile.profile_id or embedded.engine != profile.engine:
        raise PortabilityBundleValidationError(
            "Embedded profile.json identity/engine does not match bundle.json."
        )
    if embedded.created_at != profile.created_at:
        raise PortabilityBundleValidationError(
            "Embedded profile.json creation time does not match bundle.json."
        )


def _classify_collision(
    profile: BundleProfile,
    profiles_root: str | Path | None,
    limits: BundleLimits,
    cancel: Callable[[], bool] | None,
) -> ProfileCollisionInspection:
    _cancelled(cancel)
    root = managed_profiles_root(profiles_root)
    lexical_path = root / profile.profile_id
    try:
        local_path = resolve_profile_storage_path(profile.profile_id, root)
    except Exception as exc:
        return ProfileCollisionInspection(
            profile.profile_id, LocalProfileCollision.UNSAFE, lexical_path, str(exc)
        )
    if not os.path.lexists(local_path):
        return ProfileCollisionInspection(
            profile.profile_id, LocalProfileCollision.ABSENT, local_path
        )
    inspection = inspect_profile_storage(
        profile.profile_id,
        profiles_root=root,
    )
    if inspection.status is not ProfileStorageStatus.READY or inspection.manifest is None:
        return ProfileCollisionInspection(
            profile.profile_id,
            LocalProfileCollision.UNSAFE,
            local_path,
            inspection.detail,
        )
    if inspection.manifest.engine != profile.engine:
        return ProfileCollisionInspection(
            profile.profile_id,
            LocalProfileCollision.ENGINE_MISMATCH,
            local_path,
            f"Local engine is {inspection.manifest.engine}; bundle engine is {profile.engine}.",
        )
    try:
        local = _local_hashes(local_path, limits, cancel)
    except PortabilityBundleCancelledError:
        raise
    except Exception as exc:
        return ProfileCollisionInspection(
            profile.profile_id, LocalProfileCollision.UNSAFE, local_path, str(exc)
        )
    bundled = {item.path: (item.size, item.sha256) for item in profile.files}
    outcome = (
        LocalProfileCollision.IDENTICAL if local == bundled else LocalProfileCollision.DIFFERENT
    )
    return ProfileCollisionInspection(profile.profile_id, outcome, local_path)


def _classify_omitted_collision(
    profile: BundleDatabaseProfile,
    profiles_root: str | Path | None,
    cancel: Callable[[], bool] | None,
) -> OmittedProfileCollisionInspection:
    _cancelled(cancel)
    root = managed_profiles_root(profiles_root)
    lexical_path = root / profile.profile_id
    try:
        local_path = resolve_profile_storage_path(profile.profile_id, root)
    except Exception as exc:
        return OmittedProfileCollisionInspection(
            profile.profile_id,
            OmittedProfileCollision.UNSAFE,
            lexical_path,
            profile.engine,
            str(exc),
        )
    if not os.path.lexists(local_path):
        return OmittedProfileCollisionInspection(
            profile.profile_id,
            OmittedProfileCollision.ABSENT,
            local_path,
            profile.engine,
        )
    inspection = inspect_profile_storage(profile.profile_id, profiles_root=root)
    if inspection.status is not ProfileStorageStatus.READY or inspection.manifest is None:
        return OmittedProfileCollisionInspection(
            profile.profile_id,
            OmittedProfileCollision.UNSAFE,
            local_path,
            profile.engine,
            inspection.detail,
        )
    try:
        profile_tree_lstat_fingerprint(inspection.path)
    except Exception as exc:
        return OmittedProfileCollisionInspection(
            profile.profile_id,
            OmittedProfileCollision.UNSAFE,
            local_path,
            profile.engine,
            str(exc),
        )
    local_engine = inspection.manifest.engine
    if (
        not profile.engine
        or profile.engine not in SUPPORTED_PROFILE_ENGINES
        or local_engine != profile.engine
    ):
        return OmittedProfileCollisionInspection(
            profile.profile_id,
            OmittedProfileCollision.ENGINE_MISMATCH,
            local_path,
            profile.engine,
            f"Local engine is {local_engine}; database engine is {profile.engine or 'unset'}.",
        )
    return OmittedProfileCollisionInspection(
        profile.profile_id,
        OmittedProfileCollision.READY,
        local_path,
        profile.engine,
    )
