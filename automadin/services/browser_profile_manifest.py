"""Versioned, non-secret identity manifests for managed browser profiles."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from automadin.services.browser_profile_paths import (
    canonical_profile_id,
    ensure_profile_storage_directory,
    path_has_redirect,
    resolve_profile_storage_path,
)

PROFILE_MANIFEST_FILENAME = "profile.json"
PROFILE_MANIFEST_FORMAT = "automadin.browser-profile"
PROFILE_MANIFEST_VERSION = 1
SUPPORTED_PROFILE_ENGINES = frozenset(
    {"chromium", "chrome", "msedge", "firefox", "webkit"}
)
_MANIFEST_KEYS = frozenset(
    {"format", "manifest_version", "profile_id", "engine", "created_at"}
)
_MAX_MANIFEST_BYTES = 64 * 1024


class BrowserProfileManifestError(ValueError):
    """Base class for profile manifest read and validation failures."""


class InvalidBrowserProfileManifestError(BrowserProfileManifestError):
    """Raised when profile.json is malformed or has unsafe values."""


class UnsupportedBrowserProfileManifestVersionError(BrowserProfileManifestError):
    """Raised when profile.json uses a schema version this build cannot read."""


def _canonical_engine(engine: object) -> str:
    if not isinstance(engine, str) or engine not in SUPPORTED_PROFILE_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_PROFILE_ENGINES))
        raise InvalidBrowserProfileManifestError(
            f"Browser profile engine must be one of: {supported}."
        )
    return engine


def _canonical_created_at(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidBrowserProfileManifestError(
            "Browser profile created_at must be an ISO-8601 timestamp with a timezone."
        )
    parse_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise InvalidBrowserProfileManifestError(
            "Browser profile created_at is not a valid ISO-8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidBrowserProfileManifestError(
            "Browser profile created_at must include a timezone."
        )
    parsed = parsed.astimezone(UTC)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _created_at_text(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidBrowserProfileManifestError(
                "Browser profile created_at must include a timezone."
            )
        timespec = "microseconds" if value.microsecond else "seconds"
        value = value.astimezone(UTC).isoformat(timespec=timespec).replace(
            "+00:00", "Z"
        )
    return _canonical_created_at(value)


@dataclass(frozen=True, slots=True)
class BrowserProfileManifest:
    """Stable identity stored beside browser data.

    The schema intentionally contains no account name, email, token, cookie,
    source path, or other user data.  Human-readable metadata belongs in the
    database and can be refreshed independently.
    """

    profile_id: str
    engine: str
    created_at: str
    format: str = PROFILE_MANIFEST_FORMAT
    manifest_version: int = PROFILE_MANIFEST_VERSION

    def __post_init__(self) -> None:
        if self.format != PROFILE_MANIFEST_FORMAT:
            raise InvalidBrowserProfileManifestError(
                "File is not an Automadin browser-profile manifest."
            )
        if type(self.manifest_version) is not int:
            raise InvalidBrowserProfileManifestError(
                "Browser profile manifest version must be an integer."
            )
        if self.manifest_version != PROFILE_MANIFEST_VERSION:
            raise UnsupportedBrowserProfileManifestVersionError(
                "Browser profile manifest version "
                f"{self.manifest_version} is not supported."
            )
        try:
            canonical_id = canonical_profile_id(self.profile_id)
        except ValueError as exc:
            raise InvalidBrowserProfileManifestError(str(exc)) from exc
        object.__setattr__(self, "profile_id", canonical_id)
        object.__setattr__(self, "engine", _canonical_engine(self.engine))
        object.__setattr__(self, "created_at", _canonical_created_at(self.created_at))

    @classmethod
    def create(
        cls,
        engine: str,
        *,
        profile_id: str | uuid.UUID | None = None,
        created_at: datetime | str | None = None,
    ) -> BrowserProfileManifest:
        """Create a v1 manifest for a new or explicitly adopted profile."""
        identity = str(uuid.uuid4()) if profile_id is None else canonical_profile_id(
            profile_id
        )
        return cls(
            profile_id=identity,
            engine=engine,
            created_at=_created_at_text(created_at),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BrowserProfileManifest:
        """Validate an untrusted decoded profile.json object."""
        keys = frozenset(value.keys())
        if keys != _MANIFEST_KEYS:
            missing = sorted(_MANIFEST_KEYS - keys)
            unexpected = sorted(keys - _MANIFEST_KEYS)
            details: list[str] = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected fields: {', '.join(unexpected)}")
            raise InvalidBrowserProfileManifestError(
                "Browser profile manifest has an invalid schema"
                + (f" ({'; '.join(details)})" if details else "")
                + "."
            )
        return cls(
            format=value["format"],
            manifest_version=value["manifest_version"],
            profile_id=value["profile_id"],
            engine=value["engine"],
            created_at=value["created_at"],
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the complete non-secret JSON representation."""
        return {
            "format": self.format,
            "manifest_version": self.manifest_version,
            "profile_id": self.profile_id,
            "engine": self.engine,
            "created_at": self.created_at,
        }


