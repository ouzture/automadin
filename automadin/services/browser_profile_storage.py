"""Transactional filesystem ownership for managed browser profiles.

Database rows own the human-readable profile name.  This module owns only the
filesystem side of a profile: an immutable UUID directory and its matching
manifest.  The split is deliberate.  A same-name folder can be offered as a
legacy candidate, but is never selected as storage without an explicit adopt
operation.

Filesystem and SQLite transactions cannot be made atomic together.  Mutating
operations therefore return :class:`ProfileStorageChange`.  Callers update the
database and then ``commit()`` the change, or ``rollback()`` it when the database
operation fails::

    lease = lease_registry.acquire_maintenance(profile_id, operation)
    change = create_clean_profile(
        profile_id,
        "chromium",
        maintenance_lease=lease,
    )
    try:
        database.mark_profile_storage_resolved(
            profile_id,
            engine=change.engine,
        )
    except Exception:
        change.rollback()
        raise
    else:
        change.commit()
    finally:
        lease.release()

The receipt also works as a context manager and rolls back unless explicitly
committed.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from automadin.services.browser_profile_manifest import (
    PROFILE_MANIFEST_FILENAME,
    SUPPORTED_PROFILE_ENGINES,
    BrowserProfileManifest,
    BrowserProfileManifestError,
    _write_profile_manifest,
    read_profile_manifest,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    MaintenanceProfileLease,
)
from automadin.services.browser_profile_metadata import profile_layout_ready
from automadin.services.browser_profile_paths import (
    BrowserProfilePathError,
    canonical_profile_id,
    managed_profiles_root,
    path_has_redirect,
    resolve_legacy_profile_path,
    resolve_profile_storage_path,
)

_STORAGE_ENGINES = frozenset({"chromium", "chrome", "msedge", "firefox"})
_CHROMIUM_ENGINES = frozenset({"chromium", "chrome", "msedge"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_SKIPPED_BROWSER_ENTRIES = frozenset(
    {
        "BrowserMetrics",
        "Cache",
        "Code Cache",
        "Crashpad",
        "DawnCache",
        "GPUCache",
        "GrShaderCache",
        "GraphiteDawnCache",
        "Safe Browsing",
        "ShaderCache",
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
        "lockfile",
    }
)
_PENDING_DELETE_NAME = re.compile(
    r"^\.deleting-"
    r"(?P<profile_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<nonce>[0-9a-f]{32})$"
)
_STAGING_REMNANT_NAME = re.compile(
    r"^\.(?P<operation>creating|importing)-"
    r"(?P<profile_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<nonce>[0-9a-f]{32})$"
)
_MANAGED_PROFILE_NAME = re.compile(
    r"^(?P<profile_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})$"
)


class BrowserProfileStorageError(RuntimeError):
    """Base class for managed profile storage failures."""


class ProfileStorageConflictError(BrowserProfileStorageError):
    """Raised when an operation would reuse or overwrite existing storage."""


class ProfileStorageValidationError(BrowserProfileStorageError):
    """Raised when profile data cannot safely be treated as Automadin-owned."""


class ProfileStorageRecoveryError(BrowserProfileStorageError):
    """Raised when compensating a partially completed operation fails."""


class ProfileStorageInventoryError(BrowserProfileStorageError):
    """Raised when read-only storage inventory cannot be completed safely."""

    def __init__(
        self,
        message: str,
        *,
        blocked: tuple[BlockedPendingProfileDelete, ...] = (),
    ) -> None:
        super().__init__(message)
        self.blocked = blocked


class ProfileStorageStatus(str, Enum):
    """User-facing readiness states for one stored profile identity."""

    READY = "ready"
    NEEDS_SETUP = "needs_setup"
    MISSING = "missing"
    CORRUPT = "corrupt"
    MISMATCH = "mismatch"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class ProfileStorageInspection:
    """Read-only result from :func:`inspect_profile_storage`."""

    status: ProfileStorageStatus
    profile_id: str
    path: Path
    engine: str = ""
    legacy_candidate: Path | None = None
    recovery_path: Path | None = None
    manifest: BrowserProfileManifest | None = None
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.status is ProfileStorageStatus.READY


@dataclass(frozen=True, slots=True)
class PendingProfileDelete:
    """One manifest-verified interrupted profile deletion.

    Discovery is deliberately read-only.  The caller must still decide whether
    the database transaction completed, then use
    :func:`recover_pending_profile_delete` to either finish or restore it.
    """

    profile_id: str
    engine: str
    tombstone_path: Path
    intended_path: Path


class PendingProfileDeleteBlockReason(str, Enum):
    """Why an exact interrupted-delete name cannot be recovered automatically."""

    MULTIPLE_CANDIDATES = "multiple_candidates"
    INVALID_PATH = "invalid_path"
    INVALID_MANIFEST = "invalid_manifest"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_ENGINE = "unsupported_engine"
    INCOMPATIBLE_LAYOUT = "incompatible_layout"


@dataclass(frozen=True, slots=True)
class BlockedPendingProfileDelete:
    """One UUID whose exact delete tombstone(s) require manual diagnosis."""

    profile_id: str
    candidate_paths: tuple[Path, ...]
    reason: PendingProfileDeleteBlockReason
    detail: str


@dataclass(frozen=True, slots=True)
class PendingProfileDeleteInventory:
    """Complete read-only delete-tombstone inventory."""

    pending: tuple[PendingProfileDelete, ...]
    blocked: tuple[BlockedPendingProfileDelete, ...]


@dataclass(frozen=True, slots=True)
class ManagedProfileStorage:
    """One strict manifest-owned UUID directory, independent of database rows."""

    profile_id: str
    engine: str
    path: Path


class ProfileStagingOperation(str, Enum):
    """Filesystem operation that created a crash-remnant staging directory."""

    CREATING = "creating"
    IMPORTING = "importing"


@dataclass(frozen=True, slots=True)
class ProfileStagingRemnant:
    """One exact, real staging directory left by an interrupted operation."""

    operation: ProfileStagingOperation
    profile_id: str
    path: Path
    has_matching_manifest: bool
    manifest_engine: str = ""
    layout_ready: bool = False
    _snapshot: str = field(default="", repr=False, compare=False)


class _ChangeState(str, Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


def _no_op() -> None:
    return None


def _require_maintenance_lease(
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: object,
    profile_id: str,
) -> MaintenanceProfileLease:
    if not isinstance(lease_registry, BrowserProfileLeaseRegistry):
        raise TypeError("A browser profile lease registry is required.")
    return lease_registry.validate_maintenance(maintenance_lease, profile_id)


@dataclass(slots=True)
class ProfileStorageChange:
    """Compensating receipt for a filesystem mutation.

    ``path`` is always the UUID storage path visible to the rest of Automadin.
    For a pending delete, ``working_path`` is the private tombstone holding the
    data until ``commit()`` permanently removes it.
    """

    operation: str
    profile_id: str
    engine: str
    path: Path
    working_path: Path
    _lease_registry: BrowserProfileLeaseRegistry = field(repr=False)
    _maintenance_lease: MaintenanceProfileLease = field(repr=False)
    _commit_action: Callable[[], None] = field(default=_no_op, repr=False)
    _rollback_action: Callable[[], None] = field(default=_no_op, repr=False)
    _state: _ChangeState = field(default=_ChangeState.ACTIVE, init=False, repr=False)

    @property
    def active(self) -> bool:
        return self._state is _ChangeState.ACTIVE

    @property
    def committed(self) -> bool:
        return self._state is _ChangeState.COMMITTED

    @property
    def rolled_back(self) -> bool:
        return self._state is _ChangeState.ROLLED_BACK

    def commit(self) -> None:
        """Finalize this change.  A failed action remains retryable."""
        if self._state is _ChangeState.COMMITTED:
            return
        if self._state is _ChangeState.ROLLED_BACK:
            raise ProfileStorageRecoveryError(
                f"Cannot commit rolled-back profile {self.operation}."
            )
        _require_maintenance_lease(
            self._lease_registry, self._maintenance_lease, self.profile_id
        )
        self._commit_action()
        self._state = _ChangeState.COMMITTED

    def rollback(self) -> None:
        """Compensate this change.  A failed action remains retryable."""
        if self._state is _ChangeState.ROLLED_BACK:
            return
        if self._state is _ChangeState.COMMITTED:
            raise ProfileStorageRecoveryError(
                f"Cannot roll back committed profile {self.operation}."
            )
        _require_maintenance_lease(
            self._lease_registry, self._maintenance_lease, self.profile_id
        )
        self._rollback_action()
        self._state = _ChangeState.ROLLED_BACK

    def __enter__(self) -> ProfileStorageChange:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self.active:
            self.rollback()


def _canonical_storage_engine(engine: str) -> str:
    if not isinstance(engine, str) or engine not in _STORAGE_ENGINES:
        supported = ", ".join(sorted(_STORAGE_ENGINES))
        raise ProfileStorageValidationError(
            f"Persistent browser profile engine must be one of: {supported}."
        )
    return engine


def _canonical_expected_engine(engine: str) -> str:
    if not isinstance(engine, str) or engine not in SUPPORTED_PROFILE_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_PROFILE_ENGINES))
        raise ProfileStorageValidationError(
            f"Browser profile engine must be one of: {supported}."
        )
    return engine


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _validated_existing_directory(path: Path, description: str) -> Path:
    """Require a real directory, not a symlink or junction/reparse redirect."""
    lexical = Path(os.path.abspath(path.expanduser()))
    if path_has_redirect(lexical):
        raise ProfileStorageValidationError(
            f"{description} may not be a symlink, junction, or redirected path."
        )
    try:
        metadata = os.lstat(lexical)
    except OSError as exc:
        raise ProfileStorageValidationError(
            f"{description} does not exist or cannot be resolved safely."
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProfileStorageValidationError(f"{description} is not a directory.")
    return lexical


def _inventory_direct_children(root: Path) -> list[Path]:
    """Enumerate a managed root without converting access failures to empty."""
    try:
        with os.scandir(root) as entries:
            return [Path(entry.path) for entry in entries]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ProfileStorageInventoryError(
            f"Managed browser-profile storage could not be enumerated: {root}"
        ) from exc


def _compatible_real_layout(directory: Path, engine: str) -> bool:
    """Require the engine's launch layout without accepting nested redirects."""
    if engine not in _STORAGE_ENGINES or not profile_layout_ready(directory, engine):
        return False
    if engine not in _CHROMIUM_ENGINES:
        return engine == "firefox"
    try:
        default_directory = _validated_existing_directory(
            directory / "Default",
            "Browser profile Default directory",
        )
    except ProfileStorageValidationError:
        return False
    return default_directory.parent == directory


