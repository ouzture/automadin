"""Safe pre-restart staging for fully inspected Automadin portability bundles."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath

from automadin.services import portability_bundle as bundle
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseCleanupError,
    BrowserProfileLeaseRegistry,
    MaintenanceProfileScope,
    ProfileUse,
    browser_profile_leases,
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
from automadin.services.database_transfer import PENDING_IMPORT_NAME
from automadin.services.portability_activation import (
    ACTIVATION_PLAN_NAME,
    PRIVATE_DATABASE_NAME,
    KeptLocalProfile,
    PortabilityActivationPlan,
    StagedProfileInput,
    stage_portability_activation,
)

_CHUNK_SIZE = 1024 * 1024


class PortabilityStagingError(RuntimeError):
    """Base class for safe portability staging failures."""


class PortabilityStagingRecoveryError(PortabilityStagingError):
    """Raised when pre-publication cleanup cannot be confirmed."""

    def __init__(
        self,
        message: str,
        recovery_paths: tuple[Path, ...],
    ) -> None:
        self.recovery_paths = recovery_paths
        rendered = "\n".join(str(path) for path in recovery_paths)
        super().__init__(f"{message}\nRecovery paths:\n{rendered}")


class ProfileRestoreDecision(str, Enum):
    INSTALL = "install"
    KEEP_LOCAL = "keep_local"


@dataclass(frozen=True, slots=True)
class PortabilityStagingResult:
    source: Path
    activation_plan: PortabilityActivationPlan
    installed_profile_ids: tuple[str, ...]
    kept_local_profile_ids: tuple[str, ...]
    staged_profile_paths: tuple[Path, ...]
    cleanup_paths: tuple[Path, ...] = ()
    warning: str = bundle.PORTABILITY_WARNING
    cleanup_error: str | None = None
    maintenance_cleanup_scope: MaintenanceProfileScope | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class _DecisionProfile:
    profile_id: str
    display_name: str
    engine: str
    included: bool


def _notify(
    progress: bundle.ProgressCallback | None,
    phase: str,
    completed: int,
    total: int,
) -> None:
    if progress is not None:
        try:
            progress(bundle.BundleProgress(phase, completed, total))
        except Exception:
            # Progress is observational and cannot cross the durable plan's
            # commit boundary as a false failure.
            pass


def _cancelled(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise bundle.PortabilityBundleCancelledError(
            "Portable backup import staging was cancelled."
        )


def _real_direct_root(path: str | Path) -> Path:
    lexical = Path(os.path.abspath(Path(path).expanduser()))
    lexical.mkdir(parents=True, exist_ok=True)
    metadata = os.lstat(lexical)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if (
        lexical.resolve(strict=True) != lexical
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (reparse and int(getattr(metadata, "st_file_attributes", 0)) & reparse)
    ):
        raise PortabilityStagingError(f"Staging root is redirected or unsafe: {lexical}")
    return lexical


def _safe_source_archive(
    source: str | Path,
    inspection: bundle.PortabilityBundleInspection,
    limits: bundle.BundleLimits,
) -> Path:
    raw = Path(source).expanduser()
    try:
        resolved = raw.resolve(strict=True)
        metadata = os.stat(resolved)
    except OSError as exc:
        raise PortabilityStagingError("The selected portability bundle is unavailable.") from exc
    if (
        resolved != inspection.path
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > limits.max_archive_bytes
    ):
        raise PortabilityStagingError(
            "The selected bundle changed, is oversized, or is not the exact resolved "
            "regular archive that was inspected."
        )
    return resolved


def _declared_members(
    manifest: bundle.PortabilityBundleManifest,
) -> dict[str, bundle.BundleFile | bundle.BundleDatabase]:
    declared: dict[str, bundle.BundleFile | bundle.BundleDatabase] = {
        manifest.database.path: manifest.database
    }
    for profile in manifest.profiles:
        for item in profile.files:
            member = f"profiles/{profile.profile_id}/{item.path}"
            if member in declared:
                raise PortabilityStagingError(
                    "The inspected manifest declares a member more than once."
                )
            declared[member] = item
    return declared


def _revalidate_structure(
    source: Path,
    inspection: bundle.PortabilityBundleInspection,
    limits: bundle.BundleLimits,
) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    try:
        bundle.preflight_zip_central_directory(source, limits=limits)
        archive = zipfile.ZipFile(source, "r", allowZip64=True)
        infos = archive.infolist()
        if not infos or len(infos) > limits.max_members:
            raise PortabilityStagingError("Bundle member count is invalid.")
        names: dict[str, zipfile.ZipInfo] = {}
        folded: set[str] = set()
        path_nodes: dict[str, tuple[str, bool]] = {}
        expanded = 0
        compressed = 0
        for info in infos:
            name = bundle._validate_zip_info(info, limits)  # noqa: SLF001
            bundle._register_archive_path(name, path_nodes)  # noqa: SLF001
            if name in names or name.casefold() in folded:
                raise PortabilityStagingError("Bundle repeats an archive path.")
            names[name] = info
            folded.add(name.casefold())
            expanded += info.file_size
            compressed += info.compress_size
        if expanded > limits.max_total_expanded_bytes:
            raise PortabilityStagingError("Bundle exceeds the expanded-size limit.")
        if expanded and compressed and expanded / compressed > limits.max_compression_ratio:
            raise PortabilityStagingError("Bundle exceeds the aggregate compression-ratio limit.")
        manifest_info = names.get(bundle.BUNDLE_MANIFEST_PATH)
        if manifest_info is None or manifest_info.file_size > limits.max_manifest_bytes:
            raise PortabilityStagingError("Bundle has no valid bundle.json.")
        with archive.open(manifest_info) as stream:
            payload = stream.read(limits.max_manifest_bytes + 1)
        if len(payload) > limits.max_manifest_bytes:
            raise PortabilityStagingError("bundle.json exceeds the manifest limit.")
        current_manifest = bundle._parse_manifest(payload, limits)  # noqa: SLF001
        if current_manifest != inspection.manifest:
            raise PortabilityStagingError("bundle.json changed after the bundle was inspected.")
        declared = _declared_members(current_manifest)
        if set(names) != set(declared) | {bundle.BUNDLE_MANIFEST_PATH}:
            raise PortabilityStagingError("Bundle member set no longer matches bundle.json.")
        for name, expected in declared.items():
            if names[name].file_size != expected.size:
                raise PortabilityStagingError(
                    f"Bundle member size changed after inspection: {name}"
                )
        return archive, names
    except Exception:
        if "archive" in locals():
            archive.close()
        raise


def _normalized_decisions(
    inspection: bundle.PortabilityBundleInspection,
    decisions: Mapping[str, ProfileRestoreDecision],
) -> dict[str, ProfileRestoreDecision]:
    included = {profile.profile_id for profile in inspection.manifest.profiles}
    omitted = {item.profile_id: item for item in inspection.omitted_collisions}
    expected_omitted = {
        item.profile_id for item in inspection.database_profiles if not item.included
    }
    if set(omitted) != expected_omitted:
        raise PortabilityStagingError(
            "The bundle must be fully inspected with omitted-profile collision classification."
        )
    required_omitted: set[str] = set()
    for profile_id, collision in omitted.items():
        if collision.outcome is bundle.OmittedProfileCollision.READY:
            required_omitted.add(profile_id)
        elif collision.outcome is bundle.OmittedProfileCollision.ABSENT:
            continue
        else:
            raise PortabilityStagingError(
                f"Omitted database profile {profile_id} is blocked by local collision: "
                f"{collision.outcome.value}."
            )
    normalized: dict[str, ProfileRestoreDecision] = {}
    for raw_id, raw_decision in decisions.items():
        profile_id = canonical_profile_id(raw_id)
        try:
            decision = ProfileRestoreDecision(raw_decision)
        except ValueError as exc:
            raise PortabilityStagingError(
                f"Unknown restore decision for profile {profile_id}."
            ) from exc
        if profile_id in normalized:
            raise PortabilityStagingError("Restore decisions repeat a profile UUID.")
        normalized[profile_id] = decision
    if set(normalized) != included | required_omitted:
        raise PortabilityStagingError(
            "Choose exactly one required restore action for every included or "
            "ready omitted profile."
        )
    collisions = {item.profile_id: item for item in inspection.collisions}
    if set(collisions) != included:
        raise PortabilityStagingError(
            "The bundle must be fully inspected with local collision classification."
        )
    for profile_id in sorted(included):
        outcome = collisions[profile_id].outcome
        decision = normalized[profile_id]
        if outcome is bundle.LocalProfileCollision.ABSENT:
            if decision is not ProfileRestoreDecision.INSTALL:
                raise PortabilityStagingError(f"Absent profile {profile_id} must be installed.")
        elif outcome in {
            bundle.LocalProfileCollision.IDENTICAL,
            bundle.LocalProfileCollision.DIFFERENT,
        }:
            if decision is not ProfileRestoreDecision.KEEP_LOCAL:
                raise PortabilityStagingError(
                    f"Existing profile {profile_id} must explicitly keep local data."
                )
        else:
            raise PortabilityStagingError(
                f"Profile {profile_id} is blocked by local collision: {outcome.value}."
            )
    for profile_id in sorted(required_omitted):
        if normalized[profile_id] is not ProfileRestoreDecision.KEEP_LOCAL:
            raise PortabilityStagingError(
                f"Omitted database profile {profile_id} must explicitly keep local data."
            )
    return normalized


def _validate_current_local_state(
    profiles: Mapping[str, _DecisionProfile],
    decisions: Mapping[str, ProfileRestoreDecision],
    profiles_root: Path,
) -> None:
    for profile_id, decision in decisions.items():
        profile = profiles[profile_id]
        destination = resolve_profile_storage_path(profile_id, profiles_root)
        if decision is ProfileRestoreDecision.INSTALL:
            if os.path.lexists(destination):
                raise PortabilityStagingError(f"Profile {profile_id} appeared after inspection.")
            continue
        current = inspect_profile_storage(
            profile_id,
            profile.engine,
            profiles_root=profiles_root,
        )
        if current.status is not ProfileStorageStatus.READY:
            raise PortabilityStagingError(
                f"Kept-local profile {profile_id} changed after inspection: {current.detail}"
            )
        try:
            profile_tree_lstat_fingerprint(current.path)
        except Exception as exc:
            raise PortabilityStagingError(
                f"Kept-local profile {profile_id} has an unsafe tree; review again: {exc}"
            ) from exc


def _revalidate_identical_keeps(
    inspection: bundle.PortabilityBundleInspection,
    decisions: Mapping[str, ProfileRestoreDecision],
    profiles_root: Path,
    limits: bundle.BundleLimits,
    cancel: Callable[[], bool] | None,
) -> dict[str, str]:
    profiles = {profile.profile_id: profile for profile in inspection.manifest.profiles}
    reviewed_fingerprints: dict[str, str] = {}
    for collision in inspection.collisions:
        if (
            collision.outcome is not bundle.LocalProfileCollision.IDENTICAL
            or decisions.get(collision.profile_id) is not ProfileRestoreDecision.KEEP_LOCAL
        ):
            continue
        try:
            before = profile_tree_lstat_fingerprint(collision.local_path)
        except Exception as exc:
            raise PortabilityStagingError(
                f"Kept-local profile {collision.profile_id} became unsafe; "
                "review the collision decision again."
            ) from exc
        current = bundle._classify_collision(  # noqa: SLF001
            profiles[collision.profile_id],
            profiles_root,
            limits,
            cancel,
        )
        try:
            after = profile_tree_lstat_fingerprint(current.local_path)
        except Exception as exc:
            raise PortabilityStagingError(
                f"Kept-local profile {collision.profile_id} became unsafe; "
                "review the collision decision again."
            ) from exc
        if current.outcome is not bundle.LocalProfileCollision.IDENTICAL or before != after:
            raise PortabilityStagingError(
                f"Kept-local profile {collision.profile_id} changed after inspection; "
                "review the collision decision again."
            )
        reviewed_fingerprints[collision.profile_id] = after
    return reviewed_fingerprints


def _pending_paths(data_root: Path) -> tuple[Path, ...]:
    return (
        data_root / ACTIVATION_PLAN_NAME,
        data_root / PRIVATE_DATABASE_NAME,
        data_root / PENDING_IMPORT_NAME,
    )


def _active_database_path(path: str | Path) -> tuple[Path, Path]:
    active = Path(os.path.abspath(Path(path).expanduser()))
    data_root = _real_direct_root(active.parent)
    expected = data_root / "automadin.db"
    if active != expected:
        raise PortabilityStagingError(
            "The active database path must be the direct data-root file automadin.db."
        )
    if os.path.lexists(active):
        metadata = os.lstat(active)
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if (
            active.resolve(strict=True) != active
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (reparse and int(getattr(metadata, "st_file_attributes", 0)) & reparse)
        ):
            raise PortabilityStagingError(
                "The active automadin.db is redirected or is not a regular file."
            )
    return active, data_root


def _safe_direct_regular_file_size(
    path: Path,
    parent: Path,
    description: str,
) -> int:
    """Return a sidecar's size only when it is one private, direct file."""
    if not os.path.lexists(path):
        return 0
    lexical = Path(os.path.abspath(path))
    try:
        metadata = os.lstat(lexical)
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PortabilityStagingError(f"{description} is unavailable or unsafe.") from exc
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if (
        lexical.parent != parent
        or resolved != lexical
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (reparse and int(getattr(metadata, "st_file_attributes", 0)) & reparse)
        or metadata.st_nlink != 1
    ):
        raise PortabilityStagingError(
            f"{description} is redirected, hard-linked, or not a regular file."
        )
    return metadata.st_size


