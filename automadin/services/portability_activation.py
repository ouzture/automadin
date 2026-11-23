"""Durable restart activation for non-destructive portability restores."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseCleanupError,
    BrowserProfileLeaseRegistry,
    MaintenanceProfileLease,
    MaintenanceProfileScope,
    ProfileUse,
    browser_profile_leases,
)
from automadin.services.browser_profile_storage import (
    ProfileStorageStatus,
    StagedProfilePublication,
    inspect_profile_storage,
    prepare_staged_profile_publication,
    profile_tree_lstat_fingerprint,
)
from automadin.services.browser_profile_manifest import SUPPORTED_PROFILE_ENGINES
from automadin.services.browser_profile_paths import (
    canonical_profile_id,
    managed_profiles_root,
    resolve_profile_storage_path,
)
from automadin.services.database import CURRENT_SCHEMA_VERSION
from automadin.services.database_transfer import (
    PENDING_IMPORT_NAME,
    PORTABILITY_ACTIVATION_PLAN_NAME,
    PORTABILITY_PRIVATE_DATABASE_NAME,
    apply_portability_owned_database_import as apply_pending_database_import,
    inspect_database,
    stage_database_copy,
)

ACTIVATION_PLAN_NAME = PORTABILITY_ACTIVATION_PLAN_NAME
PRIVATE_DATABASE_NAME = PORTABILITY_PRIVATE_DATABASE_NAME
STAGING_REMNANT_REPORTED_NAME = ".automadin-portability-remnants-reported.json"
_DATABASE_REMNANT = re.compile(
    r"^(?P<base>(?:\.automadin-portability-staging-[0-9a-f]{32}\.db|"
    r"\.automadin-portability-pending\.db\.[0-9a-f]{32}\.tmp|"
    r"\.automadin-import-pending\.db\.[0-9a-f]{32}\.tmp|"
    r"\.automadin\.db\.restore-[0-9a-f]{32}\.tmp))"
    r"(?P<suffix>-wal|-shm|-journal)?$"
)
ACTIVATION_FORMAT = "automadin.portability-activation"
ACTIVATION_VERSION = 1
MAX_DATABASE_PROFILES = 10_000
_MAX_PROFILE_DISPLAY_NAME_CHARS = 200
_MAX_PROFILE_ENGINE_CHARS = 32
_MAX_SQLITE_VALUE_BYTES = 1024 * 1024


class PortabilityActivationError(RuntimeError):
    """Raised when an activation state is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class StagedProfileInput:
    profile_id: str
    engine: str
    path: Path


@dataclass(frozen=True, slots=True)
class KeptLocalProfile:
    profile_id: str
    engine: str
    reviewed_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class PortabilityActivationPlan:
    plan_id: str
    path: Path
    private_database_path: Path
    profile_ids: tuple[str, ...]
    kept_local_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortabilityActivationResult:
    applied: bool
    backup_path: Path | None = None
    rejected_path: Path | None = None
    error: str | None = None
    recovery_paths: tuple[Path, ...] = ()
    failed_plan_path: Path | None = None
    database_activated: bool = False
    unresolved_paths: tuple[Path, ...] = ()
    maintenance_cleanup_scope: MaintenanceProfileScope | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def _maintenance_acquisition_blocked_result(
    root: Path,
    journal_path: Path,
    error: Exception,
    cleanup_scope: MaintenanceProfileScope | None = None,
) -> PortabilityActivationResult:
    """Report a retryable ownership conflict without consuming activation state."""
    preserved = [journal_path]
    for candidate in (
        root / PRIVATE_DATABASE_NAME,
        root / PENDING_IMPORT_NAME,
    ):
        if os.path.lexists(candidate):
            preserved.append(candidate)
    return PortabilityActivationResult(
        False,
        error=(
            "Portable restore activation is waiting for browser-profile maintenance "
            f"ownership; its activation files were left in place for retry. {error}"
        ),
        unresolved_paths=tuple(dict.fromkeys(preserved)),
        maintenance_cleanup_scope=cleanup_scope,
    )


