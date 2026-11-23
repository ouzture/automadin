"""Safe filesystem boundaries for Automadin-managed browser profiles.

New profiles use a canonical UUID as their storage directory.  Display names
are deliberately absent from that mapping so a database import cannot bind an
identity to an unrelated same-name directory.  The legacy-name resolver remains
available for an explicit adoption workflow and for compatibility while callers
are migrated; it never participates in UUID resolution.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath


class BrowserProfilePathError(ValueError):
    """Base class for invalid or unsafe managed-profile paths."""


class UnsafeBrowserProfileNameError(BrowserProfilePathError):
    """Raised when a legacy profile name could address outside managed storage."""


class InvalidBrowserProfileIdError(BrowserProfilePathError):
    """Raised when a profile ID is not a canonical UUID."""


class UnsafeBrowserProfilePathError(BrowserProfilePathError):
    """Raised when a managed profile path is redirected or is not a directory."""


def path_has_redirect(path: str | Path) -> bool:
    """Return whether any existing path component is a filesystem redirect.

    ``Path.resolve() != lexical_path`` is not a valid redirect test on Windows:
    resolving an ordinary 8.3 short-name path expands it to its long spelling.
    Inspecting each component with ``lstat`` distinguishes that harmless alias
    from POSIX symlinks and Windows reparse points without following them.
    """
    lexical = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            continue
        try:
            metadata = os.lstat(current)
        except OSError:
            return True
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISLNK(metadata.st_mode) or (
            reparse_flag and attributes & reparse_flag
        ):
            return True
    return False


def managed_profiles_root(profiles_root: str | Path | None = None) -> Path:
    """Return the trusted root used for Automadin-owned profiles.

    A deliberately configured directory link is canonicalized because the root
    defines the trusted boundary. Otherwise the caller's spelling is retained
    so valid Windows short-name aliases do not leak into public paths as a
    different long-name spelling.
    """
    if profiles_root is None:
        data_dir = os.environ.get("AUTOMADIN_DATA_DIR", ".")
        profiles_root = os.environ.get(
            "AUTOMADIN_PROFILES_DIR",
            os.path.join(data_dir, "profiles"),
        )
    root = Path(os.path.abspath(Path(profiles_root).expanduser()))
    if path_has_redirect(root):
        try:
            root = root.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise UnsafeBrowserProfilePathError(
                "Automadin's managed browser-profile directory cannot be resolved."
            ) from exc
    if root.exists() and not root.is_dir():
        raise UnsafeBrowserProfilePathError(
            "Automadin's managed browser-profile path is not a directory."
        )
    return root


def canonical_profile_id(profile_id: str | uuid.UUID) -> str:
    """Return *profile_id* in canonical UUID text form or reject it.

    String inputs must already be the lowercase, hyphenated canonical form.
    Rejecting alternative spellings avoids multiple paths representing one
    identity.
    """
    if isinstance(profile_id, uuid.UUID):
        return str(profile_id)
    if not isinstance(profile_id, str) or not profile_id:
        raise InvalidBrowserProfileIdError(
            "Browser profile ID must be a canonical UUID."
        )
    try:
        parsed = uuid.UUID(profile_id)
    except (ValueError, AttributeError) as exc:
        raise InvalidBrowserProfileIdError(
            "Browser profile ID must be a canonical UUID."
        ) from exc
    canonical = str(parsed)
    if profile_id != canonical:
        raise InvalidBrowserProfileIdError(
            "Browser profile ID must use lowercase, hyphenated canonical UUID text."
        )
    return canonical


def new_profile_id() -> str:
    """Create a canonical random identity for a new managed browser profile."""
    return str(uuid.uuid4())


def _safe_direct_child(
    root: Path,
    leaf_name: str,
    *,
    error_type: type[BrowserProfilePathError],
    description: str,
) -> Path:
    """Return a direct child while rejecting symlinks and reparse points."""
    lexical = root / leaf_name
    if lexical.parent != root or path_has_redirect(lexical):
        raise error_type(
            f"{description} is redirected or resolves outside Automadin's managed "
            "profiles directory and was blocked."
        )
    if lexical.exists() and not lexical.is_dir():
        raise error_type(f"{description} is not a directory.")
    return lexical


def resolve_profile_storage_path(
    profile_id: str | uuid.UUID,
    profiles_root: str | Path | None = None,
) -> Path:
    """Resolve the UUID storage directory for a canonical profile identity.

    This function intentionally does not inspect display-name or legacy
    directories.  Adopting legacy data must be an explicit higher-level action.
    """
    canonical_id = canonical_profile_id(profile_id)
    root = managed_profiles_root(profiles_root)
    return _safe_direct_child(
        root,
        canonical_id,
        error_type=UnsafeBrowserProfilePathError,
        description=f"Browser profile '{canonical_id}'",
    )


def ensure_profile_storage_directory(
    profile_id: str | uuid.UUID,
    profiles_root: str | Path | None = None,
    *,
    lease_registry: object,
    maintenance_lease: object,
) -> Path:
    """Create (or validate) one UUID-backed managed profile directory."""
    from automadin.services.browser_profile_leases import BrowserProfileLeaseRegistry

    canonical_id = canonical_profile_id(profile_id)
    if not isinstance(lease_registry, BrowserProfileLeaseRegistry):
        raise TypeError("A browser profile lease registry is required.")
    lease_registry.validate_maintenance(maintenance_lease, canonical_id)
    root = managed_profiles_root(profiles_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UnsafeBrowserProfilePathError(
            "Automadin's managed browser-profile directory could not be created."
        ) from exc

    candidate = resolve_profile_storage_path(canonical_id, root)
    try:
        candidate.mkdir(exist_ok=True)
    except OSError as exc:
        raise UnsafeBrowserProfilePathError(
            f"Browser profile directory '{candidate.name}' could not be created."
        ) from exc

    # Re-resolve after creation to close the common symlink/junction race and
    # reject a file occupying the expected directory.
    candidate = resolve_profile_storage_path(profile_id, root)
    if not candidate.is_dir():
        raise UnsafeBrowserProfilePathError(
            f"Browser profile path '{candidate.name}' is not a directory."
        )
    return candidate


def _validate_legacy_profile_name(profile_name: str) -> None:
    if not isinstance(profile_name, str) or not profile_name:
        raise UnsafeBrowserProfileNameError(
            "The browser profile has no valid internal name. Recreate or re-import it."
        )

    posix_name = PurePosixPath(profile_name)
    windows_name = PureWindowsPath(profile_name)
    has_separator = "/" in profile_name or "\\" in profile_name
    has_drive_or_root = bool(windows_name.drive or windows_name.root)
    if (
        profile_name in {".", ".."}
        or has_separator
        or posix_name.is_absolute()
        or windows_name.is_absolute()
        or has_drive_or_root
        or "\x00" in profile_name
    ):
        raise UnsafeBrowserProfileNameError(
            f"Browser profile '{profile_name}' has an unsafe internal name. "
            "Profile names must be one folder name inside Automadin's managed "
            "profiles directory; absolute paths, '..', and nested paths are not allowed. "
            "Recreate or re-import the profile, then select it again."
        )


def resolve_legacy_profile_path(
    profile_name: str,
    profiles_root: str | Path | None = None,
) -> Path:
    """Resolve a legacy display-name directory for explicit adoption only.

    Resolving a candidate neither creates a directory nor associates it with a
    canonical profile ID.
    """
    _validate_legacy_profile_name(profile_name)
    root = managed_profiles_root(profiles_root)
    return _safe_direct_child(
        root,
        profile_name,
        error_type=UnsafeBrowserProfileNameError,
        description=f"Browser profile '{profile_name}'",
    )


def resolve_managed_profile_path(
    profile_name: str,
    profiles_root: str | Path | None = None,
) -> Path:
    """Compatibility alias for the explicit legacy-name resolver.

    Existing callers still store profile display names as identity.  New code
    must use :func:`resolve_profile_storage_path` instead.
    """
    return resolve_legacy_profile_path(profile_name, profiles_root)