def _create_profile_staging(root: Path, profile_id: str) -> Path:
    path = root / f".importing-{profile_id}-{uuid.uuid4().hex}"
    if path.parent != root or os.path.lexists(path):
        raise PortabilityStagingError("A safe profile staging path could not be reserved.")
    path.mkdir()
    if path.resolve(strict=True) != path:
        raise PortabilityStagingError("Profile staging path was redirected.")
    return path


def _validated_output_parent(staging_root: Path, relative_parent: PurePosixPath) -> Path:
    def validate(path: Path) -> None:
        metadata = os.lstat(path)
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if (
            path.resolve(strict=True) != path
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (reparse and int(getattr(metadata, "st_file_attributes", 0)) & reparse)
        ):
            raise PortabilityStagingError(f"Staged output parent became unsafe: {path}")

    validate(staging_root)
    current = staging_root
    for component in relative_parent.parts:
        if len(component) > 255:
            raise PortabilityStagingError("Staged output component exceeds 255 characters.")
        next_path = current / component
        if os.path.lexists(next_path):
            validate(next_path)
        else:
            next_path.mkdir()
            validate(next_path)
        validate(current)
        current = next_path
    if current != staging_root and staging_root not in current.parents:
        raise PortabilityStagingError("Staged output left its generated profile root.")
    return current