def _with_maintenance_release_error(
    result: PortabilityActivationResult | None,
    error: Exception,
) -> PortabilityActivationResult:
    """Preserve the decided activation outcome while reporting cleanup failure."""
    detail = f"Browser-profile maintenance ownership cleanup failed: {error}"
    cleanup_scope = (
        error.scope if isinstance(error, BrowserProfileLeaseCleanupError) else None
    )
    if result is None:
        return PortabilityActivationResult(
            False,
            error=detail,
            maintenance_cleanup_scope=cleanup_scope,
        )
    combined_error = f"{result.error} {detail}" if result.error else detail
    return replace(
        result,
        error=combined_error,
        maintenance_cleanup_scope=cleanup_scope,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_data_dir(data_dir: str | Path) -> Path:
    lexical = Path(os.path.abspath(Path(data_dir).expanduser()))
    lexical.mkdir(parents=True, exist_ok=True)
    if lexical.resolve(strict=True) != lexical or not lexical.is_dir():
        raise PortabilityActivationError("The Automadin data directory may not be redirected.")
    return lexical


def _profiles_root(profiles_root: str | Path) -> Path:
    """Use the profile subsystem's canonical configured trust boundary."""
    root = managed_profiles_root(profiles_root)
    root.mkdir(parents=True, exist_ok=True)
    return managed_profiles_root(root)


def _database_profiles(path: Path) -> dict[str, str]:
    inspection = inspect_database(path)
    if inspection.schema_version != CURRENT_SCHEMA_VERSION:
        raise PortabilityActivationError("The staged database is not at the current schema.")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    profiles: dict[str, str] = {}
    seen_names: set[str] = set()
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            if hasattr(connection, "setlimit") and hasattr(
                sqlite3, "SQLITE_LIMIT_LENGTH"
            ):
                connection.setlimit(
                    sqlite3.SQLITE_LIMIT_LENGTH,
                    _MAX_SQLITE_VALUE_BYTES,
                )
            rows = connection.execute(
                "SELECT "
                "typeof(profile_id), length(profile_id), substr(profile_id, 1, 37), "
                "typeof(name), length(name), substr(name, 1, 201), "
                "typeof(engine), length(engine), substr(engine, 1, 33) "
                "FROM profiles"
            )
            for row in rows:
                if len(profiles) >= MAX_DATABASE_PROFILES:
                    raise PortabilityActivationError(
                        "The staged database contains too many browser profiles."
                    )
                if (
                    len(row) != 9
                    or row[0] != "text"
                    or type(row[1]) is not int
                    or not 0 < row[1] <= 36
                    or row[3] != "text"
                    or type(row[4]) is not int
                    or not 0 < row[4] <= _MAX_PROFILE_DISPLAY_NAME_CHARS
                    or row[6] != "text"
                    or type(row[7]) is not int
                    or not 0 <= row[7] <= _MAX_PROFILE_ENGINE_CHARS
                ):
                    raise PortabilityActivationError(
                        "A staged database profile has an unsafe text type or length."
                    )
                try:
                    profile_id = canonical_profile_id(row[2])
                except (TypeError, ValueError) as exc:
                    raise PortabilityActivationError(
                        "A staged database profile ID is not a canonical UUID."
                    ) from exc
                name = row[5]
                engine = row[8]
                if (
                    not isinstance(name, str)
                    or name != name.strip()
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in name
                    )
                ):
                    raise PortabilityActivationError(
                        "A staged database profile has an invalid display name."
                    )
                if (
                    not isinstance(engine, str)
                    or (engine and engine not in SUPPORTED_PROFILE_ENGINES)
                ):
                    raise PortabilityActivationError(
                        "A staged database profile has an unsupported engine."
                    )
                folded_name = name.casefold()
                if profile_id in profiles or folded_name in seen_names:
                    raise PortabilityActivationError(
                        "The staged database repeats a profile UUID or display name."
                    )
                profiles[profile_id] = engine
                seen_names.add(folded_name)
    except sqlite3.Error as exc:
        raise PortabilityActivationError(
            f"The staged database profile rows could not be read safely: {exc}"
        ) from exc
    return profiles


def _write_journal(path: Path, document: dict[str, object], *, create: bool = False) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            if os.path.lexists(path):
                raise FileExistsError(f"A portability activation is already pending at {path}.")
            if os.name == "nt":
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
                temporary.unlink()
        else:
            os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            pass
        else:
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise PortabilityActivationError(
                f"Activation journal repeats field {key!r}."
            )
        value[key] = item
    return value


