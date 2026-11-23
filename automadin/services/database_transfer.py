"""Safe, restart-based transfer of Automadin SQLite databases."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from automadin.services.database import (
    CURRENT_SCHEMA_TABLES,
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    DatabaseService,
    UnsupportedDatabaseVersionError,
    schema_tables_for_version,
)


PENDING_IMPORT_NAME = ".automadin-import-pending.db"
PORTABILITY_ACTIVATION_PLAN_NAME = ".automadin-portability-pending.json"
PORTABILITY_PRIVATE_DATABASE_NAME = ".automadin-portability-pending.db"
REQUIRED_TABLES = CURRENT_SCHEMA_TABLES
MAX_SCHEMA_TABLES = 1024


@dataclass(frozen=True)
class DatabaseInspection:
    path: Path
    schema_version: int
    table_names: frozenset[str]
    flow_count: int
    image_count: int
    profile_count: int


@dataclass(frozen=True)
class DatabaseImportResult:
    applied: bool
    backup_path: Path | None = None
    error: str | None = None
    rejected_path: Path | None = None
    active_database_matches_import: bool = False


class DatabaseInspectionCancelledError(RuntimeError):
    """Raised when a caller cooperatively cancels database inspection."""


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)


def _direct_private_file_identity(path: Path, parent: Path) -> tuple[int, int]:
    lexical = Path(os.path.abspath(path))
    try:
        metadata = os.lstat(path)
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if (
            lexical.parent != parent
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or (
                reparse
                and int(getattr(metadata, "st_file_attributes", 0)) & reparse
            )
            or path.resolve(strict=True) != lexical
        ):
            raise ValueError
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("The database file is not a real private sibling file.") from exc
    return metadata.st_dev, metadata.st_ino


def _remove_private_temp(path: Path) -> None:
    """Remove only direct private temp inodes; leave raced-in artifacts untouched."""
    parent = Path(os.path.abspath(path.parent))
    for candidate in (
        path,
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
        path.with_name(path.name + "-journal"),
    ):
        if not os.path.lexists(candidate):
            continue
        try:
            _direct_private_file_identity(candidate, parent)
        except ValueError:
            continue
        candidate.unlink()


def inspect_database(
    path: str | Path,
    *,
    cancel: Callable[[], bool] | None = None,
) -> DatabaseInspection:
    """Validate an Automadin database without modifying it."""
    if cancel is not None and cancel():
        raise DatabaseInspectionCancelledError("Database inspection was cancelled.")
    database_path = Path(path).resolve()
    if not database_path.is_file():
        raise ValueError(f"Database file does not exist: {database_path}")
    if database_path.stat().st_size == 0:
        raise ValueError("The selected database is empty.")

    cancelled = False
    try:
        with closing(_read_only_connection(database_path)) as connection:
            def progress() -> int:
                nonlocal cancelled
                cancelled = cancel is not None and cancel()
                return 1 if cancelled else 0

            if cancel is not None:
                connection.set_progress_handler(progress, 1000)
            check_rows = connection.execute("PRAGMA quick_check").fetchall()
            if cancel is not None and cancel():
                raise DatabaseInspectionCancelledError(
                    "Database inspection was cancelled."
                )
            if not check_rows or any(str(row[0]).lower() != "ok" for row in check_rows):
                raise ValueError("SQLite integrity check failed.")
            table_name_values: set[str] = set()
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ):
                if len(table_name_values) >= MAX_SCHEMA_TABLES:
                    raise ValueError(
                        "The selected database contains too many schema tables."
                    )
                table_name_values.add(str(row[0]))
            table_names = frozenset(table_name_values)
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if cancel is not None and cancel():
                raise DatabaseInspectionCancelledError(
                    "Database inspection was cancelled."
                )
            if schema_version > CURRENT_SCHEMA_VERSION:
                raise UnsupportedDatabaseVersionError(
                    f"Database schema version {schema_version} is newer than this "
                    f"Automadin build (supports through version {CURRENT_SCHEMA_VERSION})."
                )
            if schema_version < MIN_SUPPORTED_SCHEMA_VERSION:
                raise UnsupportedDatabaseVersionError(
                    f"Database schema version {schema_version} is no longer supported."
                )
            required_tables = schema_tables_for_version(schema_version)
            missing = sorted(required_tables - table_names)
            if missing:
                raise ValueError(
                    "The selected file is not a compatible Automadin database. "
                    f"Missing tables: {', '.join(missing)}"
                )
            flow_count = int(connection.execute("SELECT COUNT(*) FROM flows").fetchone()[0])
            if cancel is not None and cancel():
                raise DatabaseInspectionCancelledError(
                    "Database inspection was cancelled."
                )
            image_count = int(connection.execute("SELECT COUNT(*) FROM images").fetchone()[0])
            profile_count = int(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0])
    except sqlite3.Error as exc:
        if cancel is not None and cancelled:
            raise DatabaseInspectionCancelledError(
                "Database inspection was cancelled."
            ) from exc
        raise ValueError(f"Could not read the selected SQLite database: {exc}") from exc

    return DatabaseInspection(
        path=database_path,
        schema_version=schema_version,
        table_names=table_names,
        flow_count=flow_count,
        image_count=image_count,
        profile_count=profile_count,
    )


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination = Path(os.path.abspath(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = Path(os.path.abspath(destination.parent))
    if destination.parent.resolve(strict=True) != parent:
        raise ValueError("The database backup directory may not be redirected.")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(destination, flags, 0o600)
    identity: tuple[int, int] | None = None
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        destination_uri = f"{destination.as_uri()}?mode=rw&nofollow=1"
        with closing(_read_only_connection(source)) as source_connection:
            with closing(
                sqlite3.connect(destination_uri, uri=True, timeout=30.0)
            ) as destination_connection:
                if _direct_private_file_identity(destination, parent) != identity:
                    raise ValueError(
                        "The database backup destination changed before it was opened."
                    )
                source_connection.backup(destination_connection)
                destination_connection.commit()
        if _direct_private_file_identity(destination, parent) != identity:
            raise ValueError("The database backup destination changed during backup.")
    except Exception:
        try:
            if (
                identity is not None
                and _direct_private_file_identity(destination, parent) == identity
            ):
                destination.unlink()
        except (OSError, ValueError):
            pass
        raise
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pending_database_identity(path: Path, root: Path) -> tuple[int, int]:
    lexical = Path(os.path.abspath(path))
    try:
        metadata = os.lstat(path)
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        safe = (
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
    except (OSError, RuntimeError) as exc:
        raise ValueError("The pending database path could not be validated safely.") from exc
    if not safe:
        raise ValueError(
            "The pending database must be a real, private, direct file in "
            "Automadin's data directory."
        )
    return metadata.st_dev, metadata.st_ino


def _lexical_active_database(path: str | Path) -> Path:
    """Validate the lexical active DB boundary before deriving sibling paths."""
    active = Path(os.path.abspath(Path(path).expanduser()))
    active.parent.mkdir(parents=True, exist_ok=True)
    parent = Path(os.path.abspath(active.parent))
    try:
        if active.parent.resolve(strict=True) != parent or not parent.is_dir():
            raise ValueError
        if os.path.lexists(active):
            metadata = os.lstat(active)
            reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            if (
                active.resolve(strict=True) != active
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
                or (
                    reparse
                    and int(getattr(metadata, "st_file_attributes", 0)) & reparse
                )
            ):
                raise ValueError
        elif active.resolve(strict=False) != active:
            raise ValueError
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            "The active Automadin database must be a real private file in its "
            "lexical data directory."
        ) from exc
    return active


def _migrate_staged_database(path: Path) -> None:
    """Migrate a writable staging copy without enabling runtime WAL mode."""
    database = DatabaseService(str(path), enable_wal=False)
    try:
        if database.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"Migration stopped at schema version {database.schema_version}; "
                f"expected {CURRENT_SCHEMA_VERSION}."
            )
    finally:
        database.close()


def _publish_staged_database(
    temporary: Path,
    destination: Path,
    *,
    replace_existing: bool,
) -> None:
    """Publish one sibling staging file without a check-then-rename race."""
    if replace_existing:
        os.replace(temporary, destination)
        return

    if os.name == "nt":
        if os.path.lexists(destination):
            raise FileExistsError(
                f"A staged database already exists at {destination}. "
                "Apply or remove it before staging another import."
            )
        try:
            # Windows rename is atomic and refuses an existing destination.  It
            # also works on FAT/exFAT portable data directories without hard links.
            os.rename(temporary, destination)
        except OSError as exc:
            if os.path.lexists(destination):
                raise FileExistsError(
                    f"A staged database already exists at {destination}. "
                    "Apply or remove it before staging another import."
                ) from exc
            raise
        return

    try:
        # A same-directory hard link publishes the fully written inode atomically
        # and fails if another operation already owns the destination name.
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise FileExistsError(
            f"A staged database already exists at {destination}. "
            "Apply or remove it before staging another import."
        ) from exc
    else:
        temporary.unlink()


def stage_database_copy(
    source_path: str | Path,
    staged_path: str | Path,
    *,
    replace_existing: bool = False,
) -> DatabaseInspection:
    """Validate, migrate, and atomically publish a database staging copy.

    The selected source is opened read-only and is never migrated in place.
    Existing staged data is preserved unless ``replace_existing`` is explicitly
    requested by a caller that owns the destination.
    """
    source = Path(source_path).resolve()
    destination = Path(os.path.abspath(Path(staged_path).expanduser()))
    if source == destination or (
        os.path.lexists(destination)
        and destination.resolve(strict=True) == source
    ):
        raise ValueError("The selected file is already the database staging destination.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    lexical_parent = Path(os.path.abspath(destination.parent))
    if destination.parent.resolve(strict=True) != lexical_parent:
        raise ValueError("The database staging directory may not be redirected.")

    inspection = inspect_database(source)
    temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _sqlite_backup(source, temporary)
        _migrate_staged_database(temporary)
        migrated = inspect_database(temporary)
        if migrated.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError("The staged database did not reach the current schema version.")
        _publish_staged_database(
            temporary,
            destination,
            replace_existing=replace_existing,
        )
    finally:
        _remove_private_temp(temporary)
    return inspection


def stage_database_import(source_path: str | Path, active_db_path: str | Path) -> DatabaseInspection:
    """Copy a consistent validated backup beside the active DB for next startup."""
    active = _lexical_active_database(active_db_path)
    source = Path(source_path).resolve()
    if source == active:
        raise ValueError("The selected file is already the active Automadin database.")
    portability_paths = (
        active.parent / PORTABILITY_ACTIVATION_PLAN_NAME,
        active.parent / PORTABILITY_PRIVATE_DATABASE_NAME,
    )
    if any(os.path.lexists(path) for path in portability_paths):
        raise FileExistsError(
            "A portability-bundle activation is already pending. Apply or recover "
            "that operation before staging a database-only import."
        )
    return stage_database_copy(
        source,
        active.parent / PENDING_IMPORT_NAME,
    )


def _unique_backup_path(active: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    while True:
        candidate = active.with_name(
            f"automadin-before-import-{stamp}-{uuid.uuid4().hex}.db"
        )
        if not os.path.lexists(candidate):
            return candidate


def _create_safety_backup(active: Path) -> Path:
    """Create, validate, and atomically publish an immutable sibling backup."""
    backup = _unique_backup_path(active)
    temporary = active.with_name(f".{active.name}.restore-{uuid.uuid4().hex}.tmp")
    try:
        _sqlite_backup(active, temporary)
        inspect_database(temporary)
        expected_identity = _direct_private_file_identity(
            temporary,
            Path(os.path.abspath(active.parent)),
        )
        _publish_staged_database(
            temporary,
            backup,
            replace_existing=False,
        )
        if (
            _direct_private_file_identity(
                backup,
                Path(os.path.abspath(active.parent)),
            )
            != expected_identity
        ):
            raise ValueError("The published safety backup changed identity.")
        return backup
    finally:
        _remove_private_temp(temporary)


def _reject_pending(pending: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    while True:
        rejected = pending.with_name(
            f"automadin-import-rejected-{stamp}-{uuid.uuid4().hex}.db"
        )
        if os.path.lexists(rejected):
            continue
        try:
            _publish_staged_database(
                pending,
                rejected,
                replace_existing=False,
            )
        except FileExistsError:
            continue
        return rejected


def _restore_backup_atomically(active: Path, backup: Path) -> None:
    """Validate a sibling restore file before atomically replacing ``active``."""
    temporary = active.with_name(f".{active.name}.restore-{uuid.uuid4().hex}.tmp")
    try:
        _sqlite_backup(backup, temporary)
        inspect_database(temporary)
        os.replace(temporary, active)
    finally:
        _remove_private_temp(temporary)


def _apply_pending_database_import(
    active_db_path: str | Path,
    *,
    allow_portability_owned: bool,
) -> DatabaseImportResult | None:
    """Apply a staged database before DatabaseService opens the active file."""
    try:
        active = _lexical_active_database(active_db_path)
    except ValueError as exc:
        return DatabaseImportResult(applied=False, error=str(exc))
    pending = active.parent / PENDING_IMPORT_NAME
    if not os.path.lexists(pending):
        return None
    try:
        pending_identity = _pending_database_identity(pending, active.parent)
    except ValueError as exc:
        return DatabaseImportResult(applied=False, error=str(exc))
    if not allow_portability_owned and any(
        os.path.lexists(active.parent / name)
        for name in (
            PORTABILITY_ACTIVATION_PLAN_NAME,
            PORTABILITY_PRIVATE_DATABASE_NAME,
        )
    ):
        return DatabaseImportResult(
            applied=False,
            error=(
                "The pending database belongs to a portability-bundle activation "
                "and cannot be applied as a database-only import."
            ),
        )

    try:
        inspect_database(pending)
        _migrate_staged_database(pending)
        if _pending_database_identity(pending, active.parent) != pending_identity:
            raise ValueError("The pending database changed identity during validation.")
        migrated = inspect_database(pending)
        if migrated.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError("The pending database did not reach the current schema version.")
    except Exception as exc:
        try:
            rejected = (
                _reject_pending(pending)
                if _pending_database_identity(pending, active.parent) == pending_identity
                else pending
            )
        except (OSError, ValueError):
            rejected = pending
        return DatabaseImportResult(
            applied=False,
            error=str(exc),
            rejected_path=rejected,
        )

    backup: Path | None = None
    activated = False
    active_disturbed = False
    imported_hash: str | None = None
    try:
        imported_hash = _sha256(pending)
        active.parent.mkdir(parents=True, exist_ok=True)
        if active.is_file():
            backup = _create_safety_backup(active)

        # A database left in WAL mode may have stale sidecars from a prior run.
        # The safety backup above includes committed WAL content before removal.
        active_disturbed = backup is not None
        for suffix in ("-wal", "-shm"):
            active.with_name(active.name + suffix).unlink(missing_ok=True)
        if _pending_database_identity(pending, active.parent) != pending_identity:
            raise ValueError("The pending database changed identity before activation.")
        os.replace(pending, active)
        activated = True
        inspect_database(active)
        return DatabaseImportResult(
            applied=True,
            backup_path=backup,
            active_database_matches_import=True,
        )
    except Exception as exc:
        error = str(exc)
        if (activated or active_disturbed) and backup is not None and backup.is_file():
            try:
                _restore_backup_atomically(active, backup)
            except Exception as restore_exc:
                error = (
                    f"{error} Automatic restore also failed: {restore_exc}. "
                    f"The safety backup is preserved at {backup}."
                )
        elif activated and active.is_file():
            try:
                _reject_pending(active)
            except OSError as reject_exc:
                error = (
                    f"{error} The failed imported database could not be moved aside: "
                    f"{reject_exc}."
                )
        active_matches_import = False
        if imported_hash is not None:
            try:
                active_matches_import = (
                    active.is_file() and _sha256(active) == imported_hash
                )
            except Exception:
                pass
        return DatabaseImportResult(
            applied=False,
            backup_path=backup,
            error=error,
            active_database_matches_import=active_matches_import,
        )


def apply_pending_database_import(active_db_path: str | Path) -> DatabaseImportResult | None:
    """Apply only an unowned database-only pending import."""
    return _apply_pending_database_import(
        active_db_path,
        allow_portability_owned=False,
    )


def apply_portability_owned_database_import(
    active_db_path: str | Path,
) -> DatabaseImportResult | None:
    """Apply the standard pending DB while a validated portability journal owns it."""
    return _apply_pending_database_import(
        active_db_path,
        allow_portability_owned=True,
    )