def _open_output(staging_root: Path, relative: str):
    relative_path = PurePosixPath(relative)
    if len(relative_path.name) > 255:
        raise PortabilityStagingError("Staged output component exceeds 255 characters.")
    parent = _validated_output_parent(staging_root, relative_path.parent)
    destination = parent / relative_path.name
    if os.path.lexists(destination):
        raise PortabilityStagingError(f"Staged output already exists: {destination}")
    stream = destination.open("xb")
    try:
        _validated_output_parent(staging_root, relative_path.parent)
    except Exception:
        stream.close()
        raise
    metadata = os.lstat(destination)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        stream.close()
        raise PortabilityStagingError(f"Staged output is not a private file: {destination}")
    return stream


def _stream_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    expected: bundle.BundleFile | bundle.BundleDatabase,
    destination,
    *,
    cancel: Callable[[], bool] | None,
    advance: Callable[[int], None],
) -> None:
    digest = hashlib.sha256()
    total = 0
    with archive.open(info) as source:
        while True:
            _cancelled(cancel)
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > expected.size:
                raise PortabilityStagingError(
                    f"Bundle member exceeded its declared size: {info.filename}"
                )
            digest.update(chunk)
            destination.write(chunk)
            advance(len(chunk))
    if total != expected.size or digest.hexdigest() != expected.sha256:
        raise PortabilityStagingError(f"Bundle member size/hash mismatch: {info.filename}")