def _read_journal(path: Path) -> dict[str, object]:
    try:
        lexical = Path(os.path.abspath(path))
        metadata = os.lstat(path)
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if (
            path.resolve(strict=True) != lexical
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (reparse and int(getattr(metadata, "st_file_attributes", 0)) & reparse)
            or metadata.st_size > 1024 * 1024
        ):
            raise PortabilityActivationError(
                "Activation journal is redirected, oversized, or not a regular file."
            )
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortabilityActivationError(f"Activation journal is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PortabilityActivationError("Activation journal root is not an object.")
    required = {
        "format",
        "version",
        "plan_id",
        "phase",
        "database",
        "profiles",
        "kept_local",
        "reviewed_absent",
        "published",
        "error",
        "rejected_path",
        "database_activated",
    }
    if set(value) != required:
        raise PortabilityActivationError("Activation journal has an invalid schema.")
    if value["format"] != ACTIVATION_FORMAT or value["version"] != ACTIVATION_VERSION:
        raise PortabilityActivationError("Activation journal version is unsupported.")
    try:
        if str(uuid.UUID(str(value["plan_id"]))) != value["plan_id"]:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortabilityActivationError("Activation journal plan ID is invalid.") from exc
    if value["phase"] not in {
        "ready",
        "profiles_published",
        "database_pending",
        "failed",
    }:
        raise PortabilityActivationError("Activation journal phase is invalid.")
    if (
        not isinstance(value["profiles"], list)
        or not isinstance(value["kept_local"], list)
        or not isinstance(value["reviewed_absent"], list)
    ):
        raise PortabilityActivationError("Activation journal profile lists are invalid.")
    if not isinstance(value["published"], list):
        raise PortabilityActivationError("Activation journal publication state is invalid.")
    if (
        any(not isinstance(item, str) for item in value["published"])
        or len(set(value["published"])) != len(value["published"])
        or not isinstance(value["error"], str)
        or not isinstance(value["rejected_path"], str)
        or not isinstance(value["database_activated"], bool)
    ):
        raise PortabilityActivationError("Activation journal scalar state is invalid.")
    return value


def stage_portability_activation(
    data_dir: str | Path,
    staged_database: str | Path,
    staged_profiles: tuple[StagedProfileInput, ...],
    kept_local_profiles: tuple[KeptLocalProfile, ...] = (),
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_leases: Mapping[str, MaintenanceProfileLease],
    profiles_root: str | Path | None = None,
) -> PortabilityActivationPlan:
    """Validate all inputs, then atomically publish one durable pending plan."""
    root = _safe_data_dir(data_dir)
    profile_root = _profiles_root(profiles_root or root / "profiles")
    journal_path = root / ACTIVATION_PLAN_NAME
    if os.path.lexists(journal_path):
        raise FileExistsError(f"A portability activation is already pending at {journal_path}.")
    if os.path.lexists(root / PENDING_IMPORT_NAME):
        raise FileExistsError(
            f"A database import is already pending at {root / PENDING_IMPORT_NAME}."
        )

    staged_db = Path(staged_database).resolve(strict=True)
    database_profiles = _database_profiles(staged_db)
    imported_ids = {item.profile_id for item in staged_profiles}
    kept_ids = {item.profile_id for item in kept_local_profiles}
    if len(imported_ids) != len(staged_profiles) or len(kept_ids) != len(kept_local_profiles):
        raise PortabilityActivationError("Activation profile decisions contain duplicates.")
    if imported_ids & kept_ids:
        raise PortabilityActivationError("A profile cannot be both imported and kept local.")

    publications: list[StagedProfilePublication] = []
    for item in sorted(staged_profiles, key=lambda value: value.profile_id):
        if database_profiles.get(item.profile_id) != item.engine:
            raise PortabilityActivationError(
                f"Staged profile {item.profile_id} does not match its database row and engine."
            )
        publications.append(
            prepare_staged_profile_publication(
                item.path,
                item.profile_id,
                item.engine,
                lease_registry=lease_registry,
                maintenance_lease=maintenance_leases[item.profile_id],
                profiles_root=profile_root,
            )
        )
    for item in sorted(kept_local_profiles, key=lambda value: value.profile_id):
        if database_profiles.get(item.profile_id) != item.engine:
            raise PortabilityActivationError(
                f"Kept-local profile {item.profile_id} does not match its database row and engine."
            )
        inspection = inspect_profile_storage(
            item.profile_id,
            item.engine,
            profiles_root=profile_root,
        )
        if inspection.status is not ProfileStorageStatus.READY:
            raise PortabilityActivationError(
                f"Kept-local profile {item.profile_id} is not ready: {inspection.detail}"
            )
        current_fingerprint = profile_tree_lstat_fingerprint(inspection.path)
        if (
            item.reviewed_fingerprint is not None
            and current_fingerprint != item.reviewed_fingerprint
        ):
            raise PortabilityActivationError(
                f"Kept-local profile {item.profile_id} changed after collision review."
            )
    for profile_id in sorted(database_profiles):
        if (
            profile_id not in imported_ids
            and profile_id not in kept_ids
            and os.path.lexists(profile_root / profile_id)
        ):
            raise PortabilityActivationError(
                f"Local profile {profile_id} requires an explicit kept-local decision."
            )

    plan_id = str(uuid.uuid4())
    private_db = root / PRIVATE_DATABASE_NAME
    if os.path.lexists(private_db):
        raise FileExistsError(
            f"An interrupted portability database is awaiting recovery at {private_db}."
        )
    stage_database_copy(staged_db, private_db)
    document: dict[str, object] = {
        "format": ACTIVATION_FORMAT,
        "version": ACTIVATION_VERSION,
        "plan_id": plan_id,
        "phase": "ready",
        "database": {"path": private_db.name, "sha256": _sha256(private_db)},
        "profiles": [
            {
                "profile_id": item.profile_id,
                "engine": item.engine,
                "staging_name": item.staging_path.name,
                "fingerprint": item.fingerprint,
            }
            for item in publications
        ],
        "kept_local": [
            {
                "profile_id": item.profile_id,
                "engine": item.engine,
                "reviewed_fingerprint": item.reviewed_fingerprint or "",
            }
            for item in sorted(kept_local_profiles, key=lambda value: value.profile_id)
        ],
        "reviewed_absent": sorted(
            set(database_profiles) - imported_ids - kept_ids
        ),
        "published": [],
        "error": "",
        "rejected_path": "",
        "database_activated": False,
    }
    try:
        _write_journal(journal_path, document, create=True)
    except Exception:
        private_db.unlink(missing_ok=True)
        raise
    return PortabilityActivationPlan(
        plan_id,
        journal_path,
        private_db,
        tuple(item.profile_id for item in publications),
        tuple(sorted(kept_ids)),
    )


def _journal_profile_ids(document: dict[str, object]) -> tuple[str, ...]:
    profile_ids: list[str] = []
    for raw in document["profiles"]:  # type: ignore[union-attr]
        if not isinstance(raw, dict) or set(raw) != {
            "profile_id",
            "engine",
            "staging_name",
            "fingerprint",
        }:
            raise PortabilityActivationError("Activation profile entry is invalid.")
        profile_ids.append(canonical_profile_id(str(raw["profile_id"])))
    return tuple(profile_ids)


def _journal_publications(
    document: dict[str, object],
    profile_root: Path,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_scope: MaintenanceProfileScope,
) -> list[StagedProfilePublication]:
    publications: list[StagedProfilePublication] = []
    for raw in document["profiles"]:  # type: ignore[union-attr]
        if not isinstance(raw, dict) or set(raw) != {
            "profile_id",
            "engine",
            "staging_name",
            "fingerprint",
        }:
            raise PortabilityActivationError("Activation profile entry is invalid.")
        profile_id = canonical_profile_id(str(raw["profile_id"]))
        engine = str(raw["engine"])
        staging = profile_root / str(raw["staging_name"])
        final = profile_root / profile_id
        publications.append(
            StagedProfilePublication(
                profile_id,
                engine,
                staging,
                final,
                str(raw["fingerprint"]),
                profile_root,
                lease_registry,
                maintenance_scope.for_profile(profile_id),
            )
        )
    return publications


def _validate_kept_profiles(document: dict[str, object], profile_root: Path) -> None:
    for raw in document["kept_local"]:  # type: ignore[union-attr]
        if not isinstance(raw, dict) or set(raw) != {
            "profile_id",
            "engine",
            "reviewed_fingerprint",
        }:
            raise PortabilityActivationError("Kept-local profile entry is invalid.")
        inspection = inspect_profile_storage(
            str(raw["profile_id"]),
            str(raw["engine"]),
            profiles_root=profile_root,
        )
        if inspection.status is not ProfileStorageStatus.READY:
            raise PortabilityActivationError(
                f"Kept-local profile {raw['profile_id']} changed before activation."
            )
        current_fingerprint = profile_tree_lstat_fingerprint(inspection.path)
        reviewed_fingerprint = str(raw["reviewed_fingerprint"])
        if reviewed_fingerprint and current_fingerprint != reviewed_fingerprint:
            raise PortabilityActivationError(
                f"Kept-local profile {raw['profile_id']} changed after collision review."
            )


def _validate_absent_profiles(document: dict[str, object], profile_root: Path) -> None:
    reviewed = document["reviewed_absent"]
    if not isinstance(reviewed, list) or len(set(map(str, reviewed))) != len(reviewed):
        raise PortabilityActivationError("Reviewed-absent profile state is invalid.")
    for raw_profile_id in reviewed:
        profile_id = canonical_profile_id(raw_profile_id)
        path = resolve_profile_storage_path(profile_id, profile_root)
        if os.path.lexists(path):
            raise PortabilityActivationError(
                f"Profile {profile_id} appeared after absence review; activation was blocked."
            )


def _move_database_no_replace(source: Path, destination: Path) -> None:
    if os.path.lexists(destination):
        raise PortabilityActivationError(
            f"Another database import is already pending at {destination}."
        )
    try:
        if os.name == "nt":
            os.rename(source, destination)
        else:
            os.link(source, destination)
            source.unlink()
    except OSError as exc:
        if os.path.lexists(destination):
            raise PortabilityActivationError(
                f"Another database import is already pending at {destination}."
            ) from exc
        raise


def _rollback_publications(
    publications: list[StagedProfilePublication],
) -> tuple[Path, ...]:
    recovery: list[Path] = []
    errors: list[str] = []
    for publication in reversed(publications):
        try:
            publication.rollback()
            recovery.append(publication.staging_path)
        except Exception as exc:
            errors.append(f"{publication.profile_id}: {exc}")
            if os.path.lexists(publication.staging_path):
                recovery.append(publication.staging_path)
            if os.path.lexists(publication.final_path):
                recovery.append(publication.final_path)
    if errors:
        raise PortabilityActivationError(
            "Profile rollback was incomplete; recovery data was preserved. "
            + " | ".join(errors)
        )
    return tuple(recovery)


def _apply_pending_portability_activation(
    data_dir: str | Path,
    *,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_scope: MaintenanceProfileScope,
    profiles_root: str | Path | None = None,
) -> PortabilityActivationResult | None:
    """Idempotently apply one journal before the active DatabaseService is opened."""
    root = _safe_data_dir(data_dir)
    profile_root = _profiles_root(profiles_root or root / "profiles")
    journal_path = root / ACTIVATION_PLAN_NAME
    if not os.path.lexists(journal_path):
        return None
    document = _read_journal(journal_path)
    if document["phase"] == "failed":
        failed_plan = _quarantine_path(journal_path, "automadin-portability-rejected", ".json")
        return PortabilityActivationResult(
            False,
            error=str(document["error"]),
            rejected_path=(
                Path(str(document["rejected_path"]))
                if document["rejected_path"]
                else None
            ),
            failed_plan_path=failed_plan,
            database_activated=bool(document["database_activated"]),
        )

    database = document["database"]
    if not isinstance(database, dict) or set(database) != {"path", "sha256"}:
        raise PortabilityActivationError("Activation database entry is invalid.")
    private_db = root / str(database["path"])
    if (
        Path(str(database["path"])).name != str(database["path"])
        or not private_db.name.startswith(".automadin-portability-")
        or private_db.suffix != ".db"
        or len(str(database["sha256"])) != 64
        or any(character not in "0123456789abcdef" for character in str(database["sha256"]))
    ):
        raise PortabilityActivationError("Activation private database path or digest is invalid.")
    pending_db = root / PENDING_IMPORT_NAME
    active_db = root / "automadin.db"
    expected_db_hash = str(database["sha256"])
    publications = _journal_publications(
        document,
        profile_root,
        lease_registry,
        maintenance_scope,
    )
    publication_ids = tuple(publication.profile_id for publication in publications)
    if (
        len(set(publication_ids)) != len(publication_ids)
        or tuple(sorted(publication_ids)) != maintenance_scope.profile_ids
    ):
        raise PortabilityActivationError(
            "Activation maintenance ownership does not match its profile plan."
        )
    _validate_kept_profiles(document, profile_root)
    _validate_absent_profiles(document, profile_root)

    # Detect a completed DB swap that occurred immediately before a crash.
    if (
        not os.path.lexists(private_db)
        and not os.path.lexists(pending_db)
        and active_db.is_file()
    ):
        if _sha256(active_db) != expected_db_hash:
            raise PortabilityActivationError(
                "Activation database state is ambiguous; recovery paths were preserved."
            )
        if any(publication.location() != "final" for publication in publications):
            raise PortabilityActivationError(
                "The imported database is active but an imported profile is not "
                "in its verified final location."
            )
        journal_path.unlink()
        return PortabilityActivationResult(True, database_activated=True)

    for publication in publications:
        publication.publish()
        published = document["published"]
        if publication.profile_id not in published:  # type: ignore[operator]
            published.append(publication.profile_id)  # type: ignore[union-attr]
            document["phase"] = "profiles_published"
            _write_journal(journal_path, document)

    if private_db.is_file() and not os.path.lexists(pending_db):
        if _sha256(private_db) != expected_db_hash:
            raise PortabilityActivationError("Private staged database changed before activation.")
        _move_database_no_replace(private_db, pending_db)
        document["phase"] = "database_pending"
        _write_journal(journal_path, document)
    elif not private_db.exists() and pending_db.is_file():
        if _sha256(pending_db) != expected_db_hash:
            raise PortabilityActivationError("Pending database does not match the activation plan.")
    else:
        raise PortabilityActivationError(
            "Activation database state is ambiguous; no data was overwritten."
        )

    db_result = apply_pending_database_import(active_db)
    if db_result is not None and db_result.applied:
        if not active_db.is_file() or _sha256(active_db) != expected_db_hash:
            raise PortabilityActivationError(
                "Imported database bytes do not match the activation plan."
            )
        journal_path.unlink()
        return PortabilityActivationResult(
            True,
            backup_path=db_result.backup_path,
            database_activated=True,
        )

    error = str(db_result.error if db_result is not None else "Database import did not run.")
    if active_db.is_file() and _sha256(active_db) == expected_db_hash:
        document["phase"] = "failed"
        document["error"] = (
            f"{error} The imported database appears active, so profile data was preserved."
        )
        document["rejected_path"] = (
            str(db_result.rejected_path) if db_result and db_result.rejected_path else ""
        )
        document["database_activated"] = True
        _write_journal(journal_path, document)
        raise PortabilityActivationError(str(document["error"]))
    recovery_paths = _rollback_publications(publications)
    owned_pending = _quarantine_owned_database(
        pending_db,
        root,
        expected_db_hash,
    )
    if owned_pending is None and os.path.lexists(pending_db):
        owned_pending = _quarantine_ambiguous_pending(pending_db, root)
    if owned_pending is not None:
        recovery_paths = (*recovery_paths, owned_pending)
    document["phase"] = "failed"
    document["error"] = error
    document["rejected_path"] = (
        str(db_result.rejected_path) if db_result and db_result.rejected_path else ""
    )
    document["published"] = []
    document["database_activated"] = False
    _write_journal(journal_path, document)
    failed_plan = _quarantine_path(
        journal_path,
        "automadin-portability-rejected",
        ".json",
    )
    return PortabilityActivationResult(
        False,
        backup_path=db_result.backup_path if db_result else None,
        rejected_path=owned_pending or (db_result.rejected_path if db_result else None),
        error=error,
        recovery_paths=recovery_paths,
        failed_plan_path=failed_plan,
        database_activated=False,
    )


def _quarantine_path(path: Path, prefix: str, suffix: str) -> Path:
    destination = path.with_name(f"{prefix}-{uuid.uuid4().hex}{suffix}")
    os.rename(path, destination)
    return destination


def _quarantine_owned_database(
    path: Path,
    root: Path,
    expected_hash: str,
) -> Path | None:
    """Quarantine only a real direct file whose bytes belong to this plan."""
    if not os.path.lexists(path):
        return None
    if not _safe_direct_regular_file(path, root) or _sha256(path) != expected_hash:
        return None
    return _quarantine_path(path, "automadin-portability-rejected", ".db")


def _quarantine_ambiguous_pending(path: Path, root: Path) -> Path:
    """Preserve an unresolved standard pending DB without allowing DB-only apply."""
    if not _safe_direct_regular_file(path, root):
        raise PortabilityActivationError(
            "An ambiguous standard pending database could not be quarantined safely."
        )
    return _quarantine_path(path, "automadin-portability-ambiguous", ".db")


def _quarantine_ambiguous_private(path: Path, root: Path) -> Path:
    """Preserve a safe but non-matching private DB during first-pass recovery."""
    if not _safe_direct_regular_file(path, root):
        raise PortabilityActivationError(
            "An ambiguous private portability database could not be quarantined safely."
        )
    return _quarantine_path(path, "automadin-portability-ambiguous", ".db")


def _recoverable_publications(
    journal_path: Path,
    profile_root: Path,
    lease_registry: BrowserProfileLeaseRegistry,
    maintenance_scope: MaintenanceProfileScope | None,
) -> list[StagedProfilePublication]:
    if maintenance_scope is None:
        return []
    try:
        return _journal_publications(
            _read_journal(journal_path),
            profile_root,
            lease_registry,
            maintenance_scope,
        )
    except Exception:
        return []


def _safe_direct_regular_file(path: Path, root: Path) -> bool:
    lexical = Path(os.path.abspath(path))
    try:
        metadata = os.lstat(path)
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        return bool(
            lexical.parent == root
            and path.resolve(strict=True) == lexical
            and stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_nlink == 1
            and not (
                reparse
                and int(getattr(metadata, "st_file_attributes", 0)) & reparse
            )
        )
    except (OSError, RuntimeError):
        return False


def _remnant_snapshot(paths: list[Path]) -> str:
    parts: list[tuple[object, ...]] = []
    for path in sorted(paths, key=lambda item: item.name):
        try:
            metadata = os.lstat(path)
            parts.append(
                (
                    path.name,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_nlink,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
            )
        except OSError as exc:
            parts.append((path.name, type(exc).__name__, str(exc)))
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def _staging_remnant_notice(root: Path) -> PortabilityActivationResult | None:
    """Quarantine exact crash remnants once and report unsafe lookalikes once."""
    marker = root / STAGING_REMNANT_REPORTED_NAME
    try:
        with os.scandir(root) as entries:
            candidates = sorted(
                [
                    Path(entry.path)
                    for entry in entries
                    if entry.name.startswith(
                        (
                            ".automadin-portability-staging-",
                            ".automadin-portability-pending.db.",
                            ".automadin-import-pending.db.",
                            ".automadin.db.restore-",
                        )
                    )
                ],
                key=lambda path: path.name,
            )
    except OSError as exc:
        return PortabilityActivationResult(
            False,
            error=f"Portability staging remnants could not be inventoried: {exc}",
        )
    marker_exists = os.path.lexists(marker)
    if marker_exists:
        try:
            marker_size = os.lstat(marker).st_size
        except OSError:
            marker_size = -1
        if not _safe_direct_regular_file(marker, root) or not 0 <= marker_size <= 4096:
            return PortabilityActivationResult(
                False,
                error=(
                    "The portability-remnant notice marker is unsafe or oversized "
                    "and was left untouched."
                ),
                unresolved_paths=(marker, *candidates),
            )
    if not candidates:
        if marker_exists:
            try:
                marker.unlink()
            except OSError as exc:
                return PortabilityActivationResult(
                    False,
                    error=f"The portability-remnant notice marker could not be removed: {exc}",
                    unresolved_paths=(marker,),
                )
        return None

    snapshot = _remnant_snapshot(candidates)
    if marker_exists:
        try:
            previous = json.loads(marker.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous = {}
        if isinstance(previous, dict) and previous.get("snapshot") == snapshot:
            return None

    groups: dict[str, list[tuple[Path, str]]] = {}
    unresolved: list[Path] = []
    for path in candidates:
        match = _DATABASE_REMNANT.fullmatch(path.name)
        if match is None:
            unresolved.append(path)
            continue
        groups.setdefault(match.group("base"), []).append(
            (path, match.group("suffix") or "")
        )

    recovered: list[Path] = []
    for members in groups.values():
        if not all(_safe_direct_regular_file(path, root) for path, _suffix in members):
            unresolved.extend(path for path, _suffix in members)
            continue
        base_name = members[0][0].name
        prefix = (
            "automadin-portability-orphaned"
            if base_name.startswith(".automadin-portability-")
            else "automadin-database-orphaned"
        )
        destination_base = root / f"{prefix}-{uuid.uuid4().hex}.db"
        for path, suffix in sorted(members, key=lambda item: item[1]):
            destination = destination_base.with_name(destination_base.name + suffix)
            try:
                os.rename(path, destination)
            except OSError:
                unresolved.append(path)
            else:
                recovered.append(destination)

    remaining_snapshot = _remnant_snapshot(
        [path for path in unresolved if os.path.lexists(path)]
    )
    marker_error = ""
    try:
        _write_journal(
            marker,
            {"snapshot": remaining_snapshot},
        )
    except OSError as exc:
        marker_error = f" Notice state could not be saved: {exc}"
        unresolved.append(marker)
    detail_parts: list[str] = []
    if recovered:
        detail_parts.append(
            f"Recovered {len(recovered)} interrupted portability staging file(s)."
        )
    if unresolved:
        detail_parts.append(
            f"Left {len(unresolved)} unsafe or malformed lookalike(s) untouched."
        )
    return PortabilityActivationResult(
        False,
        rejected_path=recovered[0] if recovered else None,
        error=" ".join(detail_parts) + marker_error,
        recovery_paths=tuple(recovered),
        unresolved_paths=tuple(dict.fromkeys(unresolved)),
    )


def apply_pending_portability_activation(
    data_dir: str | Path,
    *,
    profiles_root: str | Path | None = None,
    lease_registry: BrowserProfileLeaseRegistry = browser_profile_leases,
) -> PortabilityActivationResult | None:
    """Apply safely; quarantine a bad plan so startup is never permanently blocked."""
    root = _safe_data_dir(data_dir)
    profile_root = _profiles_root(profiles_root or root / "profiles")
    journal_path = root / ACTIVATION_PLAN_NAME
    orphan_database = root / PRIVATE_DATABASE_NAME
    if not os.path.lexists(journal_path):
        if os.path.lexists(orphan_database):
            if not _safe_direct_regular_file(orphan_database, root):
                return PortabilityActivationResult(
                    False,
                    error=(
                        "The fixed portability database path is unsafe and was "
                        "left untouched for recovery."
                    ),
                    unresolved_paths=(orphan_database,),
                )
            rejected = _quarantine_path(
                orphan_database,
                "automadin-portability-orphaned",
                ".db",
            )
            return PortabilityActivationResult(
                False,
                error="An interrupted portability staging database was quarantined.",
                rejected_path=rejected,
                recovery_paths=(rejected,),
            )
        if os.path.lexists(root / PENDING_IMPORT_NAME):
            return None
        remnant_result = _staging_remnant_notice(root)
        if remnant_result is not None:
            return remnant_result
        return None

    maintenance_scope: MaintenanceProfileScope | None = None
    activation_result: PortabilityActivationResult | None = None
    try:
        document = _read_journal(journal_path)
        planned_profile_ids = _journal_profile_ids(document)
        try:
            maintenance_scope = lease_registry.acquire_maintenance_many(
                planned_profile_ids,
                ProfileUse(
                    "maintenance",
                    "activating a portable browser-profile restore",
                    str(uuid.uuid4()),
                ),
            )
        except (BrowserProfileInUseError, BrowserProfileLeaseCleanupError) as exc:
            return _maintenance_acquisition_blocked_result(
                root,
                journal_path,
                exc,
                exc.scope if isinstance(exc, BrowserProfileLeaseCleanupError) else None,
            )
        activation_result = _apply_pending_portability_activation(
            root,
            lease_registry=lease_registry,
            maintenance_scope=maintenance_scope,
            profiles_root=profile_root,
        )
    except Exception as exc:
        publications = _recoverable_publications(
            journal_path,
            profile_root,
            lease_registry,
            maintenance_scope,
        )
        recovery: list[Path] = []
        rollback_errors: list[str] = []
        active_matches_import = False
        expected_database_hash = ""
        try:
            document = _read_journal(journal_path)
            database = document.get("database")
            if isinstance(database, dict):
                expected = str(database.get("sha256", ""))
                expected_database_hash = expected
                active = root / "automadin.db"
                active_matches_import = active.is_file() and _sha256(active) == expected
        except Exception:
            pass
        if not active_matches_import:
            for publication in reversed(publications):
                try:
                    location = publication.location()
                    if location == "final":
                        if maintenance_scope is None:
                            raise PortabilityActivationError(
                                "Profile rollback requires maintenance ownership."
                            )
                        publication.rollback()
                    recovery.append(publication.staging_path)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{publication.profile_id}: {rollback_exc}")
                    for candidate in (publication.staging_path, publication.final_path):
                        if os.path.lexists(candidate):
                            recovery.append(candidate)
        failed_plan: Path | None = None
        rejected_database: Path | None = None
        unresolved: list[Path] = []
        pending_database = root / PENDING_IMPORT_NAME
        fixed_databases = (pending_database, orphan_database)
        if expected_database_hash:
            for candidate in fixed_databases:
                try:
                    rejected = _quarantine_owned_database(
                        candidate,
                        root,
                        expected_database_hash,
                    )
                    if rejected is not None:
                        recovery.append(rejected)
                        if rejected_database is None:
                            rejected_database = rejected
                except Exception as quarantine_exc:
                    rollback_errors.append(
                        f"owned database quarantine: {quarantine_exc}"
                    )
        for candidate in fixed_databases:
            if not os.path.lexists(candidate):
                continue
            try:
                ambiguous = (
                    _quarantine_ambiguous_pending(candidate, root)
                    if candidate == pending_database
                    else _quarantine_ambiguous_private(candidate, root)
                )
                recovery.append(ambiguous)
                if rejected_database is None:
                    rejected_database = ambiguous
            except Exception as quarantine_exc:
                rollback_errors.append(
                    f"ambiguous database quarantine: {quarantine_exc}"
                )
                if os.path.lexists(candidate):
                    unresolved.append(candidate)
        database_quarantine_failed = any(
            os.path.lexists(candidate) for candidate in fixed_databases
        )
        # Keep the journal in place if its matching database could not be moved.
        # That retained ownership prevents ordinary DB-only startup from adopting it.
        if os.path.lexists(journal_path):
            if database_quarantine_failed:
                unresolved.append(journal_path)
            elif not _safe_direct_regular_file(journal_path, root):
                unresolved.append(journal_path)
            else:
                try:
                    failed_plan = _quarantine_path(
                        journal_path,
                        "automadin-portability-rejected",
                        ".json",
                    )
                    recovery.append(failed_plan)
                except OSError as quarantine_exc:
                    rollback_errors.append(f"journal quarantine: {quarantine_exc}")
                    if os.path.lexists(journal_path):
                        unresolved.append(journal_path)
        detail = str(exc)
        if rollback_errors:
            detail += " Recovery was incomplete: " + " | ".join(rollback_errors)
        activation_result = PortabilityActivationResult(
            False,
            rejected_path=rejected_database,
            error=detail,
            recovery_paths=tuple(dict.fromkeys(recovery)),
            failed_plan_path=failed_plan,
            database_activated=active_matches_import,
            unresolved_paths=tuple(dict.fromkeys(unresolved)),
        )
    if maintenance_scope is not None:
        try:
            if not maintenance_scope.release():
                raise RuntimeError("The maintenance scope was already released.")
        except Exception as release_exc:
            activation_result = _with_maintenance_release_error(
                activation_result,
                release_exc,
            )
    return activation_result