def _staging_remnant_snapshot(directory: Path) -> str:
    """Fingerprint ownership-relevant metadata without reading profile data."""
    parts: list[tuple[object, ...]] = []
    for label, path in (
        ("root", directory),
        ("manifest", directory / PROFILE_MANIFEST_FILENAME),
        ("default", directory / "Default"),
    ):
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            parts.append((label, "missing"))
            continue
        parts.append(
            (
                label,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        )
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def profile_tree_lstat_fingerprint(directory: str | Path) -> str:
    """Fingerprint every real entry's lstat metadata without following redirects."""
    root = _validated_existing_directory(Path(directory), "Browser profile tree")
    parts: list[tuple[object, ...]] = []

    def visit(current: Path, relative: str) -> None:
        metadata = os.lstat(current)
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if stat.S_ISLNK(metadata.st_mode) or (reparse_flag and attributes & reparse_flag):
            raise ProfileStorageValidationError(
                f"Browser profile tree contains a redirected entry: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode):
            raise ProfileStorageValidationError(
                f"Browser profile tree contains an unsupported entry: {current}"
            )
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise ProfileStorageValidationError(
                f"Browser profile tree contains a hard-linked file: {current}"
            )
        parts.append(
            (
                relative,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                # Root ctime may change on an otherwise safe atomic rename.
                None if not relative else metadata.st_ctime_ns,
            )
        )
        if stat.S_ISDIR(metadata.st_mode):
            try:
                with os.scandir(current) as entries:
                    children = sorted(list(entries), key=lambda item: item.name)
            except OSError as exc:
                raise ProfileStorageValidationError(
                    f"Browser profile tree could not be enumerated: {current}"
                ) from exc
            folded: set[str] = set()
            for child in children:
                name = child.name
                folded_name = name.casefold()
                stem = name.split(".", 1)[0].upper()
                if (
                    folded_name in folded
                    or name.endswith((" ", "."))
                    or ":" in name
                    or stem in _WINDOWS_RESERVED_NAMES
                ):
                    raise ProfileStorageValidationError(
                        f"Browser profile tree contains an unsafe or ambiguous name: {child.path}"
                    )
                folded.add(folded_name)
                child_relative = f"{relative}/{child.name}" if relative else child.name
                visit(Path(child.path), child_relative)

    visit(root, "")
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def _ensure_root(profiles_root: str | Path | None) -> Path:
    root = managed_profiles_root(profiles_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrowserProfileStorageError(
            "Automadin's browser-profile directory could not be created."
        ) from exc
    return _validated_existing_directory(root, "Managed browser-profile directory")


def _generated_sibling(root: Path, prefix: str) -> Path:
    candidate = root / f"{prefix}{uuid.uuid4().hex}"
    lexical = Path(os.path.abspath(candidate))
    if lexical.parent != root or path_has_redirect(lexical):
        raise ProfileStorageValidationError(
            "A temporary browser-profile path could not be created safely."
        )
    return lexical


def _raise_rename_error(exc: OSError, destination: Path) -> None:
    if exc.errno in {errno.EEXIST, errno.ENOTEMPTY} or _lexists(destination):
        raise ProfileStorageConflictError(
            f"Browser profile storage already exists: {destination}"
        ) from exc
    raise BrowserProfileStorageError(
        f"Browser profile storage could not be moved into place: {destination}"
    ) from exc


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename a directory while refusing an existing destination.

    Windows ``rename`` is no-replace.  Linux's ``renameat2(RENAME_NOREPLACE)``
    closes the check/rename race that ordinary POSIX ``rename`` would leave.
    On platforms without a no-replace primitive we fail closed rather than risk
    replacing an empty destination directory.
    """
    if _lexists(destination):
        raise ProfileStorageConflictError(
            f"Browser profile storage already exists: {destination}"
        )

    if sys.platform == "win32":
        try:
            os.rename(source, destination)
        except OSError as exc:
            _raise_rename_error(exc, destination)
        return

    if sys.platform.startswith("linux"):
        renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if renameat2 is None:
            raise BrowserProfileStorageError(
                "This system does not provide atomic no-replace profile moves."
            )
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,  # AT_FDCWD
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,  # RENAME_NOREPLACE
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise BrowserProfileStorageError(
                "This filesystem does not provide atomic no-replace profile moves."
            )
        _raise_rename_error(OSError(error_number, os.strerror(error_number)), destination)
        return

    raise BrowserProfileStorageError(
        "This platform does not provide an approved atomic no-replace profile move."
    )


def _safe_remove_transient(path: Path, root: Path) -> None:
    """Remove one generated direct child without ever following redirection."""
    if not _lexists(path):
        return
    lexical = Path(os.path.abspath(path))
    try:
        verified = _validated_existing_directory(
            lexical,
            "Temporary profile data",
        )
    except ProfileStorageValidationError as exc:
        raise ProfileStorageRecoveryError(
            f"Temporary profile data could not be resolved for cleanup: {path}"
        ) from exc
    if lexical.parent != root or verified != lexical:
        raise ProfileStorageRecoveryError(
            f"Refusing to clean redirected temporary profile data: {path}"
        )
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise ProfileStorageRecoveryError(
            f"Temporary profile data could not be cleaned up: {path}"
        ) from exc


def _manifest_matches(
    directory: Path,
    profile_id: str,
    engine: str | None = None,
) -> BrowserProfileManifest:
    try:
        manifest = read_profile_manifest(directory)
    except BrowserProfileManifestError as exc:
        raise ProfileStorageValidationError(
            f"Browser profile manifest is missing, malformed, or unsupported: {directory}"
        ) from exc
    if manifest.profile_id != profile_id:
        raise ProfileStorageValidationError(
            "Browser profile manifest identity does not match the requested profile."
        )
    if engine is not None and manifest.engine != engine:
        raise ProfileStorageValidationError(
            "Browser profile manifest engine does not match the requested profile."
        )
    return manifest


@dataclass(slots=True)
class StagedProfilePublication:
    """Idempotently move one verified importing directory to/from its UUID path."""

    profile_id: str
    engine: str
    staging_path: Path
    final_path: Path
    fingerprint: str
    profiles_root: Path
    _lease_registry: BrowserProfileLeaseRegistry = field(repr=False)
    _maintenance_lease: MaintenanceProfileLease = field(repr=False)

    def _validate_location(self, path: Path, *, staging: bool) -> None:
        directory = _validated_existing_directory(path, "Staged browser profile")
        if directory.parent != self.profiles_root:
            raise ProfileStorageValidationError(
                "Browser profile publication path left managed storage."
            )
        if staging:
            match = _STAGING_REMNANT_NAME.fullmatch(directory.name)
            if (
                match is None
                or match.group("operation") != ProfileStagingOperation.IMPORTING.value
                or match.group("profile_id") != self.profile_id
            ):
                raise ProfileStorageValidationError(
                    "Browser profile staging name does not match its import plan."
                )
        elif directory != self.final_path:
            raise ProfileStorageValidationError(
                "Browser profile destination does not match its UUID."
            )
        _manifest_matches(directory, self.profile_id, self.engine)
        if not _compatible_real_layout(directory, self.engine):
            raise ProfileStorageValidationError(
                "Browser profile staging data has an incompatible layout."
            )
        if profile_tree_lstat_fingerprint(directory) != self.fingerprint:
            raise ProfileStorageRecoveryError(
                "Browser profile staging data changed after the activation plan was created."
            )

    def location(self) -> str:
        staging_exists = _lexists(self.staging_path)
        final_exists = _lexists(self.final_path)
        if staging_exists == final_exists:
            raise ProfileStorageRecoveryError(
                "Browser profile activation is ambiguous because both or neither "
                "of its staging and final paths exist."
            )
        if staging_exists:
            self._validate_location(self.staging_path, staging=True)
            return "staging"
        self._validate_location(self.final_path, staging=False)
        return "final"

    def publish(self) -> bool:
        """Publish once; return whether this call performed the atomic move."""
        _require_maintenance_lease(
            self._lease_registry, self._maintenance_lease, self.profile_id
        )
        if self.location() == "final":
            return False
        _rename_directory_no_replace(self.staging_path, self.final_path)
        self._validate_location(self.final_path, staging=False)
        return True

    def rollback(self) -> bool:
        """Return published data to its exact staging path without deleting it."""
        _require_maintenance_lease(
            self._lease_registry, self._maintenance_lease, self.profile_id
        )
        if self.location() == "staging":
            return False
        _rename_directory_no_replace(self.final_path, self.staging_path)
        self._validate_location(self.staging_path, staging=True)
        return True


def prepare_staged_profile_publication(
    staging_path: str | Path,
    profile_id: str,
    engine: str,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    profiles_root: str | Path | None = None,
    expected_fingerprint: str | None = None,
) -> StagedProfilePublication:
    """Validate one exact `.importing-<UUID>-<nonce>` directory for activation."""
    profile_id = canonical_profile_id(profile_id)
    _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
    engine = _canonical_storage_engine(engine)
    root = _ensure_root(profiles_root)
    staging = Path(os.path.abspath(Path(staging_path).expanduser()))
    final = resolve_profile_storage_path(profile_id, root)
    publication = StagedProfilePublication(
        profile_id=profile_id,
        engine=engine,
        staging_path=staging,
        final_path=final,
        fingerprint=expected_fingerprint or profile_tree_lstat_fingerprint(staging),
        profiles_root=root,
        _lease_registry=lease_registry,
        _maintenance_lease=maintenance_lease,
    )
    if publication.location() != "staging":
        raise ProfileStorageConflictError(
            f"Browser profile storage already exists: {final}"
        )
    return publication


def _remove_verified_profile(
    directory: Path,
    root: Path,
    profile_id: str,
    engine: str,
) -> None:
    """Delete only an unchanged, manifest-owned direct child."""
    if not _lexists(directory):
        return
    verified = _validated_existing_directory(directory, "Browser profile storage")
    if verified.parent != root:
        raise ProfileStorageRecoveryError(
            "Refusing to delete browser profile data outside managed storage."
        )
    _manifest_matches(verified, profile_id, engine)
    try:
        shutil.rmtree(verified)
    except OSError as exc:
        raise ProfileStorageRecoveryError(
            f"Browser profile storage could not be removed: {verified}"
        ) from exc


def _browser_copy_ignore(current_directory: str, names: list[str]) -> set[str]:
    """Skip volatile files and filesystem redirects while copying browser data."""
    ignored: set[str] = set()
    current = Path(current_directory)
    for name in names:
        child = current / name
        if name in _SKIPPED_BROWSER_ENTRIES or name.endswith((".tmp", ".log", ".lock")):
            ignored.add(name)
            continue
        if path_has_redirect(child):
            ignored.add(name)
    return ignored


def _stage_browser_profile(source: str | Path, staging: str | Path, engine: str) -> None:
    """Copy a Chrome, Edge, Chromium, or Firefox profile into launch layout.

    ``source`` is never moved or modified.  ``staging`` must not exist and is
    created by this function.  Redirected source roots and nested redirects are
    rejected or skipped so managed storage cannot become an alias to unrelated
    data.
    """
    engine = _canonical_storage_engine(engine)
    source_path = _validated_existing_directory(Path(source), "Source browser profile")
    staging_path = Path(os.path.abspath(Path(staging).expanduser()))
    try:
        resolved_staging = staging_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProfileStorageValidationError(
            "Temporary profile storage cannot be resolved safely."
        ) from exc
    if source_path == resolved_staging or source_path in resolved_staging.parents:
        raise ProfileStorageValidationError(
            "Source browser profile may not contain the temporary import destination."
        )
    if _lexists(staging_path):
        raise ProfileStorageConflictError(
            f"Temporary profile storage already exists: {staging_path}"
        )

    if engine in _CHROMIUM_ENGINES:
        staging_path.mkdir(parents=False)
        shutil.copytree(
            source_path,
            staging_path / "Default",
            ignore=_browser_copy_ignore,
        )
        local_state = source_path.parent / "Local State"
        if local_state.is_file() and not path_has_redirect(local_state):
            shutil.copy2(local_state, staging_path / "Local State")
        return

    source_manifest = source_path / PROFILE_MANIFEST_FILENAME
    if _lexists(source_manifest):
        raise ProfileStorageValidationError(
            "Firefox source contains profile.json; refusing to replace or reinterpret it."
        )
    shutil.copytree(source_path, staging_path, ignore=_browser_copy_ignore)


def _create_clean_layout(staging: Path, engine: str) -> None:
    staging.mkdir()
    if engine in _CHROMIUM_ENGINES:
        (staging / "Default").mkdir()


def _new_manifest(
    profile_id: str,
    engine: str,
    created_at: datetime | str | None,
) -> BrowserProfileManifest:
    try:
        return BrowserProfileManifest.create(
            engine,
            profile_id=profile_id,
            created_at=created_at,
        )
    except BrowserProfileManifestError as exc:
        raise ProfileStorageValidationError(str(exc)) from exc


def _created_change(
    operation: str,
    profile_id: str,
    engine: str,
    destination: Path,
    root: Path,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
) -> ProfileStorageChange:
    return ProfileStorageChange(
        operation=operation,
        profile_id=profile_id,
        engine=engine,
        path=destination,
        working_path=destination,
        _lease_registry=lease_registry,
        _maintenance_lease=maintenance_lease,
        _rollback_action=lambda: _remove_verified_profile(
            destination,
            root,
            profile_id,
            engine,
        ),
    )


def create_clean_profile(
    profile_id: str,
    engine: str,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    profiles_root: str | Path | None = None,
    created_at: datetime | str | None = None,
) -> ProfileStorageChange:
    """Create a new empty UUID-backed persistent browser profile.

    The complete layout and manifest are built in a sibling staging directory,
    then published using an atomic no-replace rename.  Existing destinations are
    always conflicts, even when their manifest happens to match.
    """
    profile_id = canonical_profile_id(profile_id)
    _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
    engine = _canonical_storage_engine(engine)
    root = _ensure_root(profiles_root)
    destination = resolve_profile_storage_path(profile_id, root)
    if _lexists(destination):
        raise ProfileStorageConflictError(
            f"Browser profile storage already exists: {destination}"
        )
    staging = _generated_sibling(root, f".creating-{profile_id}-")
    manifest = _new_manifest(profile_id, engine, created_at)
    try:
        _create_clean_layout(staging, engine)
        _write_profile_manifest(staging, manifest)
        _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
        _rename_directory_no_replace(staging, destination)
    except Exception as exc:
        try:
            _safe_remove_transient(staging, root)
        except ProfileStorageRecoveryError as cleanup_exc:
            raise cleanup_exc from exc
        raise
    return _created_change(
        "create",
        profile_id,
        engine,
        destination,
        root,
        lease_registry,
        maintenance_lease,
    )


def import_browser_profile(
    source: str | Path,
    profile_id: str,
    engine: str,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    profiles_root: str | Path | None = None,
    created_at: datetime | str | None = None,
) -> ProfileStorageChange:
    """Copy a browser profile through staging into new UUID storage.

    The selected identity manifest is written before the staging directory is
    atomically renamed.  The source folder is preserved and an existing UUID
    destination is never reused or replaced.
    """
    profile_id = canonical_profile_id(profile_id)
    _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
    engine = _canonical_storage_engine(engine)
    source_path = _validated_existing_directory(Path(source), "Source browser profile")
    proposed_root = managed_profiles_root(profiles_root)
    if source_path == proposed_root or source_path in proposed_root.parents:
        raise ProfileStorageValidationError(
            "Source browser profile may not contain the managed profiles directory."
        )
    root = _ensure_root(profiles_root)
    destination = resolve_profile_storage_path(profile_id, root)
    if _lexists(destination):
        raise ProfileStorageConflictError(
            f"Browser profile storage already exists: {destination}"
        )
    staging = _generated_sibling(root, f".importing-{profile_id}-")
    manifest = _new_manifest(profile_id, engine, created_at)
    try:
        _stage_browser_profile(source_path, staging, engine)
        _write_profile_manifest(staging, manifest)
        _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
        _rename_directory_no_replace(staging, destination)
    except Exception as exc:
        try:
            _safe_remove_transient(staging, root)
        except ProfileStorageRecoveryError as cleanup_exc:
            raise cleanup_exc from exc
        raise
    return _created_change(
        "import",
        profile_id,
        engine,
        destination,
        root,
        lease_registry,
        maintenance_lease,
    )


def adopt_legacy_profile(
    legacy_name: str,
    profile_id: str,
    engine: str,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    profiles_root: str | Path | None = None,
    created_at: datetime | str | None = None,
) -> ProfileStorageChange:
    """Explicitly move one validated legacy folder to its UUID destination.

    A missing root ``profile.json`` is expected for legacy data.  If one exists,
    it must already be a valid Automadin manifest for exactly this identity and
    engine; malformed, foreign, and mismatched files are blocked.
    """
    profile_id = canonical_profile_id(profile_id)
    _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
    engine = _canonical_storage_engine(engine)
    root = _ensure_root(profiles_root)
    destination = resolve_profile_storage_path(profile_id, root)
    legacy = resolve_legacy_profile_path(legacy_name, root)
    if destination == legacy:
        raise ProfileStorageValidationError(
            "UUID storage cannot be adopted as a legacy display-name folder."
        )
    if _lexists(destination):
        raise ProfileStorageConflictError(
            f"Browser profile storage already exists: {destination}"
        )
    legacy = _validated_existing_directory(legacy, "Legacy browser profile")
    if legacy.parent != root:
        raise ProfileStorageValidationError(
            "Legacy browser profile must be one direct child of managed storage."
        )

    manifest_path = legacy / PROFILE_MANIFEST_FILENAME
    created_manifest = not _lexists(manifest_path)
    if created_manifest:
        manifest = _new_manifest(profile_id, engine, created_at)
    else:
        manifest = _manifest_matches(legacy, profile_id, engine)
    if not profile_layout_ready(legacy, engine):
        raise ProfileStorageValidationError(
            f"Legacy browser profile does not contain the required {engine} layout."
        )

    def remove_created_manifest(directory: Path) -> None:
        manifest_file = directory / PROFILE_MANIFEST_FILENAME
        if not _lexists(manifest_file):
            return
        restored_manifest = _manifest_matches(directory, profile_id, engine)
        if restored_manifest != manifest:
            raise ProfileStorageRecoveryError(
                "Adopted profile manifest changed; it was preserved for recovery."
            )
        try:
            manifest_file.unlink()
        except OSError as exc:
            raise ProfileStorageRecoveryError(
                "Legacy profile was restored, but its new identity manifest remains."
            ) from exc

    moved = False
    try:
        _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
        _rename_directory_no_replace(legacy, destination)
        moved = True
        if created_manifest:
            _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
            _write_profile_manifest(destination, manifest)
    except Exception:
        if moved and _lexists(destination):
            try:
                _rename_directory_no_replace(destination, legacy)
            except Exception as rollback_exc:
                raise ProfileStorageRecoveryError(
                    "Legacy profile adoption failed and its folder could not be restored."
                ) from rollback_exc
            if created_manifest:
                remove_created_manifest(legacy)
        raise

    def rollback_adoption() -> None:
        _manifest_matches(destination, profile_id, engine)
        _rename_directory_no_replace(destination, legacy)
        if created_manifest:
            remove_created_manifest(legacy)

    return ProfileStorageChange(
        operation="adopt",
        profile_id=profile_id,
        engine=engine,
        path=destination,
        working_path=destination,
        _lease_registry=lease_registry,
        _maintenance_lease=maintenance_lease,
        _rollback_action=rollback_adoption,
    )


def begin_profile_delete(
    profile_id: str,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    engine: str | None = None,
    profiles_root: str | Path | None = None,
) -> ProfileStorageChange:
    """Move verified UUID storage to a private tombstone pending DB deletion."""
    profile_id = canonical_profile_id(profile_id)
    _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
    expected_engine = _canonical_expected_engine(engine) if engine is not None else None
    root = _ensure_root(profiles_root)
    destination = resolve_profile_storage_path(profile_id, root)
    directory = _validated_existing_directory(destination, "Browser profile storage")
    if directory.parent != root:
        raise ProfileStorageValidationError(
            "Browser profile storage is outside the managed directory."
        )
    manifest = _manifest_matches(directory, profile_id, expected_engine)
    tombstone = _generated_sibling(root, f".deleting-{profile_id}-")
    _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
    _rename_directory_no_replace(destination, tombstone)

    return _deletion_change(
        profile_id,
        manifest,
        destination,
        tombstone,
        root,
        lease_registry,
        maintenance_lease,
    )


def _deletion_change(
    profile_id: str,
    manifest: BrowserProfileManifest,
    destination: Path,
    tombstone: Path,
    root: Path,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    *,
    operation: str = "delete",
) -> ProfileStorageChange:
    def commit_delete() -> None:
        _remove_verified_profile(
            tombstone,
            root,
            profile_id,
            manifest.engine,
        )

    def rollback_delete() -> None:
        _manifest_matches(tombstone, profile_id, manifest.engine)
        _rename_directory_no_replace(tombstone, destination)

    return ProfileStorageChange(
        operation=operation,
        profile_id=profile_id,
        engine=manifest.engine,
        path=destination,
        working_path=tombstone,
        _lease_registry=lease_registry,
        _maintenance_lease=maintenance_lease,
        _commit_action=commit_delete,
        _rollback_action=rollback_delete,
    )


def list_managed_profile_storage(
    profiles_root: str | Path | None = None,
) -> list[ManagedProfileStorage]:
    """Return strict UUID-owned directories without consulting the database.

    Invalid names, redirected paths, malformed or mismatched manifests,
    unsupported engines, and incompatible layouts are left untouched and
    omitted.  Root enumeration failures are raised because an incomplete scan
    must not be mistaken for an empty managed-profile directory.
    """
    root = managed_profiles_root(profiles_root)
    discovered: list[ManagedProfileStorage] = []
    for candidate in _inventory_direct_children(root):
        match = _MANAGED_PROFILE_NAME.fullmatch(candidate.name)
        if match is None:
            continue
        profile_id = match.group("profile_id")
        try:
            profile_id = canonical_profile_id(profile_id)
            directory = _validated_existing_directory(
                candidate,
                "Managed browser profile",
            )
            if directory.parent != root:
                continue
            manifest = _manifest_matches(directory, profile_id)
            if not _compatible_real_layout(directory, manifest.engine):
                continue
            intended_path = resolve_profile_storage_path(profile_id, root)
            if directory != intended_path:
                continue
        except (
            BrowserProfilePathError,
            BrowserProfileStorageError,
            OSError,
            ValueError,
        ):
            continue
        discovered.append(
            ManagedProfileStorage(
                profile_id=profile_id,
                engine=manifest.engine,
                path=directory,
            )
        )
    return sorted(discovered, key=lambda item: item.profile_id)


def list_profile_staging_remnants(
    profiles_root: str | Path | None = None,
) -> list[ProfileStagingRemnant]:
    """Return exact real create/import remnants without changing them.

    A matching manifest is strict schema-valid, identifies the UUID encoded in
    the directory name, and declares an engine supported by managed storage.
    ``layout_ready`` is reported separately because a crash can leave otherwise
    identifiable data incomplete.
    """
    root = managed_profiles_root(profiles_root)
    discovered: list[ProfileStagingRemnant] = []
    for candidate in _inventory_direct_children(root):
        match = _STAGING_REMNANT_NAME.fullmatch(candidate.name)
        if match is None:
            continue
        profile_id = match.group("profile_id")
        try:
            profile_id = canonical_profile_id(profile_id)
            directory = _validated_existing_directory(
                candidate,
                "Browser profile staging remnant",
            )
        except (ProfileStorageValidationError, OSError, ValueError):
            continue
        if directory.parent != root:
            continue

        manifest_engine = ""
        has_matching_manifest = False
        layout_ready = False
        try:
            manifest = read_profile_manifest(directory)
        except BrowserProfileManifestError:
            pass
        else:
            manifest_engine = manifest.engine
            has_matching_manifest = (
                manifest.profile_id == profile_id
                and manifest.engine in _STORAGE_ENGINES
            )
            layout_ready = has_matching_manifest and _compatible_real_layout(
                directory,
                manifest.engine,
            )
        discovered.append(
            ProfileStagingRemnant(
                operation=ProfileStagingOperation(match.group("operation")),
                profile_id=profile_id,
                path=directory,
                has_matching_manifest=has_matching_manifest,
                manifest_engine=manifest_engine,
                layout_ready=layout_ready,
                _snapshot=_staging_remnant_snapshot(directory),
            )
        )
    return sorted(
        discovered,
        key=lambda item: (item.profile_id, item.operation.value, item.path.name),
    )


def discard_profile_staging_remnant(
    remnant: ProfileStagingRemnant,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    profiles_root: str | Path | None = None,
) -> None:
    """Remove one freshly revalidated generated staging remnant.

    The caller must pass an unchanged value returned by
    :func:`list_profile_staging_remnants`.  A stale, forged, redirected,
    renamed, or ownership-relevant mutated entry is rejected.  UUID storage
    paths never match the generated staging-name contract.
    """
    if not isinstance(remnant, ProfileStagingRemnant):
        raise ProfileStorageValidationError(
            "A listed browser-profile staging remnant is required."
        )
    _require_maintenance_lease(
        lease_registry,
        maintenance_lease,
        remnant.profile_id,
    )
    root = managed_profiles_root(profiles_root)
    current = next(
        (
            item
            for item in list_profile_staging_remnants(root)
            if item.path == remnant.path
        ),
        None,
    )
    if current is None:
        raise ProfileStorageValidationError(
            "The staging remnant is stale or is no longer safely listed."
        )
    if (
        current.operation is not remnant.operation
        or current.profile_id != remnant.profile_id
        or current.has_matching_manifest != remnant.has_matching_manifest
        or current.manifest_engine != remnant.manifest_engine
        or current.layout_ready != remnant.layout_ready
        or not remnant._snapshot
        or current._snapshot != remnant._snapshot
    ):
        raise ProfileStorageValidationError(
            "The staging remnant changed after it was inspected; nothing was deleted."
        )

    match = _STAGING_REMNANT_NAME.fullmatch(current.path.name)
    if (
        match is None
        or match.group("profile_id") != current.profile_id
        or match.group("operation") != current.operation.value
        or current.path.parent != root
    ):
        raise ProfileStorageValidationError(
            "The staging remnant no longer has a valid generated path."
        )
    directory = _validated_existing_directory(
        current.path,
        "Browser profile staging remnant",
    )
    if _staging_remnant_snapshot(directory) != current._snapshot:
        raise ProfileStorageValidationError(
            "The staging remnant changed during validation; nothing was deleted."
        )
    _require_maintenance_lease(
        lease_registry,
        maintenance_lease,
        current.profile_id,
    )
    _safe_remove_transient(directory, root)


def _pending_delete_candidates(root: Path) -> dict[str, list[Path]]:
    """Group exact generated tombstone names without inspecting their targets."""
    candidates: dict[str, list[Path]] = {}
    for child in _inventory_direct_children(root):
        match = _PENDING_DELETE_NAME.fullmatch(child.name)
        if match is None:
            continue
        profile_id = match.group("profile_id")
        try:
            profile_id = canonical_profile_id(profile_id)
        except ValueError:
            continue
        candidates.setdefault(profile_id, []).append(child)
    return candidates


def _inspect_pending_delete_candidate(
    root: Path,
    profile_id: str,
    candidate: Path,
) -> tuple[PendingProfileDelete | None, BlockedPendingProfileDelete | None]:
    lexical = Path(os.path.abspath(candidate))

    def blocked(
        reason: PendingProfileDeleteBlockReason,
        detail: str,
    ) -> tuple[None, BlockedPendingProfileDelete]:
        return None, BlockedPendingProfileDelete(
            profile_id=profile_id,
            candidate_paths=(lexical,),
            reason=reason,
            detail=detail,
        )

    try:
        tombstone = _validated_existing_directory(
            candidate,
            "Interrupted browser profile deletion",
        )
    except ProfileStorageValidationError as exc:
        return blocked(PendingProfileDeleteBlockReason.INVALID_PATH, str(exc))
    if tombstone.parent != root:
        return blocked(
            PendingProfileDeleteBlockReason.INVALID_PATH,
            "Interrupted delete is not a real direct child of managed storage.",
        )

    try:
        manifest = read_profile_manifest(tombstone)
    except BrowserProfileManifestError:
        return blocked(
            PendingProfileDeleteBlockReason.INVALID_MANIFEST,
            "Interrupted delete has no strict readable profile manifest.",
        )
    if manifest.profile_id != profile_id:
        return blocked(
            PendingProfileDeleteBlockReason.IDENTITY_MISMATCH,
            "Interrupted-delete manifest identity does not match its directory name.",
        )
    if manifest.engine not in _STORAGE_ENGINES:
        return blocked(
            PendingProfileDeleteBlockReason.UNSUPPORTED_ENGINE,
            "Interrupted-delete manifest engine is not supported by managed storage.",
        )
    if not _compatible_real_layout(tombstone, manifest.engine):
        return blocked(
            PendingProfileDeleteBlockReason.INCOMPATIBLE_LAYOUT,
            "Interrupted delete does not contain a compatible real browser layout.",
        )
    try:
        intended_path = resolve_profile_storage_path(profile_id, root)
    except BrowserProfilePathError as exc:
        return blocked(PendingProfileDeleteBlockReason.INVALID_PATH, str(exc))
    return (
        PendingProfileDelete(
            profile_id=profile_id,
            engine=manifest.engine,
            tombstone_path=tombstone,
            intended_path=intended_path,
        ),
        None,
    )


def inspect_pending_profile_deletes(
    profiles_root: str | Path | None = None,
) -> PendingProfileDeleteInventory:
    """Return verified and blocked exact tombstones without changing them."""
    root = managed_profiles_root(profiles_root)
    pending: list[PendingProfileDelete] = []
    blocked: list[BlockedPendingProfileDelete] = []
    for profile_id, candidates in _pending_delete_candidates(root).items():
        if len(candidates) != 1:
            blocked.append(
                BlockedPendingProfileDelete(
                    profile_id=profile_id,
                    candidate_paths=tuple(
                        sorted(
                            (Path(os.path.abspath(path)) for path in candidates),
                            key=lambda path: path.name,
                        )
                    ),
                    reason=PendingProfileDeleteBlockReason.MULTIPLE_CANDIDATES,
                    detail=(
                        "Multiple exact interrupted deletes exist for this UUID; "
                        "no candidate was selected."
                    ),
                )
            )
            continue
        item, diagnostic = _inspect_pending_delete_candidate(
            root,
            profile_id,
            candidates[0],
        )
        if item is not None:
            pending.append(item)
        if diagnostic is not None:
            blocked.append(diagnostic)
    return PendingProfileDeleteInventory(
        pending=tuple(
            sorted(
                pending,
                key=lambda item: (item.profile_id, item.tombstone_path.name),
            )
        ),
        blocked=tuple(
            sorted(
                blocked,
                key=lambda item: (item.profile_id, item.reason.value),
            )
        ),
    )


def list_pending_profile_deletes(
    profiles_root: str | Path | None = None,
) -> list[PendingProfileDelete]:
    """Return verified tombstones, failing closed on exact blocked entries.

    Only exact names generated by :func:`begin_profile_delete` are considered.
    Use :func:`inspect_pending_profile_deletes` to render its typed diagnostics.
    This compatibility list raises when any exact candidate is blocked so a
    manager cannot misreport hidden recovery data as missing.
    """
    inventory = inspect_pending_profile_deletes(profiles_root)
    if inventory.blocked:
        profile_ids = ", ".join(item.profile_id for item in inventory.blocked)
        raise ProfileStorageInventoryError(
            "Interrupted browser-profile deletion data requires manual review "
            f"for UUID(s): {profile_ids}.",
            blocked=inventory.blocked,
        )
    return list(inventory.pending)


def recover_pending_profile_delete(
    profile_id: str,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    engine: str | None = None,
    profiles_root: str | Path | None = None,
) -> ProfileStorageChange:
    """Reopen an interrupted tombstone so the caller can finish or restore it.

    If the corresponding database row still exists, call ``rollback()``.  If
    the database deletion was committed before the process stopped, call
    ``commit()``.  No choice is made implicitly because only the database owner
    can determine which side of that boundary completed.
    """
    profile_id = canonical_profile_id(profile_id)
    _require_maintenance_lease(lease_registry, maintenance_lease, profile_id)
    expected_engine = _canonical_expected_engine(engine) if engine is not None else None
    root = _ensure_root(profiles_root)
    destination = resolve_profile_storage_path(profile_id, root)
    if _lexists(destination):
        raise ProfileStorageConflictError(
            "Cannot recover an interrupted delete while UUID storage exists."
        )
    inventory = inspect_pending_profile_deletes(root)
    blocked = [
        item for item in inventory.blocked if item.profile_id == profile_id
    ]
    if blocked:
        diagnostic = blocked[0]
        error_message = (
            f"{diagnostic.detail} Candidates: "
            + ", ".join(str(path) for path in diagnostic.candidate_paths)
        )
        if (
            diagnostic.reason
            is PendingProfileDeleteBlockReason.MULTIPLE_CANDIDATES
        ):
            raise ProfileStorageRecoveryError(error_message)
        raise ProfileStorageValidationError(error_message)
    tombstones = [
        item for item in inventory.pending if item.profile_id == profile_id
    ]
    if not tombstones:
        raise ProfileStorageValidationError(
            "No interrupted delete was found for this browser profile."
        )
    if len(tombstones) != 1:
        raise ProfileStorageRecoveryError(
            "Multiple interrupted deletes were found; automatic recovery was blocked."
        )
    tombstone = tombstones[0].tombstone_path
    manifest = _manifest_matches(tombstone, profile_id, expected_engine)
    return _deletion_change(
        profile_id,
        manifest,
        destination,
        tombstone,
        root,
        lease_registry,
        maintenance_lease,
        operation="recover-delete",
    )


def delete_verified_profile(
    profile_id: str,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_lease: MaintenanceProfileLease,
    engine: str | None = None,
    profiles_root: str | Path | None = None,
) -> ProfileStorageChange:
    """Immediately tombstone and remove verified UUID-owned storage."""
    change = begin_profile_delete(
        profile_id,
        lease_registry=lease_registry,
        maintenance_lease=maintenance_lease,
        engine=engine,
        profiles_root=profiles_root,
    )
    change.commit()
    return change


def _inspection(
    status: ProfileStorageStatus,
    profile_id: str,
    path: Path,
    *,
    engine: str = "",
    legacy_candidate: Path | None = None,
    recovery_path: Path | None = None,
    manifest: BrowserProfileManifest | None = None,
    detail: str = "",
) -> ProfileStorageInspection:
    return ProfileStorageInspection(
        status=status,
        profile_id=profile_id,
        path=path,
        engine=engine,
        legacy_candidate=legacy_candidate,
        recovery_path=recovery_path,
        manifest=manifest,
        detail=detail,
    )


def _inspect_existing(
    directory: Path,
    profile_id: str,
    expected_engine: str | None,
    *,
    legacy_candidate: Path | None = None,
) -> ProfileStorageInspection:
    try:
        manifest = read_profile_manifest(directory)
    except BrowserProfileManifestError as exc:
        return _inspection(
            ProfileStorageStatus.CORRUPT,
            profile_id,
            directory,
            engine=expected_engine or "",
            legacy_candidate=legacy_candidate,
            detail=str(exc),
        )
    if manifest.profile_id != profile_id:
        return _inspection(
            ProfileStorageStatus.MISMATCH,
            profile_id,
            directory,
            engine=expected_engine or manifest.engine,
            legacy_candidate=legacy_candidate,
            manifest=manifest,
            detail="Manifest identity does not match this profile.",
        )
    if expected_engine is not None and manifest.engine != expected_engine:
        return _inspection(
            ProfileStorageStatus.MISMATCH,
            profile_id,
            directory,
            engine=expected_engine,
            legacy_candidate=legacy_candidate,
            manifest=manifest,
            detail="Manifest engine does not match this profile.",
        )
    engine = expected_engine or manifest.engine
    if engine not in _STORAGE_ENGINES or not profile_layout_ready(directory, engine):
        return _inspection(
            ProfileStorageStatus.INCOMPATIBLE,
            profile_id,
            directory,
            engine=engine,
            legacy_candidate=legacy_candidate,
            manifest=manifest,
            detail=f"Profile does not contain the required {engine} browser layout.",
        )
    status = (
        ProfileStorageStatus.NEEDS_SETUP
        if legacy_candidate is not None
        else ProfileStorageStatus.READY
    )
    return _inspection(
        status,
        profile_id,
        directory,
        engine=engine,
        legacy_candidate=legacy_candidate,
        manifest=manifest,
        detail=(
            "A compatible legacy folder is available for explicit adoption."
            if legacy_candidate is not None
            else "Profile storage is ready."
        ),
    )


def inspect_profile_storage(
    profile_id: str,
    engine: str | None = None,
    *,
    legacy_storage_name: str = "",
    profiles_root: str | Path | None = None,
) -> ProfileStorageInspection:
    """Inspect UUID storage and, optionally, one explicit legacy candidate.

    Looking at a same-name legacy candidate never binds, creates, moves, or
    modifies it.  Invalid UUID input still raises because it is a caller/data
    contract error; filesystem and manifest problems are represented as status.
    """
    profile_id = canonical_profile_id(profile_id)
    expected_engine = _canonical_expected_engine(engine) if engine is not None else None
    root = managed_profiles_root(profiles_root)
    destination = root / profile_id
    try:
        destination = resolve_profile_storage_path(profile_id, root)
    except BrowserProfilePathError as exc:
        return _inspection(
            ProfileStorageStatus.CORRUPT,
            profile_id,
            destination,
            engine=expected_engine or "",
            detail=str(exc),
        )

    if _lexists(destination):
        try:
            directory = _validated_existing_directory(
                destination,
                "Browser profile storage",
            )
        except ProfileStorageValidationError as exc:
            return _inspection(
                ProfileStorageStatus.CORRUPT,
                profile_id,
                destination,
                engine=expected_engine or "",
                detail=str(exc),
            )
        inspected = _inspect_existing(directory, profile_id, expected_engine)
        if (
            legacy_storage_name
            and inspected.status is ProfileStorageStatus.READY
        ):
            return ProfileStorageInspection(
                status=ProfileStorageStatus.NEEDS_SETUP,
                profile_id=profile_id,
                path=destination,
                engine=inspected.engine,
                manifest=inspected.manifest,
                detail=(
                    "UUID storage is ready, but the migrated database record is still "
                    "unbound. Finish setup explicitly before using this profile."
                ),
            )
        return inspected

    inventory = inspect_pending_profile_deletes(root)
    blocked = [
        item for item in inventory.blocked if item.profile_id == profile_id
    ]
    if blocked:
        diagnostic = blocked[0]
        recovery_path = (
            diagnostic.candidate_paths[0]
            if diagnostic.candidate_paths
            else None
        )
        return _inspection(
            ProfileStorageStatus.CORRUPT,
            profile_id,
            destination,
            engine=expected_engine or "",
            recovery_path=recovery_path,
            detail=(
                f"{diagnostic.detail} No recovery candidate was selected. "
                "Candidates: "
                + ", ".join(str(path) for path in diagnostic.candidate_paths)
            ),
        )
    tombstones = [
        item for item in inventory.pending if item.profile_id == profile_id
    ]
    if tombstones:
        if len(tombstones) != 1:
            return _inspection(
                ProfileStorageStatus.CORRUPT,
                profile_id,
                destination,
                engine=expected_engine or "",
                detail="Multiple interrupted profile deletions require manual review.",
            )
        tombstone = tombstones[0].tombstone_path
        try:
            tombstone = _validated_existing_directory(
                tombstone,
                "Interrupted browser profile deletion",
            )
            manifest = _manifest_matches(tombstone, profile_id, expected_engine)
        except ProfileStorageValidationError as exc:
            return _inspection(
                ProfileStorageStatus.CORRUPT,
                profile_id,
                destination,
                engine=expected_engine or "",
                recovery_path=tombstone,
                detail=str(exc),
            )
        return _inspection(
            ProfileStorageStatus.NEEDS_SETUP,
            profile_id,
            destination,
            engine=manifest.engine,
            recovery_path=tombstone,
            manifest=manifest,
            detail=(
                "Profile deletion was interrupted. Restore it if the database record "
                "still exists, or finish deleting it if the record was removed."
            ),
        )

    if not legacy_storage_name:
        return _inspection(
            ProfileStorageStatus.MISSING,
            profile_id,
            destination,
            engine=expected_engine or "",
            detail="Profile storage has not been created or attached.",
        )

    try:
        legacy = resolve_legacy_profile_path(legacy_storage_name, root)
        if not _lexists(legacy):
            return _inspection(
                ProfileStorageStatus.MISSING,
                profile_id,
                destination,
                engine=expected_engine or "",
                legacy_candidate=legacy,
                detail="The recorded legacy profile folder is missing.",
            )
        legacy = _validated_existing_directory(legacy, "Legacy browser profile")
    except (BrowserProfilePathError, ProfileStorageValidationError) as exc:
        return _inspection(
            ProfileStorageStatus.CORRUPT,
            profile_id,
            destination,
            engine=expected_engine or "",
            detail=str(exc),
        )

    manifest_path = legacy / PROFILE_MANIFEST_FILENAME
    if _lexists(manifest_path):
        inspected = _inspect_existing(
            legacy,
            profile_id,
            expected_engine,
            legacy_candidate=legacy,
        )
        return ProfileStorageInspection(
            status=inspected.status,
            profile_id=inspected.profile_id,
            path=destination,
            engine=inspected.engine,
            legacy_candidate=legacy,
            manifest=inspected.manifest,
            detail=inspected.detail,
        )

    if expected_engine is not None and not profile_layout_ready(legacy, expected_engine):
        return _inspection(
            ProfileStorageStatus.INCOMPATIBLE,
            profile_id,
            destination,
            engine=expected_engine,
            legacy_candidate=legacy,
            detail=(
                f"Legacy folder does not contain the required {expected_engine} "
                "browser layout."
            ),
        )
    return _inspection(
        ProfileStorageStatus.NEEDS_SETUP,
        profile_id,
        destination,
        engine=expected_engine or "",
        legacy_candidate=legacy,
        detail="A legacy folder is available for explicit adoption.",
    )