class _NullWriter:
    @staticmethod
    def write(value: bytes) -> int:
        return len(value)


def _safe_remove_staging(path: Path, root: Path) -> None:
    if not os.path.lexists(path):
        return
    lexical = Path(os.path.abspath(path))
    if lexical.parent != root or not lexical.name.startswith(
        (".importing-", ".automadin-portability-staging-")
    ):
        raise PortabilityStagingError(f"Refusing to clean an unexpected path: {path}")
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if (
        path.resolve(strict=True) != lexical
        or stat.S_ISLNK(metadata.st_mode)
        or (reparse and int(getattr(metadata, "st_file_attributes", 0)) & reparse)
    ):
        raise PortabilityStagingError(f"Refusing to clean redirected staging: {path}")
    if stat.S_ISDIR(metadata.st_mode):
        profile_tree_lstat_fingerprint(path)
        shutil.rmtree(path)
    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
        path.unlink()
    else:
        raise PortabilityStagingError(f"Refusing to clean unsafe staging: {path}")


def _sync_directory_tree(root: Path) -> None:
    if os.name == "nt":
        return
    profile_tree_lstat_fingerprint(root)
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for directory in reversed(directories):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_prepublication(
    paths: list[tuple[Path, Path]],
    cause: BaseException,
) -> None:
    uncertain: list[Path] = []
    for path, root in reversed(paths):
        try:
            _safe_remove_staging(path, root)
        except Exception:
            uncertain.append(path)
    if uncertain:
        raise PortabilityStagingRecoveryError(
            f"Portability staging failed ({cause}) and cleanup could not be confirmed.",
            tuple(uncertain),
        ) from cause


