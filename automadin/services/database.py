from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow
from automadin.models.managed_emulator import ManagedEmulatorProfile
from automadin.models.resource_path import (
    WorkspaceFolderIndex,
    normalize_resource_group,
    normalize_resource_path,
    resource_path_error,
    split_resource_path,
)
from automadin.models.run_config import SavedRunConfig
from automadin.models.visual_rule import VisualRule
from automadin.services.browser_profile_paths import (
    resolve_managed_profile_path,
)
from automadin.services.legacy_value_model_migration import (
    LegacyValueModelMigrationError,
    migrate_legacy_flows,
)

if TYPE_CHECKING:
    from automadin.services.browser_profile_metadata import (
        ProfileIdentityObservation,
        StoredBrowserProfile,
    )


CURRENT_SCHEMA_VERSION = 8
MIN_SUPPORTED_SCHEMA_VERSION = 0
_BASE_SCHEMA_TABLES = frozenset(
    {
        "app_settings",
        "flow_versions",
        "flows",
        "images",
        "profiles",
        "run_configs",
        "visual_rules",
    }
)
_SCHEMA_V4_TABLES = _BASE_SCHEMA_TABLES | frozenset({"schedules", "run_history"})
CURRENT_SCHEMA_TABLES = _SCHEMA_V4_TABLES | frozenset(
    {"managed_emulator_profiles"}
)
LEGACY_SCHEMA_TABLES = frozenset({"flows", "images", "profiles", "run_configs"})
_SCHEMA_V1_COLUMNS = {
    "flows": frozenset({"id", "name", "data", "created_at", "updated_at"}),
    "images": frozenset({"id", "name", "data", "width", "height", "created_at"}),
    "run_configs": frozenset({"id", "name", "data", "created_at", "updated_at"}),
    "profiles": frozenset(
        {
            "id",
            "name",
            "created_at",
            "browser",
            "source_profile",
            "account_name",
            "account_email",
            "engine",
        }
    ),
    "app_settings": frozenset({"key", "value", "updated_at"}),
    "visual_rules": frozenset({"name", "data", "created_at", "updated_at"}),
    "flow_versions": frozenset({"id", "flow_name", "data", "change_type", "saved_at"}),
}
_SCHEMA_V2_COLUMNS = {
    **_SCHEMA_V1_COLUMNS,
    "profiles": _SCHEMA_V1_COLUMNS["profiles"]
    | frozenset({"profile_id", "legacy_storage_name"}),
}
_SCHEMA_V4_COLUMNS = {
    **_SCHEMA_V2_COLUMNS,
    "profiles": _SCHEMA_V2_COLUMNS["profiles"]
    | frozenset({"identity_checked_at", "identity_stale_mask"}),
    "schedules": frozenset(
        {
            "schedule_id",
            "name",
            "run_config_id",
            "data",
            "enabled",
            "next_due_at_utc",
            "last_due_at_utc",
            "created_at",
            "updated_at",
        }
    ),
    "run_history": frozenset(
        {
            "run_id",
            "source",
            "schedule_id",
            "schedule_name",
            "run_config_id",
            "run_config_name",
            "flow_name",
            "scheduled_for_utc",
            "started_at_utc",
            "finished_at_utc",
            "outcome",
            "message",
        }
    ),
}
_SCHEMA_V5_COLUMNS = {
    **_SCHEMA_V4_COLUMNS,
    "flows": _SCHEMA_V4_COLUMNS["flows"] | frozenset({"flow_id"}),
    "flow_versions": _SCHEMA_V4_COLUMNS["flow_versions"]
    | frozenset({"flow_id"}),
}
_SCHEMA_V6_COLUMNS = {
    **_SCHEMA_V5_COLUMNS,
    "managed_emulator_profiles": frozenset(
        {
            "profile_id",
            "name",
            "target_key",
            "data",
            "created_at",
            "updated_at",
        }
    ),
}
CURRENT_SCHEMA_COLUMNS = {
    **_SCHEMA_V6_COLUMNS,
    "images": _SCHEMA_V6_COLUMNS["images"]
    | frozenset(
        {
            "source_surface_width",
            "source_surface_height",
            "source_coordinate_space",
        }
    ),
}
_SCHEMA_TABLES_BY_VERSION = {
    1: _BASE_SCHEMA_TABLES,
    2: _BASE_SCHEMA_TABLES,
    3: _BASE_SCHEMA_TABLES,
    4: _SCHEMA_V4_TABLES,
    5: _SCHEMA_V4_TABLES,
    6: CURRENT_SCHEMA_TABLES,
    7: CURRENT_SCHEMA_TABLES,
    8: CURRENT_SCHEMA_TABLES,
}
_SCHEMA_COLUMNS_BY_VERSION = {
    1: _SCHEMA_V1_COLUMNS,
    2: _SCHEMA_V2_COLUMNS,
    3: {
        **_SCHEMA_V2_COLUMNS,
        "profiles": _SCHEMA_V2_COLUMNS["profiles"]
        | frozenset({"identity_checked_at", "identity_stale_mask"}),
    },
    4: _SCHEMA_V4_COLUMNS,
    5: _SCHEMA_V5_COLUMNS,
    6: _SCHEMA_V6_COLUMNS,
    7: CURRENT_SCHEMA_COLUMNS,
    8: CURRENT_SCHEMA_COLUMNS,
}
_PROFILE_ID_UNIQUE_INDEX = "idx_profiles_profile_id"
_FLOW_ID_UNIQUE_INDEX = "idx_flows_flow_id"
_FLOW_VERSION_ID_INDEX = "idx_flow_versions_flow_id_saved"


def schema_tables_for_version(version: int) -> frozenset[str]:
    if version == 0:
        return LEGACY_SCHEMA_TABLES
    try:
        return _SCHEMA_TABLES_BY_VERSION[version]
    except KeyError as exc:
        raise UnsupportedDatabaseVersionError(
            f"No schema contract is defined for database version {version}."
        ) from exc


class UnsupportedDatabaseVersionError(ValueError):
    """Raised before mutation when a database schema is newer than this build."""


class DatabaseMigrationError(RuntimeError):
    """Raised after a schema migration is rolled back."""


class DatabaseSchemaError(ValueError):
    """Raised when a versioned database does not match its schema contract."""


class ImageAlreadyExistsError(ValueError):
    """Raised when a new image collides with an existing image identity."""


class ImageRevisionConflictError(ValueError):
    """Raised when an image changed after the caller reviewed it."""


@dataclass(frozen=True, slots=True)
class ImageRevision:
    """Immutable image identity and content revision used for guarded writes."""

    name: str
    width: int
    height: int
    sha256: str
    source_surface_width: int = 0
    source_surface_height: int = 0
    source_coordinate_space: str = ""