def _validated_profile_directory(profile_directory: str | Path) -> Path:
    """Return an existing real directory, rejecting symlink/junction redirects."""
    raw = Path(profile_directory).expanduser()
    lexical = Path(os.path.abspath(raw))
    if path_has_redirect(lexical):
        raise InvalidBrowserProfileManifestError(
            "Browser profile directory may not be a symlink or junction."
        )
    try:
        metadata = os.lstat(lexical)
    except OSError as exc:
        raise InvalidBrowserProfileManifestError(
            "Browser profile directory does not exist or cannot be resolved."
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise InvalidBrowserProfileManifestError(
            "Browser profile manifest parent is not a directory."
        )
    return lexical


def profile_manifest_path(profile_directory: str | Path) -> Path:
    """Return profile.json below a validated real profile directory."""
    path = _validated_profile_directory(profile_directory) / PROFILE_MANIFEST_FILENAME
    if path_has_redirect(path):
        raise InvalidBrowserProfileManifestError(
            "Browser profile manifest path may not be a symlink."
        )
    return path


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidBrowserProfileManifestError(
                f"Browser profile manifest repeats field '{key}'."
            )
        result[key] = value
    return result


def read_profile_manifest(
    profile_directory: str | Path,
) -> BrowserProfileManifest:
    """Read and validate profile.json from an existing profile directory."""
    path = profile_manifest_path(profile_directory)
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise InvalidBrowserProfileManifestError(
                "Browser profile manifest is unexpectedly large."
            )
        text = path.read_text(encoding="utf-8")
        decoded = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except BrowserProfileManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidBrowserProfileManifestError(
            "Browser profile manifest could not be read as valid UTF-8 JSON."
        ) from exc
    if not isinstance(decoded, dict):
        raise InvalidBrowserProfileManifestError(
            "Browser profile manifest root must be a JSON object."
        )
    return BrowserProfileManifest.from_mapping(decoded)


def _write_profile_manifest(
    profile_directory: str | Path,
    manifest: BrowserProfileManifest,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write profile.json in an existing real profile directory."""
    if not isinstance(manifest, BrowserProfileManifest):
        raise TypeError("manifest must be a BrowserProfileManifest")

    directory = _validated_profile_directory(profile_directory)
    destination = directory / PROFILE_MANIFEST_FILENAME
    if destination.is_symlink():
        raise InvalidBrowserProfileManifestError(
            "Browser profile manifest path may not be a symlink."
        )
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Browser profile manifest already exists: {destination}")

    payload = (
        json.dumps(
            manifest.to_mapping(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".profile.",
            suffix=".tmp",
            dir=directory,
        )
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp_path, destination)
        elif sys.platform == "win32":
            # Windows rename is atomic within one volume and refuses to replace
            # an existing destination.  Unlike a hard link, it also works on
            # FAT/exFAT drives commonly used for portable installations.
            os.rename(temp_path, destination)
        else:
            # Linking a fully flushed temporary file gives the destination
            # atomic visibility while retaining create-if-absent semantics.
            # Both paths are in the same directory/filesystem.
            os.link(temp_path, destination)
            temp_path.unlink()
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return destination


def write_managed_profile_manifest(
    manifest: BrowserProfileManifest,
    profiles_root: str | Path | None = None,
    *,
    lease_registry: object,
    maintenance_lease: object,
    overwrite: bool = False,
) -> Path:
    """Create/validate the UUID directory and atomically persist its manifest."""
    if not isinstance(manifest, BrowserProfileManifest):
        raise TypeError("manifest must be a BrowserProfileManifest")
    directory = ensure_profile_storage_directory(
        manifest.profile_id,
        profiles_root,
        lease_registry=lease_registry,
        maintenance_lease=maintenance_lease,
    )
    return _write_profile_manifest(directory, manifest, overwrite=overwrite)


def read_managed_profile_manifest(
    profile_id: str | uuid.UUID,
    profiles_root: str | Path | None = None,
) -> BrowserProfileManifest:
    """Read a managed manifest and verify directory-to-manifest identity."""
    canonical_id = canonical_profile_id(profile_id)
    directory = resolve_profile_storage_path(canonical_id, profiles_root)
    manifest = read_profile_manifest(directory)
    if manifest.profile_id != canonical_id:
        raise InvalidBrowserProfileManifestError(
            "Browser profile manifest identity does not match its storage directory."
        )
    return manifest