def stage_portability_bundle_import(
    source: str | Path,
    inspection: bundle.PortabilityBundleInspection,
    active_db_path: str | Path,
    decisions: Mapping[str, ProfileRestoreDecision],
    *,
    profiles_root: str | Path,
    lease_registry: BrowserProfileLeaseRegistry = browser_profile_leases,
    cancel: Callable[[], bool] | None = None,
    progress: bundle.ProgressCallback | None = None,
) -> PortabilityStagingResult:
    """Stage a verified bundle and atomically publish a restart activation plan."""
    if not isinstance(inspection, bundle.PortabilityBundleInspection):
        raise TypeError("inspection must be a PortabilityBundleInspection")
    limits = bundle.BundleLimits()
    archive_path = _safe_source_archive(source, inspection, limits)
    active_db, data_root = _active_database_path(active_db_path)
    for pending in _pending_paths(data_root):
        if os.path.lexists(pending):
            raise FileExistsError(f"A database or portability activation is pending at {pending}.")
    profile_root = _real_direct_root(managed_profiles_root(profiles_root))
    normalized = _normalized_decisions(inspection, decisions)
    payload_profiles = {profile.profile_id: profile for profile in inspection.manifest.profiles}
    database_profiles = {profile.profile_id: profile for profile in inspection.database_profiles}
    decision_profiles: dict[str, _DecisionProfile] = {}
    for profile_id in normalized:
        database_profile = database_profiles[profile_id]
        decision_profiles[profile_id] = _DecisionProfile(
            profile_id=profile_id,
            display_name=database_profile.display_name,
            engine=database_profile.engine,
            included=database_profile.included,
        )
    _validate_current_local_state(
        decision_profiles,
        normalized,
        profile_root,
    )
    installed_ids = tuple(
        sorted(
            profile_id
            for profile_id, decision in normalized.items()
            if decision is ProfileRestoreDecision.INSTALL
        )
    )
    kept_ids = tuple(
        sorted(
            profile_id
            for profile_id, decision in normalized.items()
            if decision is ProfileRestoreDecision.KEEP_LOCAL
        )
    )
    database_temp = data_root / f".automadin-portability-staging-{uuid.uuid4().hex}.db"
    install_size = sum(payload_profiles[item].total_size for item in installed_ids)
    database_size = inspection.manifest.database.size
    active_database_size = active_db.stat().st_size if active_db.is_file() else 0
    active_wal_size = _safe_direct_regular_file_size(
        active_db.with_name(f"{active_db.name}-wal"),
        data_root,
        "The active database WAL",
    )
    active_backup_size = active_database_size + active_wal_size
    data_required = max(
        database_size * 2,
        database_size + active_backup_size,
    )
    if os.stat(data_root).st_dev == os.stat(profile_root).st_dev:
        required = data_required + install_size
        required += max(64 * 1024**2, required // 20)
        if shutil.disk_usage(data_root).free < required:
            raise PortabilityStagingError(
                "There is not enough free space to stage the portable backup."
            )
    else:
        data_required += max(64 * 1024**2, data_required // 20)
        profile_required = install_size + max(64 * 1024**2, install_size // 20)
        if shutil.disk_usage(data_root).free < data_required:
            raise PortabilityStagingError(
                "There is not enough free space to stage the bundled database."
            )
        if shutil.disk_usage(profile_root).free < profile_required:
            raise PortabilityStagingError(
                "There is not enough free space to stage browser profiles."
            )

    maintenance_scope = lease_registry.acquire_maintenance_many(
        sorted(decision_profiles),
        ProfileUse(
            "maintenance",
            "staging a portable browser-profile restore",
            str(uuid.uuid4()),
        ),
    )
    cleanup: list[tuple[Path, Path]] = []
    staging_by_id: dict[str, Path] = {}
    published = False
    database_signature: tuple[int, int, int, int, int, int] | None = None
    result: PortabilityStagingResult | None = None
    operation_error: BaseException | None = None
    prepublication_cleanup_error: BaseException | None = None
    try:
        _validate_current_local_state(
            decision_profiles,
            normalized,
            profile_root,
        )
        reviewed_fingerprints = _revalidate_identical_keeps(
            inspection,
            normalized,
            profile_root,
            limits,
            cancel,
        )
        for profile_id in installed_ids:
            staging = _create_profile_staging(profile_root, profile_id)
            staging_by_id[profile_id] = staging
            cleanup.append((staging, profile_root))
        cleanup.append((database_temp, data_root))

        archive, names = _revalidate_structure(archive_path, inspection, limits)
        declared = _declared_members(inspection.manifest)
        total_bytes = sum(item.size for item in declared.values())
        completed = 0
        current_phase = "staging_database"

        def advance(byte_count: int) -> None:
            nonlocal completed
            completed += byte_count
            _notify(progress, current_phase, completed, total_bytes)

        _notify(progress, current_phase, completed, total_bytes)
        try:
            for member_name, expected in declared.items():
                _cancelled(cancel)
                info = names[member_name]
                if member_name == inspection.manifest.database.path:
                    with database_temp.open("xb") as destination:
                        _stream_member(
                            archive,
                            info,
                            expected,
                            destination,
                            cancel=cancel,
                            advance=advance,
                        )
                        destination.flush()
                        os.fsync(destination.fileno())
                    metadata = os.lstat(database_temp)
                    if (
                        database_temp.resolve(strict=True) != database_temp
                        or not stat.S_ISREG(metadata.st_mode)
                        or stat.S_ISLNK(metadata.st_mode)
                        or metadata.st_nlink != 1
                    ):
                        raise PortabilityStagingError("The private staged database became unsafe.")
                    database_signature = (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_mode,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                    )
                    continue
                _prefix, profile_id, relative = member_name.split("/", 2)
                decision = normalized[profile_id]
                current_phase = (
                    "staging_profiles"
                    if decision is ProfileRestoreDecision.INSTALL
                    else "verifying_kept_profiles"
                )
                if decision is ProfileRestoreDecision.INSTALL:
                    with _open_output(staging_by_id[profile_id], relative) as destination:
                        _stream_member(
                            archive,
                            info,
                            expected,
                            destination,
                            cancel=cancel,
                            advance=advance,
                        )
                        destination.flush()
                        os.fsync(destination.fileno())
                else:
                    _stream_member(
                        archive,
                        info,
                        expected,
                        _NullWriter(),
                        cancel=cancel,
                        advance=advance,
                    )
        finally:
            archive.close()
        _cancelled(cancel)
        if database_signature is None:
            raise PortabilityStagingError("The bundled database was not staged.")
        current_database = os.lstat(database_temp)
        if (
            current_database.st_dev,
            current_database.st_ino,
            current_database.st_mode,
            current_database.st_size,
            current_database.st_mtime_ns,
            current_database.st_ctime_ns,
        ) != database_signature:
            raise PortabilityStagingError(
                "The private staged database changed before plan publication."
            )
        for staging in staging_by_id.values():
            _sync_directory_tree(staging)
        _sync_directory(profile_root)
        _sync_directory(data_root)
        _validate_current_local_state(
            decision_profiles,
            normalized,
            profile_root,
        )
        _cancelled(cancel)
        _notify(progress, "publishing_activation_plan", completed, total_bytes)
        plan = stage_portability_activation(
            data_root,
            database_temp,
            tuple(
                StagedProfileInput(
                    profile_id,
                    decision_profiles[profile_id].engine,
                    staging_by_id[profile_id],
                )
                for profile_id in installed_ids
            ),
            tuple(
                KeptLocalProfile(
                    profile_id,
                    decision_profiles[profile_id].engine,
                    reviewed_fingerprints.get(profile_id),
                )
                for profile_id in kept_ids
            ),
            lease_registry=lease_registry,
            maintenance_leases={
                profile_id: maintenance_scope.for_profile(profile_id)
                for profile_id in installed_ids
            },
            profiles_root=profile_root,
        )
        published = True
        cleanup_paths: list[Path] = []
        try:
            _safe_remove_staging(database_temp, data_root)
        except Exception:
            cleanup_paths.append(database_temp)
        _notify(progress, "complete", total_bytes, total_bytes)
        result = PortabilityStagingResult(
            source=archive_path,
            activation_plan=plan,
            installed_profile_ids=installed_ids,
            kept_local_profile_ids=kept_ids,
            staged_profile_paths=tuple(staging_by_id[item] for item in installed_ids),
            cleanup_paths=tuple(cleanup_paths),
        )
    except BaseException as exc:
        operation_error = exc
        if not published:
            try:
                _cleanup_prepublication(cleanup, exc)
            except BaseException as cleanup_exc:
                prepublication_cleanup_error = cleanup_exc

    cleanup_error: Exception | None = None
    cleanup_scope: MaintenanceProfileScope | None = None
    try:
        maintenance_scope.release()
    except Exception as exc:
        cleanup_error = exc
        if isinstance(exc, BrowserProfileLeaseCleanupError):
            cleanup_scope = exc.scope

    if operation_error is not None:
        if cleanup_error is not None:
            if prepublication_cleanup_error is not None:
                cleanup_error.__cause__ = prepublication_cleanup_error
            raise operation_error.with_traceback(
                operation_error.__traceback__
            ) from cleanup_error
        if prepublication_cleanup_error is not None:
            raise operation_error.with_traceback(
                operation_error.__traceback__
            ) from prepublication_cleanup_error
        raise operation_error.with_traceback(operation_error.__traceback__)
    if result is None:
        raise RuntimeError("Portable backup staging ended without an outcome.")
    if cleanup_error is not None:
        detail = str(cleanup_error)
        return replace(
            result,
            warning=(
                f"{result.warning}\n\nThe restore was staged, but maintenance cleanup "
                f"needs attention: {detail}"
            ),
            cleanup_error=detail,
            maintenance_cleanup_scope=cleanup_scope,
        )
    return result