@dataclass(frozen=True, slots=True)
class StoredImage:
    """Stored template bytes and the surface on which they were captured."""

    name: str
    data: bytes
    width: int
    height: int
    source_surface_width: int = 0
    source_surface_height: int = 0
    source_coordinate_space: str = ""

    @property
    def source_surface_size(self) -> tuple[int, int] | None:
        if self.source_surface_width < 2 or self.source_surface_height < 2:
            return None
        return self.source_surface_width, self.source_surface_height


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_statistics_from_connection(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM flows) AS flows,
            (SELECT COUNT(*) FROM images) AS images,
            (SELECT COUNT(*) FROM run_configs) AS run_setups,
            (SELECT COUNT(*) FROM profiles) AS browser_profiles
        """
    ).fetchone()
    if row is None:  # pragma: no cover - SQLite always returns one aggregate row
        raise RuntimeError("Workspace statistics were not returned.")
    return {
        "schema_version": int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        ),
        "flows": int(row["flows"]),
        "images": int(row["images"]),
        "run_setups": int(row["run_setups"]),
        "browser_profiles": int(row["browser_profiles"]),
    }


def read_workspace_statistics(
    db_path: str | Path,
    *,
    timeout_seconds: float = 1.0,
) -> dict[str, int]:
    """Read aggregate-only statistics through a short-lived read-only connection."""
    timeout = max(0.05, min(float(timeout_seconds), 5.0))
    database_path = Path(db_path).expanduser().resolve(strict=True)
    uri = f"{database_path.as_uri()}?mode=ro"
    with closing(
        sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout,
            check_same_thread=True,
        )
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute(f"PRAGMA busy_timeout={round(timeout * 1000)}")
        return _workspace_statistics_from_connection(connection)


class DatabaseService:
    CURRENT_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
    MIN_SUPPORTED_SCHEMA_VERSION = MIN_SUPPORTED_SCHEMA_VERSION

    def __init__(self, db_path: str = "automadin.db", *, enable_wal: bool = True) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        try:
            self._local.connection = self._open_connection()
            self._initialize_schema()
            self._migrate_legacy_value_models()
            if enable_wal:
                self._conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            self.close()
            raise

    def _open_connection(self) -> sqlite3.Connection:
        """Open a connection owned by the calling thread."""
        # Connections are never used concurrently, but disabling the affinity
        # check lets the owner service close worker-created connections during
        # orderly application shutdown.
        conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        with self._connections_lock:
            self._connections.add(conn)
        return conn

    def _migrate_legacy_value_models(self) -> None:
        """Atomically replace retired flow value records after normal schema setup."""
        rows = self._conn.execute(
            "SELECT name, flow_id, data FROM flows ORDER BY name"
        ).fetchall()
        flows = [
            self._flow_from_persisted_row(
                row["flow_id"], row["data"], label=f"Flow '{row['name']}'"
            )
            for row in rows
        ]
        if not any(
            flow.value_model_version != CURRENT_FLOW_VALUE_MODEL_VERSION
            for flow in flows
        ):
            return
        try:
            migrated = migrate_legacy_flows(flows)
        except LegacyValueModelMigrationError as exc:
            raise DatabaseMigrationError(
                f"Automatic Inputs and Variables migration could not complete: {exc}"
            ) from exc
        now = _now_iso()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for flow in migrated:
                data = flow.to_dict()
                self._conn.execute(
                    "UPDATE flows SET data = ?, updated_at = ? WHERE name = ?",
                    (json.dumps(data), now, flow.name),
                )
            self._conn.execute("DELETE FROM flow_versions")
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise DatabaseMigrationError(
                f"Automatic Inputs and Variables migration was rolled back: {exc}"
            ) from exc

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = self._open_connection()
            self._local.connection = conn
        return conn

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def get_workspace_statistics(self) -> dict[str, int]:
        """Return bounded aggregate counts without loading workspace records."""
        return _workspace_statistics_from_connection(self._conn)

    def _initialize_schema(self) -> None:
        version = self.schema_version
        if version > self.CURRENT_SCHEMA_VERSION:
            raise UnsupportedDatabaseVersionError(
                f"Database schema version {version} is newer than this Automadin build "
                f"(supports through version {self.CURRENT_SCHEMA_VERSION})."
            )
        if version < self.MIN_SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedDatabaseVersionError(
                f"Database schema version {version} is no longer supported."
            )

        while version < self.CURRENT_SCHEMA_VERSION:
            next_version = version + 1
            migrator = getattr(self, f"_migrate_v{version}_to_v{next_version}", None)
            if migrator is None:
                raise UnsupportedDatabaseVersionError(
                    f"No migration is available from schema version {version}."
                )
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                migrator()
                self._validate_schema(next_version)
                self._conn.execute(f"PRAGMA user_version = {next_version}")
                self._conn.commit()
            except Exception as exc:
                self._conn.rollback()
                raise DatabaseMigrationError(
                    f"Could not migrate Automadin database from schema version "
                    f"{version} to {next_version}: {exc}"
                ) from exc
            version = next_version

        self._validate_schema(version)

    def _migrate_v0_to_v1(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS flows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data BLOB NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS run_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                browser TEXT NOT NULL DEFAULT '',
                source_profile TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                account_email TEXT NOT NULL DEFAULT '',
                engine TEXT NOT NULL DEFAULT ''
            )""",
            """CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS visual_rules (
                name TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS flow_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flow_name TEXT NOT NULL,
                data TEXT NOT NULL,
                change_type TEXT NOT NULL DEFAULT 'update',
                saved_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_flow_versions_name_saved
                ON flow_versions (flow_name, saved_at DESC, id DESC)""",
        )
        for statement in statements:
            self._conn.execute(statement)

        profile_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(profiles)")
        }
        for column in ("browser", "source_profile", "account_name", "account_email", "engine"):
            if column not in profile_columns:
                self._conn.execute(
                    f"ALTER TABLE profiles ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )

    def _migrate_v1_to_v2(self) -> None:
        """Give profiles stable UUID identities and migrate safe saved references."""
        profile_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(profiles)")
        }
        if "profile_id" not in profile_columns:
            self._conn.execute(
                "ALTER TABLE profiles ADD COLUMN profile_id TEXT NOT NULL DEFAULT ''"
            )
        if "legacy_storage_name" not in profile_columns:
            self._conn.execute(
                "ALTER TABLE profiles "
                "ADD COLUMN legacy_storage_name TEXT NOT NULL DEFAULT ''"
            )

        profile_rows = self._conn.execute(
            "SELECT id, name, profile_id, legacy_storage_name FROM profiles ORDER BY id"
        ).fetchall()
        identities: list[tuple[str, str]] = []
        for row in profile_rows:
            profile_id = str(row["profile_id"])
            try:
                parsed_profile_id = str(uuid.UUID(profile_id))
            except (ValueError, AttributeError):
                parsed_profile_id = ""
            if not profile_id or parsed_profile_id != profile_id:
                profile_id = str(uuid.uuid4())
            legacy_storage_name = str(row["legacy_storage_name"]) or str(row["name"])
            self._conn.execute(
                """
                UPDATE profiles
                   SET profile_id = ?, legacy_storage_name = ?
                 WHERE id = ?
                """,
                (profile_id, legacy_storage_name, row["id"]),
            )
            identities.append((str(row["name"]), profile_id))

        self._conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_PROFILE_ID_UNIQUE_INDEX} "
            "ON profiles (profile_id)"
        )
        self._migrate_saved_profile_references(identities)

    def _migrate_v2_to_v3(self) -> None:
        """Track field-level browser profile identity freshness."""
        profile_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(profiles)")
        }
        if "identity_checked_at" not in profile_columns:
            self._conn.execute(
                "ALTER TABLE profiles "
                "ADD COLUMN identity_checked_at TEXT NOT NULL DEFAULT ''"
            )
        if "identity_stale_mask" not in profile_columns:
            self._conn.execute(
                "ALTER TABLE profiles "
                "ADD COLUMN identity_stale_mask INTEGER NOT NULL DEFAULT 7"
            )
        # Frozen v2 accepted arbitrary SQLite values in these presentation-only
        # fields. Preserve valid labels exactly, but make legacy unsafe values
        # renderable instead of rolling back the migration and locking out users.
        identity_columns = (
            "browser",
            "source_profile",
            "account_name",
            "account_email",
        )
        for row in self._conn.execute(
            "SELECT id, browser, source_profile, account_name, account_email "
            "FROM profiles"
        ).fetchall():
            updates: dict[str, str] = {}
            for column in identity_columns:
                value = row[column]
                if not isinstance(value, str):
                    normalized = ""
                else:
                    normalized = "".join(
                        " " if ord(character) < 32 or ord(character) == 127 else character
                        for character in value[:320]
                    )
                if normalized != value:
                    updates[column] = normalized
            if updates:
                assignments = ", ".join(f"{column} = ?" for column in updates)
                self._conn.execute(
                    f"UPDATE profiles SET {assignments} WHERE id = ?",
                    (*updates.values(), row["id"]),
                )

    def _migrate_v3_to_v4(self) -> None:
        """Add durable schedule definitions and bounded run-history storage."""
        self._conn.execute(
            """
            CREATE TABLE schedules (
                schedule_id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                run_config_id INTEGER NOT NULL,
                data TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                next_due_at_utc TEXT,
                last_due_at_utc TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_config_id) REFERENCES run_configs(id)
                    ON DELETE RESTRICT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE run_history (
                run_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                schedule_id TEXT,
                schedule_name TEXT NOT NULL,
                run_config_id INTEGER,
                run_config_name TEXT NOT NULL,
                flow_name TEXT NOT NULL,
                scheduled_for_utc TEXT NOT NULL,
                started_at_utc TEXT,
                finished_at_utc TEXT,
                outcome TEXT NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY (schedule_id) REFERENCES schedules(schedule_id)
                    ON DELETE SET NULL,
                FOREIGN KEY (run_config_id) REFERENCES run_configs(id)
                    ON DELETE SET NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX idx_schedules_due
                ON schedules (enabled, next_due_at_utc)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX idx_run_history_scheduled
                ON run_history (scheduled_for_utc DESC, run_id DESC)
            """
        )

    def _migrate_v4_to_v5(self) -> None:
        """Assign stable UUID identities to flows and their version chains."""
        self._conn.execute(
            "ALTER TABLE flows ADD COLUMN flow_id TEXT NOT NULL DEFAULT ''"
        )
        self._conn.execute(
            "ALTER TABLE flow_versions ADD COLUMN flow_id TEXT NOT NULL DEFAULT ''"
        )

        used_ids: set[str] = set()

        def new_identity() -> str:
            while True:
                candidate = str(uuid.uuid4())
                if candidate not in used_ids:
                    used_ids.add(candidate)
                    return candidate

        def migration_timestamp(value: object) -> datetime | None:
            if not isinstance(value, str):
                return None
            normalized = value.strip()
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc)

        def load_payload(raw_data: object, *, label: str) -> dict[str, Any]:
            try:
                data = json.loads(raw_data)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{label} contains invalid JSON.") from exc
            if not isinstance(data, dict):
                raise ValueError(f"{label} data must be a JSON object.")
            return data

        def existing_identity(data: dict[str, Any]) -> str | None:
            raw_identity = data.get("flow_id")
            try:
                identity = str(uuid.UUID(raw_identity))
            except (AttributeError, TypeError, ValueError):
                return None
            if identity != raw_identity or identity in used_ids:
                return None
            used_ids.add(identity)
            return identity

        def stamp(
            data: dict[str, Any],
            *,
            flow_id: str,
            expected_name: str,
            label: str,
            repair_historical_name: bool = False,
        ) -> str:
            if data.get("name") != expected_name:
                if not repair_historical_name:
                    raise ValueError(
                        f"{label} name does not match its database record."
                    )
                data["name"] = expected_name
            data["flow_id"] = flow_id
            data.setdefault("value_model_version", 0)
            AutomationFlow.from_dict(data)
            return json.dumps(data)

        current: dict[str, tuple[str, datetime | None]] = {}
        flow_rows = self._conn.execute(
            "SELECT id, name, data, created_at FROM flows ORDER BY id"
        ).fetchall()
        for row in flow_rows:
            name = str(row["name"])
            data = load_payload(row["data"], label=f"Flow '{name}'")
            flow_id = existing_identity(data) or new_identity()
            current[name] = (flow_id, migration_timestamp(row["created_at"]))
            self._conn.execute(
                "UPDATE flows SET flow_id = ?, data = ? WHERE id = ?",
                (
                    flow_id,
                    stamp(
                        data,
                        flow_id=flow_id,
                        expected_name=name,
                        label=f"Flow '{name}'",
                    ),
                    row["id"],
                ),
            )

        orphan_identities: dict[str, str] = {}
        version_rows = self._conn.execute(
            "SELECT id, flow_name, data, saved_at FROM flow_versions ORDER BY id"
        ).fetchall()
        for row in version_rows:
            flow_name = str(row["flow_name"])
            saved_at = migration_timestamp(row["saved_at"])
            current_identity = current.get(flow_name)
            if (
                current_identity is not None
                and current_identity[1] is not None
                and saved_at is not None
                and saved_at >= current_identity[1]
            ):
                flow_id = current_identity[0]
            else:
                flow_id = orphan_identities.get(flow_name)
                if flow_id is None:
                    flow_id = new_identity()
                    orphan_identities[flow_name] = flow_id
            data = load_payload(
                row["data"],
                label=f"Flow version {row['id']}",
            )
            self._conn.execute(
                "UPDATE flow_versions SET flow_id = ?, data = ? WHERE id = ?",
                (
                    flow_id,
                    stamp(
                        data,
                        flow_id=flow_id,
                        expected_name=flow_name,
                        label=f"Flow version {row['id']}",
                        repair_historical_name=True,
                    ),
                    row["id"],
                ),
            )

        self._conn.execute(
            f"CREATE UNIQUE INDEX {_FLOW_ID_UNIQUE_INDEX} ON flows (flow_id)"
        )
        self._conn.execute(
            f"CREATE INDEX {_FLOW_VERSION_ID_INDEX} "
            "ON flow_versions (flow_id, saved_at DESC, id DESC)"
        )

    def _migrate_v5_to_v6(self) -> None:
        """Add reusable managed Android emulator profiles."""
        self._conn.execute(
            """
            CREATE TABLE managed_emulator_profiles (
                profile_id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                target_key TEXT NOT NULL UNIQUE,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _migrate_v6_to_v7(self) -> None:
        """Record the source automation surface for image templates."""
        self._conn.execute(
            "ALTER TABLE images "
            "ADD COLUMN source_surface_width INTEGER NOT NULL DEFAULT 0"
        )
        self._conn.execute(
            "ALTER TABLE images "
            "ADD COLUMN source_surface_height INTEGER NOT NULL DEFAULT 0"
        )
        self._conn.execute(
            "ALTER TABLE images "
            "ADD COLUMN source_coordinate_space TEXT NOT NULL DEFAULT ''"
        )

    def _migrate_v7_to_v8(self) -> None:
        """Retire Click Moving Target without leaving unloadable flow records."""

        retired_id = "click_moving_target"
        removed_by_flow: dict[str, int] = {}

        def strip_commands(raw_commands: object) -> tuple[list[object], int]:
            if not isinstance(raw_commands, list):
                raise ValueError("Flow command collections must be arrays.")
            kept: list[object] = []
            removed = 0
            for raw_command in raw_commands:
                if not isinstance(raw_command, dict):
                    raise ValueError("Flow commands must be objects.")
                if raw_command.get("id") == retired_id:
                    removed += 1
                    continue
                children, child_count = strip_commands(
                    raw_command.get("children", [])
                )
                else_children, else_count = strip_commands(
                    raw_command.get("else_children", [])
                )
                raw_command["children"] = children
                raw_command["else_children"] = else_children
                removed += child_count + else_count
                kept.append(raw_command)
            return kept, removed

        def strip_flow(raw_data: object, *, label: str) -> tuple[str, int]:
            try:
                data = json.loads(raw_data)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{label} contains invalid JSON.") from exc
            if not isinstance(data, dict):
                raise ValueError(f"{label} data must be a JSON object.")

            commands, removed = strip_commands(data.get("commands", []))
            data["commands"] = commands
            hooks = data.get("hooks", [])
            if not isinstance(hooks, list):
                raise ValueError(f"{label} hooks must be an array.")
            for hook in hooks:
                if not isinstance(hook, dict):
                    raise ValueError(f"{label} hooks must be objects.")
                actions, action_count = strip_commands(hook.get("actions", []))
                hook["actions"] = actions
                removed += action_count

            # Validate the transformed record before any row is updated. The
            # surrounding migration transaction rolls every row back together.
            AutomationFlow.from_dict(data)
            return json.dumps(data), removed

        for row in self._conn.execute(
            "SELECT id, name, data FROM flows ORDER BY id"
        ).fetchall():
            flow_name = str(row["name"])
            transformed, removed = strip_flow(
                row["data"], label=f"Flow '{flow_name}'"
            )
            if not removed:
                continue
            self._conn.execute(
                "UPDATE flows SET data = ?, updated_at = ? WHERE id = ?",
                (transformed, _now_iso(), row["id"]),
            )
            removed_by_flow[flow_name] = removed_by_flow.get(flow_name, 0) + removed

        for row in self._conn.execute(
            "SELECT id, flow_name, data FROM flow_versions ORDER BY id"
        ).fetchall():
            flow_name = str(row["flow_name"])
            transformed, removed = strip_flow(
                row["data"], label=f"Flow version {row['id']}"
            )
            if not removed:
                continue
            self._conn.execute(
                "UPDATE flow_versions SET data = ? WHERE id = ?",
                (transformed, row["id"]),
            )
            removed_by_flow[flow_name] = removed_by_flow.get(flow_name, 0) + removed

        if removed_by_flow:
            report = json.dumps(
                {
                    "command": "Click Moving Target",
                    "flows": dict(sorted(removed_by_flow.items(), key=lambda item: item[0].casefold())),
                }
            )
            now = _now_iso()
            self._conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ('retired_command_v8_report', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (report, now),
            )

    def _migrate_saved_profile_references(
        self,
        identities: list[tuple[str, str]],
    ) -> None:
        """Rewrite only legacy references that identify exactly one profile."""
        from automadin.models.device import (
            BrowserDevice,
            BrowserDeviceConfig,
            browser_profile_reference_from_saved_device_id,
            device_from_saved_id,
        )

        exact = {name: profile_id for name, profile_id in identities}
        folded: dict[str, list[str]] = {}
        for name, profile_id in identities:
            folded.setdefault(name.casefold(), []).append(profile_id)

        for row in self._conn.execute("SELECT id, data FROM run_configs").fetchall():
            try:
                data = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue

            saved_device_id = data.get("device_id")
            reference = browser_profile_reference_from_saved_device_id(saved_device_id)
            if reference is None or not reference.legacy_name:
                continue

            profile_id = exact.get(reference.legacy_name)
            if profile_id is None:
                candidates = folded.get(reference.legacy_name.casefold(), [])
                if len(candidates) != 1:
                    continue
                profile_id = candidates[0]

            try:
                device = device_from_saved_id(saved_device_id)
            except (TypeError, ValueError, IndexError):
                continue
            if not isinstance(device, BrowserDevice):
                continue
            data["device_id"] = BrowserDeviceConfig(
                engine=device.config.engine,
                headless=device.config.headless,
                screen_mode=device.config.screen_mode,
                profile_id=profile_id,
            ).to_id()
            self._conn.execute(
                "UPDATE run_configs SET data = ? WHERE id = ?",
                (json.dumps(data), row["id"]),
            )

    def _validate_schema(self, version: int) -> None:
        expected_tables = _SCHEMA_TABLES_BY_VERSION.get(version)
        expected_columns_by_table = _SCHEMA_COLUMNS_BY_VERSION.get(version)
        if expected_tables is None or expected_columns_by_table is None:
            raise DatabaseSchemaError(
                f"No schema contract is defined for database version {version}."
            )

        table_names = {
            str(row[0])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = sorted(expected_tables - table_names)
        if missing_tables:
            raise DatabaseSchemaError(
                "Database schema is incomplete. Missing tables: "
                + ", ".join(missing_tables)
            )
        for table_name, expected_columns in expected_columns_by_table.items():
            actual_columns = {
                str(row["name"])
                for row in self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            missing_columns = sorted(expected_columns - actual_columns)
            if missing_columns:
                raise DatabaseSchemaError(
                    f"Database table '{table_name}' is missing columns: "
                    + ", ".join(missing_columns)
                )

        if version >= 2:
            self._validate_profile_identity_schema()
        if version >= 3:
            self._validate_profile_refresh_schema()
        if version >= 4:
            self._validate_schedule_schema()
        if version >= 5:
            self._validate_flow_identity_schema()
        if version >= 6:
            self._validate_managed_emulator_schema()

    def _validate_managed_emulator_schema(self) -> None:
        rows = self._conn.execute(
            """
            SELECT profile_id, name, target_key, data
              FROM managed_emulator_profiles
            """
        ).fetchall()
        for row in rows:
            try:
                profile = ManagedEmulatorProfile.from_dict(json.loads(row["data"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise DatabaseSchemaError(
                    "Database contains invalid managed emulator profile data."
                ) from exc
            if (
                profile.profile_id != row["profile_id"]
                or profile.name != row["name"]
                or profile.target_key != row["target_key"]
            ):
                raise DatabaseSchemaError(
                    "Managed emulator profile columns do not match their data."
                )

    def _validate_schedule_schema(self) -> None:
        schedule_indexes = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA index_list(schedules)").fetchall()
        }
        if "idx_schedules_due" not in schedule_indexes:
            raise DatabaseSchemaError("Database schedule due index is missing.")
        history_indexes = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA index_list(run_history)").fetchall()
        }
        if "idx_run_history_scheduled" not in history_indexes:
            raise DatabaseSchemaError("Database run-history index is missing.")

    def _validate_flow_identity_schema(self) -> None:
        indexes = {
            str(row["name"]): bool(row["unique"])
            for row in self._conn.execute("PRAGMA index_list(flows)").fetchall()
        }
        if not indexes.get(_FLOW_ID_UNIQUE_INDEX, False):
            raise DatabaseSchemaError(
                "Database flow identity index is missing or is not unique."
            )
        indexed_columns = [
            str(row["name"])
            for row in self._conn.execute(
                f"PRAGMA index_info({_FLOW_ID_UNIQUE_INDEX})"
            ).fetchall()
        ]
        if indexed_columns != ["flow_id"]:
            raise DatabaseSchemaError(
                "Database flow identity index has an invalid definition."
            )
        version_indexes = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA index_list(flow_versions)").fetchall()
        }
        if _FLOW_VERSION_ID_INDEX not in version_indexes:
            raise DatabaseSchemaError("Database flow-version identity index is missing.")
        version_indexed_columns = [
            str(row["name"])
            for row in self._conn.execute(
                f"PRAGMA index_info({_FLOW_VERSION_ID_INDEX})"
            ).fetchall()
        ]
        if version_indexed_columns != ["flow_id", "saved_at", "id"]:
            raise DatabaseSchemaError(
                "Database flow-version identity index has an invalid definition."
            )

        for table, name_column, label in (
            ("flows", "name", "Flow"),
            ("flow_versions", "flow_name", "Flow version"),
        ):
            rows = self._conn.execute(
                f"SELECT flow_id, {name_column} AS persisted_name, data FROM {table}"
            ).fetchall()
            for row in rows:
                flow_id = str(row["flow_id"])
                try:
                    canonical = str(uuid.UUID(flow_id))
                except (AttributeError, ValueError) as exc:
                    raise DatabaseSchemaError(
                        f"Database contains an invalid {label.lower()} UUID."
                    ) from exc
                if flow_id != canonical:
                    raise DatabaseSchemaError(
                        f"Database contains a non-canonical {label.lower()} UUID."
                    )
                try:
                    data = json.loads(row["data"])
                    flow = AutomationFlow.from_dict(data)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise DatabaseSchemaError(
                        f"Database contains invalid {label.lower()} data."
                    ) from exc
                if flow.flow_id != flow_id:
                    raise DatabaseSchemaError(
                        f"Database {label.lower()} identity does not match its data."
                    )
                if flow.name != row["persisted_name"]:
                    raise DatabaseSchemaError(
                        f"Database {label.lower()} name does not match its data."
                    )

    def _validate_profile_identity_schema(self) -> None:
        indexes = {
            str(row["name"]): bool(row["unique"])
            for row in self._conn.execute("PRAGMA index_list(profiles)").fetchall()
        }
        if not indexes.get(_PROFILE_ID_UNIQUE_INDEX, False):
            raise DatabaseSchemaError(
                "Database profile identity index is missing or is not unique."
            )
        indexed_columns = [
            str(row["name"])
            for row in self._conn.execute(
                f"PRAGMA index_info({_PROFILE_ID_UNIQUE_INDEX})"
            ).fetchall()
        ]
        if indexed_columns != ["profile_id"]:
            raise DatabaseSchemaError(
                "Database profile identity index has an invalid definition."
            )

        for row in self._conn.execute("SELECT profile_id FROM profiles").fetchall():
            profile_id = str(row["profile_id"])
            try:
                canonical = str(uuid.UUID(profile_id))
            except (ValueError, AttributeError) as exc:
                raise DatabaseSchemaError(
                    "Database contains an invalid browser profile UUID."
                ) from exc
            if profile_id != canonical:
                raise DatabaseSchemaError(
                    "Database contains a non-canonical browser profile UUID."
                )

    def _validate_profile_refresh_schema(self) -> None:
        from automadin.services.browser_profile_metadata import (
            ProfileIdentityField,
            _validated_identity_checked_at,
            _validated_identity_text,
        )

        rows = self._conn.execute(
            """
            SELECT browser, source_profile, account_name, account_email,
                   identity_checked_at, identity_stale_mask
              FROM profiles
            """
        ).fetchall()
        for row in rows:
            try:
                for field_name in (
                    "browser",
                    "source_profile",
                    "account_name",
                    "account_email",
                ):
                    _validated_identity_text(row[field_name], field_name)
                _validated_identity_checked_at(
                    row["identity_checked_at"],
                    allow_empty=True,
                )
            except (TypeError, ValueError) as exc:
                raise DatabaseSchemaError(
                    "Database contains invalid browser profile identity metadata."
                ) from exc
            stale_mask = row["identity_stale_mask"]
            if (
                type(stale_mask) is not int
                or stale_mask < 0
                or stale_mask & ~int(ProfileIdentityField.ALL)
            ):
                raise DatabaseSchemaError(
                    "Database contains an invalid browser profile identity stale mask."
                )

    def _trim_flow_versions(self, flow_id: str, keep: int = 100) -> None:
        self._conn.execute(
            """
            DELETE FROM flow_versions
            WHERE flow_id = ?
              AND id NOT IN (
                  SELECT id FROM flow_versions
                  WHERE flow_id = ?
                  ORDER BY saved_at DESC, id DESC
                  LIMIT ?
              )
            """,
            (flow_id, flow_id, keep),
        )

    @staticmethod
    def _replace_command_references(
        commands: list[dict[str, Any]],
        old_name: str,
        new_name: str,
        scalar_keys: tuple[str, ...],
        list_keys: tuple[str, ...] = (),
    ) -> bool:
        changed = False
        for command in commands:
            params = command.get("params", {})
            if isinstance(params, dict):
                for key in scalar_keys:
                    if params.get(key) == old_name:
                        params[key] = new_name
                        changed = True
                for key in list_keys:
                    values = params.get(key)
                    if isinstance(values, list) and old_name in values:
                        params[key] = [new_name if value == old_name else value for value in values]
                        changed = True
            for child_key in ("children", "else_children"):
                children = command.get(child_key, [])
                if isinstance(children, list):
                    changed |= DatabaseService._replace_command_references(
                        children, old_name, new_name, scalar_keys, list_keys
                    )
        return changed

    @staticmethod
    def _replace_flow_data_references(
        data: dict[str, Any],
        old_name: str,
        new_name: str,
        scalar_keys: tuple[str, ...],
        list_keys: tuple[str, ...] = (),
    ) -> bool:
        changed = False
        commands = data.get("commands", [])
        if isinstance(commands, list):
            changed |= DatabaseService._replace_command_references(
                commands, old_name, new_name, scalar_keys, list_keys
            )
        hooks = data.get("hooks", [])
        if not isinstance(hooks, list):
            return changed
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            actions = hook.get("actions", [])
            if isinstance(actions, list):
                changed |= DatabaseService._replace_command_references(
                    actions, old_name, new_name, scalar_keys, list_keys
                )
            condition = hook.get("condition", {})
            if not isinstance(condition, dict):
                continue
            for key in scalar_keys:
                if condition.get(key) == old_name:
                    condition[key] = new_name
                    changed = True
            for key in list_keys:
                values = condition.get(key)
                if isinstance(values, list) and old_name in values:
                    condition[key] = [
                        new_name if value == old_name else value for value in values
                    ]
                    changed = True
                settings = condition.get("search_settings", {})
                nested = settings.get(key) if isinstance(settings, dict) else None
                if isinstance(nested, list) and old_name in nested:
                    settings[key] = [
                        new_name if value == old_name else value for value in nested
                    ]
                    changed = True
        return changed

    def _update_flow_references(
        self,
        conn: sqlite3.Connection,
        old_name: str,
        new_name: str,
        scalar_keys: tuple[str, ...],
        list_keys: tuple[str, ...] = (),
    ) -> None:
        now = _now_iso()
        for row in conn.execute("SELECT flow_id, name, data FROM flows").fetchall():
            data = json.loads(row["data"])
            if not self._replace_flow_data_references(
                data, old_name, new_name, scalar_keys, list_keys
            ):
                continue
            conn.execute(
                """
                INSERT INTO flow_versions
                    (flow_id, flow_name, data, change_type, saved_at)
                VALUES (?, ?, ?, 'reference_update', ?)
                """,
                (row["flow_id"], row["name"], row["data"], now),
            )
            conn.execute(
                "UPDATE flows SET data = ?, updated_at = ? WHERE name = ?",
                (json.dumps(data), now, row["name"]),
            )
            self._trim_flow_versions(row["flow_id"])

    def _update_flow_version_references(
        self,
        conn: sqlite3.Connection,
        old_name: str,
        new_name: str,
        scalar_keys: tuple[str, ...],
        list_keys: tuple[str, ...] = (),
    ) -> None:
        """Keep restorable flow history valid after a resource identity change."""
        for row in conn.execute("SELECT id, data FROM flow_versions").fetchall():
            data = json.loads(row["data"])
            if not self._replace_flow_data_references(
                data, old_name, new_name, scalar_keys, list_keys
            ):
                continue
            conn.execute(
                "UPDATE flow_versions SET data = ? WHERE id = ?",
                (json.dumps(data), row["id"]),
            )

    @staticmethod
    def _commands_reference(
        commands: list[dict[str, Any]],
        key: str,
        value: str,
    ) -> bool:
        for command in commands:
            params = command.get("params", {})
            if isinstance(params, dict) and params.get(key) == value:
                return True
            for child_key in ("children", "else_children"):
                children = command.get(child_key, [])
                if isinstance(children, list) and DatabaseService._commands_reference(
                    children, key, value
                ):
                    return True
        return False

    @staticmethod
    def _flow_data_commands_reference(
        data: dict[str, Any],
        key: str,
        value: str,
    ) -> bool:
        commands = data.get("commands", [])
        if isinstance(commands, list) and DatabaseService._commands_reference(
            commands, key, value
        ):
            return True
        hooks = data.get("hooks", [])
        return isinstance(hooks, list) and any(
            isinstance(hook, dict)
            and isinstance(hook.get("actions"), list)
            and DatabaseService._commands_reference(hook["actions"], key, value)
            for hook in hooks
        )

    def _backup_existing_flow(
        self,
        name: str,
        new_data_json: str | None = None,
        change_type: str = "update",
    ) -> None:
        row = self._conn.execute(
            "SELECT flow_id, data FROM flows WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return
        old_data = row["data"]
        if new_data_json is not None and old_data == new_data_json:
            return
        self._conn.execute(
            """
            INSERT INTO flow_versions
                (flow_id, flow_name, data, change_type, saved_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["flow_id"], name, old_data, change_type, _now_iso()),
        )
        self._trim_flow_versions(row["flow_id"])

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local.connection = None

    def release_current_thread_connection(self) -> bool:
        """Close and forget only the calling thread's lazy database connection."""
        connection = getattr(self._local, "connection", None)
        if connection is None:
            return False
        self._local.connection = None
        with self._connections_lock:
            self._connections.discard(connection)
        try:
            connection.close()
        except sqlite3.Error:
            pass
        return True

    def export_database(self, destination_path: str) -> None:
        """Atomically write a validated SQLite backup, including WAL changes.

        A user-selected existing destination is replaced only after the complete
        sibling backup has passed the current schema validation.
        """
        if os.path.abspath(destination_path) == os.path.abspath(self._db_path):
            raise ValueError("Choose a different path than the active database file.")
        destination = os.path.abspath(destination_path)
        parent_dir = os.path.dirname(destination)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        temporary = os.path.join(
            parent_dir,
            f".{os.path.basename(destination)}.{uuid.uuid4().hex}.tmp",
        )
        try:
            self._conn.commit()
            with closing(sqlite3.connect(temporary)) as backup_conn:
                self._conn.backup(backup_conn)
                backup_conn.commit()

            validator = DatabaseService(temporary, enable_wal=False)
            try:
                if validator.schema_version != self.CURRENT_SCHEMA_VERSION:
                    raise DatabaseSchemaError(
                        "Exported database did not retain the current schema version."
                    )
            finally:
                validator.close()
            os.replace(temporary, destination)
        finally:
            for path in (
                temporary,
                temporary + "-wal",
                temporary + "-shm",
                temporary + "-journal",
            ):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    # -- App settings -------------------------------------------------------

    def get_app_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def set_app_setting(self, key: str, value: str) -> None:
        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        self._conn.commit()

    def delete_app_setting(self, key: str) -> None:
        self._conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        self._conn.commit()

    def _assert_no_name_collision(
        self,
        table: str,
        candidate: str,
        *,
        resource_label: str,
        ignore_exact: str | None = None,
    ) -> None:
        """Reject visually equivalent names while preserving exact-key updates."""
        target = normalize_resource_path(candidate).casefold()
        for row in self._conn.execute(f"SELECT name FROM {table}").fetchall():
            existing = str(row["name"])
            if ignore_exact is not None and existing == ignore_exact:
                continue
            if normalize_resource_path(existing).casefold() == target:
                raise ValueError(
                    f"A {resource_label} named '{existing}' already exists."
                )

    def list_resource_paths(self) -> list[str]:
        """Return every folder-aware resource path used by the workspace."""
        paths = list(self.list_flows())
        paths.extend(self.list_visual_rules())
        paths.extend(str(item["name"]) for item in self.list_images())
        for name in self.list_run_configs():
            try:
                config = self.load_run_config(name)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if config is not None:
                paths.append(
                    normalize_resource_path(
                        f"{config.group_path}/{config.name}"
                        if config.group_path
                        else config.name
                    )
                )
        return paths

    def workspace_folder_index(
        self,
        *,
        ignore_run_config: str | None = None,
        candidate_run_group: str = "",
    ) -> WorkspaceFolderIndex:
        """Build the authoritative folder display index for this workspace.

        Saved run groups are explicit organization choices and take precedence
        over casing inferred from slash-qualified resource identities. A run
        editor can replace its old observation with a candidate so a case-only
        correction is respected instead of silently bouncing back.
        """
        explicit_groups: list[str] = []
        rows = self._conn.execute(
            "SELECT name, data FROM run_configs ORDER BY name"
        ).fetchall()
        for row in rows:
            if ignore_run_config is not None and row["name"] == ignore_run_config:
                continue
            try:
                config = SavedRunConfig.from_dict(json.loads(row["data"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if config.group_path:
                explicit_groups.append(config.group_path)

        resource_paths = list(self.list_flows())
        resource_paths.extend(self.list_visual_rules())
        resource_paths.extend(str(item["name"]) for item in self.list_images())
        return WorkspaceFolderIndex(
            explicit_groups=explicit_groups,
            resource_paths=resource_paths,
            candidate_groups=(candidate_run_group,) if candidate_run_group else (),
        )

    def resolve_resource_folder(
        self,
        value: Any,
        *,
        ignore_run_config: str | None = None,
        candidate_is_run_group: bool = False,
    ) -> str:
        """Return the shared display spelling for a virtual folder."""
        candidate = normalize_resource_path(value)
        return self.workspace_folder_index(
            ignore_run_config=ignore_run_config,
            candidate_run_group=candidate if candidate_is_run_group else "",
        ).canonicalize(candidate)

    def list_resource_folders(self) -> list[str]:
        """Return shared folder suggestions using one workspace-wide spelling."""
        return self.workspace_folder_index().folders

    @staticmethod
    def _path_in_folder(path: str, folder: str, *, path_is_group: bool = False) -> bool:
        path_parts = split_resource_path(path)
        folder_parts = split_resource_path(folder)
        compared = path_parts if path_is_group else path_parts[:-1]
        return len(compared) >= len(folder_parts) and all(
            left.casefold() == right.casefold()
            for left, right in zip(compared, folder_parts)
        )

    @staticmethod
    def _move_path_between_folders(
        path: str,
        source: str,
        destination: str,
        *,
        path_is_group: bool = False,
    ) -> str:
        path_parts = split_resource_path(path)
        source_parts = split_resource_path(source)
        destination_parts = split_resource_path(destination)
        prefix_length = len(source_parts)
        return "/".join(destination_parts + path_parts[prefix_length:])

    @staticmethod
    def _validate_folder_move_names(
        existing: list[str], changes: dict[str, str], *, resource_label: str
    ) -> None:
        resulting: dict[str, str] = {}
        for old_name in existing:
            candidate = changes.get(old_name, old_name)
            error = resource_path_error(candidate)
            if error:
                raise ValueError(f"Cannot move {resource_label} '{old_name}': {error}")
            folded = normalize_resource_path(candidate).casefold()
            collision = resulting.get(folded)
            if collision is not None and collision != old_name:
                raise ValueError(
                    f"Moving this folder would create two {resource_label}s named "
                    f"'{candidate}'. Move or rename one of them first."
                )
            resulting[folded] = old_name

    def move_resource_folder(
        self, source: str, destination: str
    ) -> dict[str, list[tuple[str, str]]]:
        """Atomically move a virtual folder and every resource below it.

        Virtual folders are projections of resource paths rather than separate
        records.  Moving to the parent therefore implements folder deletion
        without deleting any contained automation resource.
        """
        source_group = self.workspace_folder_index().canonicalize_existing(source)
        destination_group = normalize_resource_group(destination) if destination else ""
        if not source_group or source_group not in self.list_resource_folders():
            raise ValueError(f"Folder '{source}' no longer exists.")

        source_parts = split_resource_path(source_group)
        destination_parts = split_resource_path(destination_group)
        if (
            len(destination_parts) > len(source_parts)
            and all(
                left.casefold() == right.casefold()
                for left, right in zip(destination_parts, source_parts)
            )
        ):
            raise ValueError("A folder cannot be moved inside itself.")
        if source_group.casefold() == destination_group.casefold() and source_group == destination_group:
            return {"flow": [], "image": [], "visual_rule": [], "run_config": []}

        flow_names = self.list_flows()
        image_names = [str(item["name"]) for item in self.list_images()]
        rule_names = self.list_visual_rules()
        flow_changes = {
            name: self._move_path_between_folders(name, source_group, destination_group)
            for name in flow_names
            if self._path_in_folder(name, source_group)
        }
        image_changes = {
            name: self._move_path_between_folders(name, source_group, destination_group)
            for name in image_names
            if self._path_in_folder(name, source_group)
        }
        rule_changes = {
            name: self._move_path_between_folders(name, source_group, destination_group)
            for name in rule_names
            if self._path_in_folder(name, source_group)
        }
        self._validate_folder_move_names(flow_names, flow_changes, resource_label="flow")
        self._validate_folder_move_names(image_names, image_changes, resource_label="image")
        self._validate_folder_move_names(rule_names, rule_changes, resource_label="visual rule")

        run_changes: dict[str, str] = {}
        for name in self.list_run_configs():
            config = self.load_run_config(name)
            if config is not None and self._path_in_folder(
                config.group_path, source_group, path_is_group=True
            ):
                run_changes[name] = self._move_path_between_folders(
                    config.group_path,
                    source_group,
                    destination_group,
                    path_is_group=True,
                )

        conn = self._conn
        now = _now_iso()
        with conn:
            for old_name, new_name in flow_changes.items():
                row = conn.execute(
                    "SELECT flow_id, data FROM flows WHERE name = ?", (old_name,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"Flow '{old_name}' no longer exists.")
                data = json.loads(row["data"])
                data["name"] = new_name
                renamed_data = json.dumps(data)
                for version in conn.execute(
                    "SELECT id, data FROM flow_versions WHERE flow_id = ?",
                    (row["flow_id"],),
                ).fetchall():
                    version_data = json.loads(version["data"])
                    version_data["name"] = new_name
                    conn.execute(
                        "UPDATE flow_versions SET flow_name = ?, data = ? WHERE id = ?",
                        (new_name, json.dumps(version_data), version["id"]),
                    )
                conn.execute(
                    """
                    INSERT INTO flow_versions
                        (flow_id, flow_name, data, change_type, saved_at)
                    VALUES (?, ?, ?, 'rename', ?)
                    """,
                    (row["flow_id"], new_name, renamed_data, now),
                )
                conn.execute(
                    "UPDATE flows SET name = ?, data = ?, updated_at = ? WHERE name = ?",
                    (new_name, renamed_data, now, old_name),
                )
                self._trim_flow_versions(row["flow_id"])

            for old_name, new_name in flow_changes.items():
                self._update_flow_references(conn, old_name, new_name, ("flow_name",))
                self._update_flow_version_references(
                    conn, old_name, new_name, ("flow_name",)
                )
                for row in conn.execute("SELECT name, data FROM run_configs").fetchall():
                    data = json.loads(row["data"])
                    if data.get("flow_name") == old_name:
                        data["flow_name"] = new_name
                        conn.execute(
                            "UPDATE run_configs SET data = ?, updated_at = ? WHERE name = ?",
                            (json.dumps(data), now, row["name"]),
                        )

            for old_name, new_name in image_changes.items():
                conn.execute("UPDATE images SET name = ? WHERE name = ?", (new_name, old_name))
                self._update_flow_references(
                    conn, old_name, new_name, ("image_name",), ("images",)
                )
                self._update_flow_version_references(
                    conn, old_name, new_name, ("image_name",), ("images",)
                )

            for old_name, new_name in rule_changes.items():
                row = conn.execute(
                    "SELECT data FROM visual_rules WHERE name = ?", (old_name,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"Visual rule '{old_name}' no longer exists.")
                data = json.loads(row["data"])
                data["name"] = new_name
                conn.execute(
                    "UPDATE visual_rules SET name = ?, data = ?, updated_at = ? WHERE name = ?",
                    (new_name, json.dumps(data), now, old_name),
                )
                self._update_flow_references(
                    conn, old_name, new_name, ("target_rule", "bonus_rule")
                )
                self._update_flow_version_references(
                    conn, old_name, new_name, ("target_rule", "bonus_rule")
                )

            for config_name, new_group in run_changes.items():
                row = conn.execute(
                    "SELECT data FROM run_configs WHERE name = ?", (config_name,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"Run setup '{config_name}' no longer exists.")
                data = json.loads(row["data"])
                data["group_path"] = new_group
                conn.execute(
                    "UPDATE run_configs SET data = ?, updated_at = ? WHERE name = ?",
                    (json.dumps(data), now, config_name),
                )

        return {
            "flow": list(flow_changes.items()),
            "image": list(image_changes.items()),
            "visual_rule": list(rule_changes.items()),
            "run_config": [
                (name, name) for name in run_changes
            ],
        }

    # -- Visual Rule CRUD --------------------------------------------------

    def save_visual_rule(self, rule: VisualRule) -> None:
        self._assert_no_name_collision(
            "visual_rules",
            rule.name,
            resource_label="visual rule",
            ignore_exact=rule.name,
        )
        now = _now_iso()
        data = rule.to_dict()
        data["name"] = rule.name
        self._conn.execute(
            """
            INSERT INTO visual_rules (name, data, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (rule.name, json.dumps(data), now, now),
        )
        self._conn.commit()

    def load_visual_rule(self, name: str) -> VisualRule | None:
        row = self._conn.execute(
            "SELECT data FROM visual_rules WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["data"])
        data["name"] = name
        return VisualRule.from_dict(data)

    def list_visual_rules(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM visual_rules ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def delete_visual_rule(self, name: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM visual_rules WHERE name = ?", (name,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def rename_visual_rule(self, old_name: str, new_name: str) -> bool:
        conn = self._conn
        row = conn.execute(
            "SELECT data FROM visual_rules WHERE name = ?", (old_name,)
        ).fetchone()
        if row is None:
            return False
        self._assert_no_name_collision(
            "visual_rules",
            new_name,
            resource_label="visual rule",
            ignore_exact=old_name,
        )
        data = json.loads(row["data"])
        data["name"] = new_name
        now = _now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO visual_rules (name, data, created_at, updated_at)
                SELECT ?, ?, created_at, ? FROM visual_rules WHERE name = ?
                """,
                (new_name, json.dumps(data), now, old_name),
            )
            conn.execute("DELETE FROM visual_rules WHERE name = ?", (old_name,))
            self._update_flow_references(
                conn, old_name, new_name, ("target_rule", "bonus_rule")
            )
            self._update_flow_version_references(
                conn, old_name, new_name, ("target_rule", "bonus_rule")
            )
        return True

    def rename_run_config(self, old_name: str, new_name: str) -> bool:
        """Atomically rename a saved run configuration."""
        config = self.load_run_config(old_name)
        if config is None:
            return False
        config.name = new_name
        return self.update_run_config(old_name, config)

    def update_run_config(self, old_name: str, config: SavedRunConfig) -> bool:
        """Atomically update a run setup, including a possible identity change."""
        conn = self._conn
        if conn.execute(
            "SELECT 1 FROM run_configs WHERE name = ?", (old_name,)
        ).fetchone() is None:
            return False
        self._assert_no_name_collision(
            "run_configs",
            config.name,
            resource_label="run setup",
            ignore_exact=old_name,
        )
        data = config.to_dict()
        data["group_path"] = self.resolve_resource_folder(
            config.group_path,
            ignore_run_config=old_name,
            candidate_is_run_group=True,
        )
        with conn:
            cursor = conn.execute(
                "UPDATE run_configs SET name = ?, data = ?, updated_at = ? WHERE name = ?",
                (
                    config.name,
                    json.dumps(data),
                    _now_iso(),
                    old_name,
                ),
            )
        return cursor.rowcount > 0

    # ── Flow CRUD ──────────────────────────────────────────────────────

    @staticmethod
    def _flow_from_persisted_row(
        flow_id: object,
        raw_data: object,
        *,
        label: str,
    ) -> AutomationFlow:
        try:
            data = json.loads(raw_data)
            flow = AutomationFlow.from_dict(data)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DatabaseSchemaError(f"{label} contains invalid flow data.") from exc
        if flow.flow_id != flow_id:
            raise DatabaseSchemaError(
                f"{label} identity does not match its serialized data."
            )
        return flow

    def save_flow(self, name: str, flow: AutomationFlow) -> None:
        self._assert_no_name_collision(
            "flows",
            name,
            resource_label="flow",
            ignore_exact=name,
        )
        now = _now_iso()
        data = flow.to_dict()
        data["name"] = name
        # Keep the persistence boundary independent of Qt and target state, but
        # never store a tree that the model itself cannot load again. Semantic
        # issues remain saveable as drafts and are reported by flow preflight.
        validated_flow = AutomationFlow.from_dict(data)
        existing = self._conn.execute(
            "SELECT flow_id FROM flows WHERE name = ?",
            (name,),
        ).fetchone()
        if existing is not None and existing["flow_id"] != validated_flow.flow_id:
            raise ValueError(
                f"Flow '{name}' cannot change its stable identity while being updated."
            )
        identity_owner = self._conn.execute(
            "SELECT name FROM flows WHERE flow_id = ?",
            (validated_flow.flow_id,),
        ).fetchone()
        if identity_owner is not None and identity_owner["name"] != name:
            raise ValueError(
                f"Flow identity '{validated_flow.flow_id}' already belongs to "
                f"'{identity_owner['name']}'."
            )
        data_json = json.dumps(data)
        self._backup_existing_flow(name, data_json, "update")
        self._conn.execute(
            """
            INSERT INTO flows (flow_id, name, data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at
            """,
            (validated_flow.flow_id, name, data_json, now, now),
        )
        self._conn.commit()

    def load_flow(self, name: str) -> AutomationFlow | None:
        row = self._conn.execute(
            "SELECT flow_id, data FROM flows WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return self._flow_from_persisted_row(
            row["flow_id"],
            row["data"],
            label=f"Flow '{name}'",
        )

    def load_flow_by_id(self, flow_id: str) -> AutomationFlow | None:
        """Load the current flow owning one stable UUID, independent of its name."""
        try:
            canonical_id = str(uuid.UUID(flow_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Flow ID must be a UUID string.") from exc
        row = self._conn.execute(
            "SELECT name, flow_id, data FROM flows WHERE flow_id = ?",
            (canonical_id,),
        ).fetchone()
        if row is None:
            return None
        return self._flow_from_persisted_row(
            row["flow_id"],
            row["data"],
            label=f"Flow '{row['name']}'",
        )

    def list_flows(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM flows ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def flow_references(self, name: str) -> dict[str, list[str]]:
        """Return saved runs and flows that directly reference a flow.

        Current Run Flow commands bind the child's stable UUID.  Legacy flows
        used the mutable name, so both forms remain part of the compatibility
        boundary until every old document has been migrated.
        """
        target = self._conn.execute(
            "SELECT flow_id FROM flows WHERE name = ?", (name,)
        ).fetchone()
        target_flow_id = str(target["flow_id"]) if target is not None else ""
        calling_flows: list[str] = []
        for row in self._conn.execute("SELECT name, data FROM flows").fetchall():
            if row["name"] == name:
                continue
            data = json.loads(row["data"])
            if self._flow_data_commands_reference(
                data, "flow_name", name
            ) or (
                target_flow_id
                and self._flow_data_commands_reference(
                    data, "flow_id", target_flow_id
                )
            ):
                calling_flows.append(str(row["name"]))

        run_configs: list[str] = []
        for row in self._conn.execute("SELECT name, data FROM run_configs").fetchall():
            data = json.loads(row["data"])
            if data.get("flow_name") == name:
                run_configs.append(str(row["name"]))
        return {
            "flows": sorted(calling_flows, key=str.casefold),
            "run_configs": sorted(run_configs, key=str.casefold),
        }

    def visual_rule_references(self, name: str) -> list[str]:
        """Return flows whose commands or hook actions use a visual rule."""
        references: list[str] = []
        for row in self._conn.execute("SELECT name, data FROM flows").fetchall():
            data = json.loads(row["data"])
            if any(
                self._flow_data_commands_reference(data, key, name)
                for key in ("target_rule", "bonus_rule")
            ):
                references.append(str(row["name"]))
        return sorted(references, key=str.casefold)

    def delete_flow(self, name: str, *, force: bool = False) -> bool:
        references = self.flow_references(name)
        if not force and (references["flows"] or references["run_configs"]):
            raise ValueError(f"Flow '{name}' is still referenced by other resources.")
        self._backup_existing_flow(name, None, "delete")
        cursor = self._conn.execute("DELETE FROM flows WHERE name = ?", (name,))
        self._conn.commit()
        return cursor.rowcount > 0

    def rename_flow(self, old_name: str, new_name: str) -> bool:
        conn = self._conn
        row = conn.execute(
            "SELECT flow_id, data FROM flows WHERE name = ?", (old_name,)
        ).fetchone()
        if row is None:
            return False
        self._assert_no_name_collision(
            "flows",
            new_name,
            resource_label="flow",
            ignore_exact=old_name,
        )

        now = _now_iso()
        data = json.loads(row["data"])
        data["name"] = new_name
        commands = data.get("commands", [])
        if isinstance(commands, list):
            self._replace_command_references(
                commands, old_name, new_name, ("flow_name",)
            )
        renamed_data = json.dumps(data)
        with conn:
            version_rows = conn.execute(
                "SELECT id, data FROM flow_versions WHERE flow_id = ?",
                (row["flow_id"],),
            ).fetchall()
            for version_row in version_rows:
                version_data = json.loads(version_row["data"])
                version_data["name"] = new_name
                conn.execute(
                    "UPDATE flow_versions SET flow_name = ?, data = ? WHERE id = ?",
                    (new_name, json.dumps(version_data), version_row["id"]),
                )
            conn.execute(
                """
                INSERT INTO flow_versions
                    (flow_id, flow_name, data, change_type, saved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (row["flow_id"], new_name, renamed_data, "rename", now),
            )
            conn.execute(
                """
                UPDATE flows
                SET name = ?, data = ?, updated_at = ?
                WHERE name = ?
                """,
                (new_name, renamed_data, now, old_name),
            )
            self._trim_flow_versions(row["flow_id"])
            self._update_flow_references(conn, old_name, new_name, ("flow_name",))
            self._update_flow_version_references(
                conn, old_name, new_name, ("flow_name",)
            )
            for config_row in conn.execute(
                "SELECT name, data FROM run_configs"
            ).fetchall():
                config_data = json.loads(config_row["data"])
                if config_data.get("flow_name") != old_name:
                    continue
                config_data["flow_name"] = new_name
                conn.execute(
                    "UPDATE run_configs SET data = ?, updated_at = ? WHERE name = ?",
                    (json.dumps(config_data), now, config_row["name"]),
                )
        return True

    def list_flow_versions(self, name: str) -> list[dict[str, Any]]:
        current = self._conn.execute(
            "SELECT flow_id FROM flows WHERE name = ?",
            (name,),
        ).fetchone()
        if current is None:
            return []
        rows = self._conn.execute(
            """
            SELECT id, flow_name, change_type, saved_at
            FROM flow_versions
            WHERE flow_id = ?
            ORDER BY saved_at DESC, id DESC
            """,
            (current["flow_id"],),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "flow_name": r["flow_name"],
                "change_type": r["change_type"],
                "saved_at": r["saved_at"],
            }
            for r in rows
        ]

    def load_flow_version(self, version_id: int) -> AutomationFlow | None:
        row = self._conn.execute(
            "SELECT flow_id, data FROM flow_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if row is None:
            return None
        return self._flow_from_persisted_row(
            row["flow_id"],
            row["data"],
            label=f"Flow version {version_id}",
        )

    # ── Image CRUD ─────────────────────────────────────────────────────

    @staticmethod
    def _image_identity(name: Any) -> str:
        return normalize_resource_path(name).casefold()

    @staticmethod
    def _image_revision_from_row(row: sqlite3.Row) -> ImageRevision:
        payload = bytes(row["data"])
        return ImageRevision(
            name=str(row["name"]),
            width=int(row["width"]),
            height=int(row["height"]),
            sha256=hashlib.sha256(payload).hexdigest(),
            source_surface_width=int(row["source_surface_width"]),
            source_surface_height=int(row["source_surface_height"]),
            source_coordinate_space=str(row["source_coordinate_space"]),
        )

    def image_revision(self, name: str) -> ImageRevision | None:
        """Resolve one image by normalized, case-insensitive identity.

        Legacy databases can theoretically contain two spellings of the same
        portable identity. That ambiguity fails closed instead of selecting an
        arbitrary row.
        """
        target = self._image_identity(name)
        matches = [
            str(row["name"])
            for row in self._conn.execute("SELECT name FROM images").fetchall()
            if self._image_identity(row["name"]) == target
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ImageRevisionConflictError(
                "Multiple stored images share the requested portable identity."
            )
        row = self._conn.execute(
            """
            SELECT name, data, width, height, source_surface_width,
                   source_surface_height, source_coordinate_space
            FROM images
            WHERE name = ?
            """,
            (matches[0],),
        ).fetchone()
        if row is None:  # pragma: no cover - same-connection read cannot race
            raise ImageRevisionConflictError(
                "The requested image changed while its revision was read."
            )
        return self._image_revision_from_row(row)

    def create_image(
        self,
        name: str,
        image_bytes: bytes,
        width: int = 0,
        height: int = 0,
        *,
        source_surface_width: int = 0,
        source_surface_height: int = 0,
        source_coordinate_space: str = "",
    ) -> ImageRevision:
        """Create a canonically named image without replacing any collision."""
        canonical_name = normalize_resource_path(name)
        error = resource_path_error(canonical_name)
        if error:
            raise ValueError(error)

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            target = self._image_identity(canonical_name)
            collision = next(
                (
                    str(row["name"])
                    for row in conn.execute("SELECT name FROM images").fetchall()
                    if self._image_identity(row["name"]) == target
                ),
                None,
            )
            if collision is not None:
                raise ImageAlreadyExistsError(
                    f"An image named '{collision}' already exists."
                )
            conn.execute(
                """
                INSERT INTO images (
                    name, data, width, height, created_at,
                    source_surface_width, source_surface_height,
                    source_coordinate_space
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_name,
                    bytes(image_bytes),
                    int(width),
                    int(height),
                    _now_iso(),
                    int(source_surface_width),
                    int(source_surface_height),
                    str(source_coordinate_space),
                ),
            )
            row = conn.execute(
                """
                SELECT name, data, width, height, source_surface_width,
                       source_surface_height, source_coordinate_space
                FROM images
                WHERE name = ?
                """,
                (canonical_name,),
            ).fetchone()
            if row is None:  # pragma: no cover - guarded by the preceding INSERT
                raise RuntimeError("The image was not created.")
            revision = self._image_revision_from_row(row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return revision

    def create_images(self, images: list[StoredImage]) -> tuple[ImageRevision, ...]:
        """Create a target pose set atomically, rejecting any portable collision."""
        if not images:
            raise ValueError("At least one image is required.")
        canonical: list[StoredImage] = []
        identities: set[str] = set()
        for image in images:
            name = normalize_resource_path(image.name)
            error = resource_path_error(name)
            if error:
                raise ValueError(error)
            identity = self._image_identity(name)
            if identity in identities:
                raise ImageAlreadyExistsError(
                    "The target recording contains duplicate image names."
                )
            identities.add(identity)
            canonical.append(
                StoredImage(
                    name=name,
                    data=bytes(image.data),
                    width=int(image.width),
                    height=int(image.height),
                    source_surface_width=int(image.source_surface_width),
                    source_surface_height=int(image.source_surface_height),
                    source_coordinate_space=str(image.source_coordinate_space),
                )
            )

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = {
                self._image_identity(str(row["name"]))
                for row in conn.execute("SELECT name FROM images").fetchall()
            }
            collision = next(
                (image.name for image in canonical if self._image_identity(image.name) in existing),
                None,
            )
            if collision is not None:
                raise ImageAlreadyExistsError(
                    f"An image named '{collision}' already exists."
                )
            now = _now_iso()
            for image in canonical:
                conn.execute(
                    """
                    INSERT INTO images (
                        name, data, width, height, created_at,
                        source_surface_width, source_surface_height,
                        source_coordinate_space
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        image.name,
                        image.data,
                        image.width,
                        image.height,
                        now,
                        image.source_surface_width,
                        image.source_surface_height,
                        image.source_coordinate_space,
                    ),
                )
            revisions = []
            for image in canonical:
                row = conn.execute(
                    """
                    SELECT name, data, width, height, source_surface_width,
                           source_surface_height, source_coordinate_space
                    FROM images WHERE name = ?
                    """,
                    (image.name,),
                ).fetchone()
                if row is None:  # pragma: no cover - guarded by INSERT
                    raise RuntimeError("A target pose was not created.")
                revisions.append(self._image_revision_from_row(row))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return tuple(revisions)

    def replace_image_if_revision(
        self,
        expected: ImageRevision,
        image_bytes: bytes,
        width: int = 0,
        height: int = 0,
        *,
        source_surface_width: int = 0,
        source_surface_height: int = 0,
        source_coordinate_space: str = "",
    ) -> ImageRevision:
        """Replace exactly the reviewed image if its content is unchanged."""
        if not isinstance(expected, ImageRevision):
            raise TypeError("expected must be an ImageRevision")

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT name, data, width, height, source_surface_width,
                       source_surface_height, source_coordinate_space
                FROM images
                WHERE name = ?
                """,
                (expected.name,),
            ).fetchone()
            if row is None or self._image_revision_from_row(row) != expected:
                raise ImageRevisionConflictError(
                    "The reviewed image changed or no longer exists."
                )
            conn.execute(
                """
                UPDATE images
                SET data = ?, width = ?, height = ?,
                    source_surface_width = ?, source_surface_height = ?,
                    source_coordinate_space = ?
                WHERE name = ?
                """,
                (
                    bytes(image_bytes),
                    int(width),
                    int(height),
                    int(source_surface_width),
                    int(source_surface_height),
                    str(source_coordinate_space),
                    expected.name,
                ),
            )
            updated_row = conn.execute(
                """
                SELECT name, data, width, height, source_surface_width,
                       source_surface_height, source_coordinate_space
                FROM images
                WHERE name = ?
                """,
                (expected.name,),
            ).fetchone()
            if updated_row is None:  # pragma: no cover - protected by write lock
                raise ImageRevisionConflictError(
                    "The reviewed image no longer exists."
                )
            revision = self._image_revision_from_row(updated_row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return revision

    def save_image(
        self,
        name: str,
        image_bytes: bytes,
        width: int = 0,
        height: int = 0,
        *,
        source_surface_width: int = 0,
        source_surface_height: int = 0,
        source_coordinate_space: str = "",
    ) -> None:
        self._assert_no_name_collision(
            "images",
            name,
            resource_label="image",
            ignore_exact=name,
        )
        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO images (
                name, data, width, height, created_at,
                source_surface_width, source_surface_height,
                source_coordinate_space
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET data=excluded.data, width=excluded.width,
                height=excluded.height,
                source_surface_width=excluded.source_surface_width,
                source_surface_height=excluded.source_surface_height,
                source_coordinate_space=excluded.source_coordinate_space
            """,
            (
                name,
                image_bytes,
                width,
                height,
                now,
                int(source_surface_width),
                int(source_surface_height),
                str(source_coordinate_space),
            ),
        )
        self._conn.commit()

    def load_image(self, name: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT data FROM images WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return bytes(row["data"])

    def load_image_asset(self, name: str) -> StoredImage | None:
        row = self._conn.execute(
            """
            SELECT name, data, width, height, source_surface_width,
                   source_surface_height, source_coordinate_space
            FROM images
            WHERE name = ?
            """,
            (name,),
        ).fetchone()
        if row is None:
            return None
        return StoredImage(
            name=str(row["name"]),
            data=bytes(row["data"]),
            width=int(row["width"]),
            height=int(row["height"]),
            source_surface_width=int(row["source_surface_width"]),
            source_surface_height=int(row["source_surface_height"]),
            source_coordinate_space=str(row["source_coordinate_space"]),
        )

    def list_images(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT name, width, height, created_at, source_surface_width,
                      source_surface_height, source_coordinate_space
                 FROM images ORDER BY name"""
        ).fetchall()
        return [
            {
                "name": r["name"],
                "width": r["width"],
                "height": r["height"],
                "created_at": r["created_at"],
                "source_surface_width": r["source_surface_width"],
                "source_surface_height": r["source_surface_height"],
                "source_coordinate_space": r["source_coordinate_space"],
            }
            for r in rows
        ]

    def delete_image(self, name: str) -> bool:
        cursor = self._conn.execute("DELETE FROM images WHERE name = ?", (name,))
        self._conn.commit()
        return cursor.rowcount > 0

    def image_references(self, name: str) -> list[str]:
        """Return saved flows recursively referencing an image identity."""
        target = self._image_identity(name)

        def commands_reference_image(commands: list[dict[str, Any]]) -> bool:
            for command in commands:
                params = command.get("params", {})
                if isinstance(params, dict):
                    scalar = params.get("image_name")
                    if (
                        isinstance(scalar, str)
                        and self._image_identity(scalar) == target
                    ):
                        return True
                    images = params.get("images")
                    if isinstance(images, list) and any(
                        isinstance(value, str)
                        and self._image_identity(value) == target
                        for value in images
                    ):
                        return True
                for child_key in ("children", "else_children"):
                    children = command.get(child_key, [])
                    if isinstance(children, list) and commands_reference_image(
                        children
                    ):
                        return True
            return False

        references: list[str] = []
        for row in self._conn.execute("SELECT name, data FROM flows").fetchall():
            data = json.loads(row["data"])
            commands = data.get("commands", [])
            hooks = data.get("hooks", [])
            hook_reference = False
            if isinstance(hooks, list):
                for hook in hooks:
                    if not isinstance(hook, dict):
                        continue
                    condition = hook.get("condition", {})
                    if isinstance(condition, dict):
                        images = condition.get("images", [])
                        settings = condition.get("search_settings", {})
                        anchors = (
                            settings.get("search_anchor_images", [])
                            if isinstance(settings, dict)
                            else []
                        )
                        images = images if isinstance(images, list) else []
                        anchors = anchors if isinstance(anchors, list) else []
                        if any(
                            isinstance(value, str)
                            and self._image_identity(value) == target
                            for value in [*images, *anchors]
                        ):
                            hook_reference = True
                            break
                    actions = hook.get("actions", [])
                    if isinstance(actions, list) and commands_reference_image(actions):
                        hook_reference = True
                        break
            if (
                isinstance(commands, list)
                and commands_reference_image(commands)
            ) or hook_reference:
                references.append(str(row["name"]))
        return sorted(references, key=str.casefold)

    def rename_image(self, old_name: str, new_name: str) -> bool:
        conn = self._conn
        self._assert_no_name_collision(
            "images",
            new_name,
            resource_label="image",
            ignore_exact=old_name,
        )
        with conn:
            cursor = conn.execute(
                "UPDATE images SET name = ? WHERE name = ?", (new_name, old_name)
            )
            if cursor.rowcount:
                self._update_flow_references(
                    conn, old_name, new_name, ("image_name",), ("images",)
                )
                self._update_flow_version_references(
                    conn, old_name, new_name, ("image_name",), ("images",)
                )
        return cursor.rowcount > 0

    def get_image_thumbnail(self, name: str, size: int = 64) -> bytes | None:
        row = self._conn.execute(
            "SELECT data FROM images WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        image_data = bytes(row["data"])
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(image_data))
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except ImportError:
            return image_data

    # ── Managed emulator profile CRUD ──────────────────────────────────

    def save_managed_emulator_profile(
        self,
        profile: ManagedEmulatorProfile,
    ) -> None:
        if not isinstance(profile, ManagedEmulatorProfile):
            raise TypeError("Profile must be a ManagedEmulatorProfile.")
        name_collision = self._conn.execute(
            """
            SELECT profile_id
              FROM managed_emulator_profiles
             WHERE name = ? COLLATE NOCASE AND profile_id <> ?
             LIMIT 1
            """,
            (profile.name, profile.profile_id),
        ).fetchone()
        if name_collision is not None:
            raise ValueError(
                f"A managed emulator profile named '{profile.name}' already exists."
            )
        target_collision = self._conn.execute(
            """
            SELECT name
              FROM managed_emulator_profiles
             WHERE target_key = ? AND profile_id <> ?
             LIMIT 1
            """,
            (profile.target_key, profile.profile_id),
        ).fetchone()
        if target_collision is not None:
            raise ValueError(
                "This emulator target is already managed by profile "
                f"'{target_collision['name']}'."
            )
        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO managed_emulator_profiles
                (profile_id, name, target_key, data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                name=excluded.name,
                target_key=excluded.target_key,
                data=excluded.data,
                updated_at=excluded.updated_at
            """,
            (
                profile.profile_id,
                profile.name,
                profile.target_key,
                json.dumps(profile.to_dict()),
                now,
                now,
            ),
        )
        self._conn.commit()

    def get_managed_emulator_profile(
        self,
        profile_id: str,
    ) -> ManagedEmulatorProfile | None:
        row = self._conn.execute(
            "SELECT data FROM managed_emulator_profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        if row is None:
            return None
        return ManagedEmulatorProfile.from_dict(json.loads(row["data"]))

    def get_managed_emulator_profile_by_name(
        self,
        name: str,
    ) -> ManagedEmulatorProfile | None:
        row = self._conn.execute(
            """
            SELECT data
              FROM managed_emulator_profiles
             WHERE name = ? COLLATE NOCASE
            """,
            (name,),
        ).fetchone()
        if row is None:
            return None
        return ManagedEmulatorProfile.from_dict(json.loads(row["data"]))

    def list_managed_emulator_profiles(self) -> list[ManagedEmulatorProfile]:
        rows = self._conn.execute(
            """
            SELECT data
              FROM managed_emulator_profiles
             ORDER BY name COLLATE NOCASE, profile_id
            """
        ).fetchall()
        return [
            ManagedEmulatorProfile.from_dict(json.loads(row["data"]))
            for row in rows
        ]

    def list_run_configs_referencing_managed_emulator(
        self,
        profile_id: str,
    ) -> list[SavedRunConfig]:
        references: list[SavedRunConfig] = []
        for run_config_name in self.list_run_configs():
            try:
                config = self.load_run_config(run_config_name)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                config is not None
                and config.managed_emulator_profile_id == profile_id
            ):
                references.append(config)
        return references

    def delete_managed_emulator_profile(self, profile_id: str) -> bool:
        if self.list_run_configs_referencing_managed_emulator(profile_id):
            return False
        cursor = self._conn.execute(
            "DELETE FROM managed_emulator_profiles WHERE profile_id = ?",
            (profile_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ── RunConfig CRUD ─────────────────────────────────────────────────

    def save_run_config(self, name: str, config: SavedRunConfig) -> None:
        self._assert_no_name_collision(
            "run_configs",
            name,
            resource_label="run setup",
            ignore_exact=name,
        )
        now = _now_iso()
        data = config.to_dict()
        data["name"] = name
        data["group_path"] = self.resolve_resource_folder(
            config.group_path,
            ignore_run_config=name,
            candidate_is_run_group=True,
        )
        data_json = json.dumps(data)
        self._conn.execute(
            """
            INSERT INTO run_configs (name, data, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at
            """,
            (name, data_json, now, now),
        )
        self._conn.commit()

    def load_run_config(self, name: str) -> SavedRunConfig | None:
        row = self._conn.execute(
            "SELECT data FROM run_configs WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return SavedRunConfig.from_dict(json.loads(row["data"]))

    def load_run_config_by_id(self, run_config_id: int) -> SavedRunConfig | None:
        if type(run_config_id) is not int or run_config_id < 1:
            return None
        row = self._conn.execute(
            "SELECT data FROM run_configs WHERE id = ?",
            (run_config_id,),
        ).fetchone()
        if row is None:
            return None
        return SavedRunConfig.from_dict(json.loads(row["data"]))

    def list_run_configs(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM run_configs ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def get_run_config_id(self, name: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM run_configs WHERE name = ?",
            (name,),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def run_config_references(self, name: str) -> list[str]:
        """Return schedule names that depend on a saved run setup."""
        run_config_id = self.get_run_config_id(name)
        if run_config_id is None:
            return []
        rows = self._conn.execute(
            "SELECT name FROM schedules WHERE run_config_id = ? ORDER BY name",
            (run_config_id,),
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def delete_run_config(self, name: str) -> bool:
        run_config_id = self.get_run_config_id(name)
        if run_config_id is None:
            return False
        if self._conn.execute(
            "SELECT 1 FROM schedules WHERE run_config_id = ? LIMIT 1",
            (run_config_id,),
        ).fetchone() is not None:
            return False
        cursor = self._conn.execute("DELETE FROM run_configs WHERE name = ?", (name,))
        self._conn.commit()
        return cursor.rowcount > 0

    def list_run_configs_referencing_profile(
        self, profile_identity: str
    ) -> list[SavedRunConfig]:
        """Return saved runs targeting a stable UUID or compatible legacy name.

        Run setup device IDs have had more than one serialized shape. Delegate
        that compatibility boundary to the device model rather than searching
        JSON text.  A profile's current name and unresolved legacy storage name
        remain aliases only for old saved IDs; stable IDs survive renames.
        """
        from automadin.models.device import (
            browser_profile_reference_from_saved_device_id,
        )
        from automadin.services.browser_profile_paths import canonical_profile_id

        references: list[SavedRunConfig] = []
        identity_text = str(profile_identity)
        legacy_names = {identity_text.casefold()}
        try:
            target_profile_id: str | None = canonical_profile_id(identity_text)
            record = self.get_profile(target_profile_id)
        except ValueError:
            record = self.get_profile_by_name(identity_text)
            target_profile_id = record.profile_id if record is not None else None
        if record is not None:
            legacy_names.add(record.name.casefold())
            if record.legacy_storage_name:
                legacy_names.add(record.legacy_storage_name.casefold())

        for run_config_name in self.list_run_configs():
            try:
                config = self.load_run_config(run_config_name)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if config is None:
                continue
            reference = browser_profile_reference_from_saved_device_id(
                config.device_id
            )
            if reference is None:
                continue
            stable_match = (
                target_profile_id is not None
                and reference.profile_id == target_profile_id
            )
            legacy_match = (
                reference.legacy_name is not None
                and reference.legacy_name.casefold() in legacy_names
            )
            if stable_match or legacy_match:
                references.append(config)
        return references

    # ── Profile CRUD ───────────────────────────────────────────────────

    @staticmethod
    def _validated_profile_display_name(name: object) -> str:
        """Validate a human label without treating it as a filesystem path."""
        if not isinstance(name, str):
            raise TypeError("Browser profile name must be text.")
        label = name.strip()
        if not label:
            raise ValueError("Browser profile name cannot be empty.")
        if len(label) > 200:
            raise ValueError("Browser profile name cannot exceed 200 characters.")
        if any(ord(character) < 32 or ord(character) == 127 for character in label):
            raise ValueError("Browser profile name cannot contain control characters.")
        return label

    @staticmethod
    def _validated_profile_engine(engine: object, *, allow_empty: bool = True) -> str:
        from automadin.services.browser_profile_manifest import (
            SUPPORTED_PROFILE_ENGINES,
        )

        if not isinstance(engine, str):
            raise TypeError("Browser profile engine must be text.")
        if not engine and allow_empty:
            return ""
        if engine not in SUPPORTED_PROFILE_ENGINES:
            supported = ", ".join(sorted(SUPPORTED_PROFILE_ENGINES))
            raise ValueError(f"Browser profile engine must be one of: {supported}.")
        return engine

    @staticmethod
    def _validated_profile_identity_text(value: object, field_name: str) -> str:
        from automadin.services.browser_profile_metadata import (
            _validated_identity_text,
        )

        return _validated_identity_text(value, field_name)

    def _validated_legacy_storage_name(self, name: object) -> str:
        if not isinstance(name, str) or not name:
            raise ValueError("Legacy browser profile storage name cannot be empty.")
        # This compatibility resolver is deliberately used only for an explicit
        # legacy marker.  Display labels themselves may contain path punctuation.
        resolve_managed_profile_path(name, self._managed_profiles_root())
        return name

    def _assert_profile_name_available(
        self,
        name: str,
        *,
        ignore_profile_id: str | None = None,
    ) -> None:
        folded_name = name.casefold()
        for row in self._conn.execute(
            "SELECT profile_id, name FROM profiles"
        ).fetchall():
            if ignore_profile_id is not None and row["profile_id"] == ignore_profile_id:
                continue
            if str(row["name"]).casefold() == folded_name:
                raise ValueError(
                    f"A browser profile named '{row['name']}' already exists."
                )

    @staticmethod
    def _stored_profile_from_row(row: sqlite3.Row):
        from automadin.services.browser_profile_metadata import (
            BrowserProfileIdentity,
            StoredBrowserProfile,
        )

        return StoredBrowserProfile(
            profile_id=str(row["profile_id"]),
            name=str(row["name"]),
            created_at=str(row["created_at"]),
            identity=BrowserProfileIdentity(
                browser=str(row["browser"]),
                source_profile=str(row["source_profile"]),
                account_name=str(row["account_name"]),
                account_email=str(row["account_email"]),
            ),
            engine=str(row["engine"]),
            legacy_storage_name=str(row["legacy_storage_name"]),
            identity_checked_at=str(row["identity_checked_at"]),
            identity_stale_mask=int(row["identity_stale_mask"]),
        )

    @staticmethod
    def _profile_select() -> str:
        return (
            "SELECT profile_id, name, legacy_storage_name, created_at, browser, "
            "source_profile, account_name, account_email, engine, "
            "identity_checked_at, identity_stale_mask FROM profiles"
        )

    def create_profile(
        self,
        name: str,
        *,
        profile_id: str | None = None,
        legacy_storage_name: str = "",
        browser: str = "",
        source_profile: str = "",
        account_name: str = "",
        account_email: str = "",
        engine: str = "",
    ):
        """Create one logical profile without creating or adopting storage."""
        from automadin.services.browser_profile_paths import canonical_profile_id

        name = self._validated_profile_display_name(name)
        profile_id = (
            str(uuid.uuid4())
            if profile_id is None
            else canonical_profile_id(profile_id)
        )
        legacy_storage_name = (
            self._validated_legacy_storage_name(legacy_storage_name)
            if legacy_storage_name
            else ""
        )
        engine = self._validated_profile_engine(engine)
        for field_name, value in (
            ("browser", browser),
            ("source_profile", source_profile),
            ("account_name", account_name),
            ("account_email", account_email),
        ):
            value = self._validated_profile_identity_text(value, field_name)

        self._assert_profile_name_available(name)
        if self._conn.execute(
            "SELECT 1 FROM profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone() is not None:
            raise ValueError(f"Browser profile ID '{profile_id}' already exists.")

        self._conn.execute(
            """
            INSERT INTO profiles (
                profile_id, name, legacy_storage_name, created_at, browser,
                source_profile, account_name, account_email, engine
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                name,
                legacy_storage_name,
                _now_iso(),
                browser,
                source_profile,
                account_name,
                account_email,
                engine,
            ),
        )
        self._conn.commit()
        record = self.get_profile(profile_id)
        if record is None:  # pragma: no cover - guarded by the successful insert
            raise RuntimeError("Browser profile was not readable after creation.")
        return record

    def get_profile(self, profile_id: str):
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(profile_id)
        row = self._conn.execute(
            f"{self._profile_select()} WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        return self._stored_profile_from_row(row) if row is not None else None

    def get_profile_by_name(self, name: str):
        name = self._validated_profile_display_name(name)
        row = self._conn.execute(
            f"{self._profile_select()} WHERE name = ?",
            (name,),
        ).fetchone()
        if row is not None:
            return self._stored_profile_from_row(row)
        matches = [
            row
            for row in self._conn.execute(self._profile_select()).fetchall()
            if str(row["name"]).casefold() == name.casefold()
        ]
        return self._stored_profile_from_row(matches[0]) if len(matches) == 1 else None

    def update_profile_metadata(
        self,
        profile_id: str,
        *,
        browser: str | None = None,
        source_profile: str | None = None,
        account_name: str | None = None,
        account_email: str | None = None,
        engine: str | None = None,
    ) -> bool:
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(profile_id)
        if self.get_profile(profile_id) is None:
            return False
        updates: dict[str, object] = {}
        stale_bits = 0
        for field_name, value in (
            ("browser", browser),
            ("source_profile", source_profile),
            ("account_name", account_name),
            ("account_email", account_email),
        ):
            if value is None:
                continue
            updates[field_name] = self._validated_profile_identity_text(
                value,
                field_name,
            )
            stale_bits |= {
                "source_profile": 1,
                "account_name": 2,
                "account_email": 4,
            }.get(field_name, 0)
        if engine is not None:
            updates["engine"] = self._validated_profile_engine(engine)
        if not updates and not stale_bits:
            return True
        assignments = [f"{column} = ?" for column in updates]
        parameters: list[object] = list(updates.values())
        if stale_bits:
            assignments.append("identity_stale_mask = identity_stale_mask | ?")
            parameters.append(stale_bits)
        self._conn.execute(
            f"UPDATE profiles SET {', '.join(assignments)} WHERE profile_id = ?",
            (*parameters, profile_id),
        )
        self._conn.commit()
        return True

    def apply_profile_identity_observation(
        self,
        profile_id: str,
        observation: ProfileIdentityObservation,
        expected_engine: str,
    ) -> StoredBrowserProfile | None:
        """Atomically apply a field-scoped identity observation.

        The browser label is engine provenance and deliberately is not part of
        the refresh mask.
        """
        from automadin.services.browser_profile_metadata import (
            ProfileIdentityField,
            ProfileIdentityObservation,
        )
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(profile_id)
        if not isinstance(observation, ProfileIdentityObservation):
            raise TypeError(
                "Profile identity observation must be ProfileIdentityObservation."
            )
        expected_engine = self._validated_profile_engine(
            expected_engine,
            allow_empty=False,
        )

        field_columns = (
            (ProfileIdentityField.SOURCE_PROFILE, "source_profile"),
            (ProfileIdentityField.ACCOUNT_NAME, "account_name"),
            (ProfileIdentityField.ACCOUNT_EMAIL, "account_email"),
        )
        update_mask = observation.update_mask
        attempted_mask = observation.attempted_mask
        verified_mask = observation.verified_mask
        updates: dict[str, object] = {}
        for bit, column in field_columns:
            if update_mask & bit:
                value = getattr(observation.identity, column)
                if not isinstance(value, str):  # defensive if model construction changes
                    raise TypeError(f"Browser profile {column} must be text.")
                updates[column] = value

        connection = self._conn
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT engine, identity_stale_mask
                  FROM profiles
                 WHERE profile_id = ?
                """,
                (profile_id,),
            ).fetchone()
            if row is None or str(row["engine"]) != expected_engine:
                connection.rollback()
                return None

            stale_mask = (
                (int(row["identity_stale_mask"]) | int(attempted_mask))
                & ~int(verified_mask)
                & int(ProfileIdentityField.ALL)
            )
            updates["browser"] = {
                "chromium": "Chromium",
                "chrome": "Chrome",
                "msedge": "Edge",
                "firefox": "Firefox",
            }[expected_engine]
            updates["identity_checked_at"] = observation.checked_at
            updates["identity_stale_mask"] = stale_mask
            assignments = ", ".join(f"{column} = ?" for column in updates)
            connection.execute(
                f"UPDATE profiles SET {assignments} WHERE profile_id = ?",
                (*updates.values(), profile_id),
            )
            result_row = connection.execute(
                f"{self._profile_select()} WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if result_row is None:  # guarded by the write transaction and row check
                raise RuntimeError(
                    "Browser profile disappeared during its identity update."
                )
            result = self._stored_profile_from_row(result_row)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

    def rename_profile(self, profile_id: str, new_name: str) -> bool:
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(profile_id)
        if self.get_profile(profile_id) is None:
            return False
        new_name = self._validated_profile_display_name(new_name)
        self._assert_profile_name_available(
            new_name,
            ignore_profile_id=profile_id,
        )
        self._conn.execute(
            "UPDATE profiles SET name = ? WHERE profile_id = ?",
            (new_name, profile_id),
        )
        self._conn.commit()
        return True

    def finalize_profile_storage(
        self,
        profile_id: str,
        *,
        engine: str,
        browser: str | None = None,
        source_profile: str | None = None,
        account_name: str | None = None,
        account_email: str | None = None,
    ) -> bool:
        """Atomically bind UUID storage and any freshly detected metadata."""
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(profile_id)
        engine = self._validated_profile_engine(engine, allow_empty=False)
        updates: dict[str, object] = {
            "legacy_storage_name": "",
            "engine": engine,
            "identity_checked_at": "",
            "identity_stale_mask": 7,
        }
        for field_name, value in (
            ("browser", browser),
            ("source_profile", source_profile),
            ("account_name", account_name),
            ("account_email", account_email),
        ):
            if value is None:
                continue
            updates[field_name] = self._validated_profile_identity_text(
                value,
                field_name,
            )
        assignments = ", ".join(f"{column} = ?" for column in updates)
        cursor = self._conn.execute(
            f"UPDATE profiles SET {assignments} WHERE profile_id = ?",
            (*updates.values(), profile_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def mark_profile_storage_resolved(self, profile_id: str, *, engine: str) -> bool:
        return self.finalize_profile_storage(
            profile_id,
            engine=engine,
        )

    @staticmethod
    def _profile_snapshot_values(record: StoredBrowserProfile) -> tuple[object, ...]:
        return (
            record.name,
            record.legacy_storage_name,
            record.created_at,
            record.identity.browser,
            record.identity.source_profile,
            record.identity.account_name,
            record.identity.account_email,
            record.engine,
            record.identity_checked_at,
            record.identity_stale_mask,
        )

    def restore_profile_snapshot(
        self,
        record: StoredBrowserProfile,
        *,
        expected_current: StoredBrowserProfile,
    ) -> bool:
        """CAS-restore a snapshot without overwriting a newer profile mutation."""
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(record.profile_id)
        if canonical_profile_id(expected_current.profile_id) != profile_id:
            raise ValueError("Profile compensation snapshots must have the same identity.")
        cursor = self._conn.execute(
            """
            UPDATE profiles
               SET name = ?, legacy_storage_name = ?, created_at = ?, browser = ?,
                   source_profile = ?, account_name = ?, account_email = ?, engine = ?,
                   identity_checked_at = ?, identity_stale_mask = ?
             WHERE profile_id = ?
               AND name = ? AND legacy_storage_name = ? AND created_at = ?
               AND browser = ? AND source_profile = ? AND account_name = ?
               AND account_email = ? AND engine = ? AND identity_checked_at = ?
               AND identity_stale_mask = ?
            """,
            (
                *self._profile_snapshot_values(record),
                profile_id,
                *self._profile_snapshot_values(expected_current),
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_profile_snapshot(self, record: StoredBrowserProfile) -> bool:
        """CAS-delete only the exact profile row created by a failed operation."""
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(record.profile_id)
        cursor = self._conn.execute(
            """
            DELETE FROM profiles
             WHERE profile_id = ?
               AND name = ? AND legacy_storage_name = ? AND created_at = ?
               AND browser = ? AND source_profile = ? AND account_name = ?
               AND account_email = ? AND engine = ? AND identity_checked_at = ?
               AND identity_stale_mask = ?
            """,
            (profile_id, *self._profile_snapshot_values(record)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def mark_profile_storage_unresolved(
        self,
        profile_id: str,
        legacy_storage_name: str,
    ) -> bool:
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(profile_id)
        legacy_storage_name = self._validated_legacy_storage_name(
            legacy_storage_name
        )
        cursor = self._conn.execute(
            "UPDATE profiles SET legacy_storage_name = ? WHERE profile_id = ?",
            (legacy_storage_name, profile_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_unresolved_profiles(self):
        rows = self._conn.execute(
            f"{self._profile_select()} "
            "WHERE legacy_storage_name <> '' ORDER BY name COLLATE NOCASE, profile_id"
        ).fetchall()
        return [self._stored_profile_from_row(row) for row in rows]

    def save_profile(
        self,
        name: str,
        *,
        browser: str | None = None,
        source_profile: str | None = None,
        account_name: str | None = None,
        account_email: str | None = None,
        engine: str | None = None,
    ) -> None:
        """Compatibility upsert; new rows remain explicitly legacy/unresolved."""
        existing = self._conn.execute(
            "SELECT profile_id FROM profiles WHERE name = ?",
            (name,),
        ).fetchone()
        if existing is None:
            self.create_profile(
                name,
                legacy_storage_name=name,
                browser="" if browser is None else browser,
                source_profile="" if source_profile is None else source_profile,
                account_name="" if account_name is None else account_name,
                account_email="" if account_email is None else account_email,
                engine="" if engine is None else engine,
            )
            return
        self.update_profile_metadata(
            str(existing["profile_id"]),
            browser=browser,
            source_profile=source_profile,
            account_name=account_name,
            account_email=account_email,
            engine=engine,
        )

    def get_profile_id(self, name: str) -> str | None:
        """Return the stable identity for a profile's current display name."""
        try:
            record = self.get_profile_by_name(name)
        except (TypeError, ValueError):
            return None
        return record.profile_id if record is not None else None

    def list_profiles(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM profiles ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def list_profile_records(self):
        rows = self._conn.execute(
            f"{self._profile_select()} ORDER BY name COLLATE NOCASE, profile_id"
        ).fetchall()
        return [self._stored_profile_from_row(row) for row in rows]

    def profile_display_labels(self) -> dict[str, str]:
        return {
            record.profile_id: record.display_label
            for record in self.list_profile_records()
        }

    def get_profile_display_label(self, identity: str) -> str:
        try:
            record = self.get_profile(identity)
        except ValueError:
            try:
                record = self.get_profile_by_name(identity)
            except (TypeError, ValueError):
                record = None
        return record.display_label if record is not None else identity

    def profile_engine_specs(self):
        from automadin.services.browser_profile_manifest import (
            BrowserProfileManifestError,
            read_managed_profile_manifest,
        )
        from automadin.services.browser_profile_metadata import (
            BrowserProfileTarget,
            profile_layout_ready,
        )
        from automadin.services.browser_profile_paths import (
            BrowserProfilePathError,
            resolve_profile_storage_path,
        )

        profiles_root = self._managed_profiles_root()
        specs: list[BrowserProfileTarget] = []
        for record in self.list_profile_records():
            if record.legacy_storage_name or not record.engine:
                continue
            try:
                profile_path = resolve_profile_storage_path(
                    record.profile_id,
                    profiles_root,
                )
                manifest = read_managed_profile_manifest(
                    record.profile_id,
                    profiles_root,
                )
            except (BrowserProfileManifestError, BrowserProfilePathError, OSError):
                continue
            if (
                manifest.engine == record.engine
                and profile_layout_ready(profile_path, record.engine)
            ):
                specs.append(
                    BrowserProfileTarget(
                        record.profile_id,
                        record.name,
                        record.engine,
                    )
                )
        return specs

    def _managed_profiles_root(self) -> str:
        return os.environ.get(
            "AUTOMADIN_PROFILES_DIR",
            os.path.join(os.path.dirname(os.path.abspath(self._db_path)), "profiles"),
        )

    def delete_profile(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT profile_id FROM profiles WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return False
        return self.delete_profile_by_id(str(row["profile_id"]))

    def delete_profile_by_id(self, profile_id: str) -> bool:
        from automadin.services.browser_profile_paths import canonical_profile_id

        profile_id = canonical_profile_id(profile_id)
        cursor = self._conn.execute(
            "DELETE FROM profiles WHERE profile_id = ?",
            (profile_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0
