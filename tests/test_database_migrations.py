from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.hook import AutomationHook, VisualImageHookCondition
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
    browser_profile_id_from_saved_device_id,
    browser_profile_name_from_saved_device_id,
    device_from_saved_id,
)
from automadin.services.database import (
    CURRENT_SCHEMA_VERSION,
    DatabaseMigrationError,
    DatabaseSchemaError,
    DatabaseService,
    UnsupportedDatabaseVersionError,
)
from automadin.services.database_transfer import (
    PENDING_IMPORT_NAME,
    apply_pending_database_import,
    inspect_database,
    stage_database_import,
)


def _schema_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _create_legacy_v0_database(path: Path, profile_name: str = "Legacy User") -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE flows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data BLOB NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE run_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO profiles (name, created_at) VALUES (?, ?)",
            (profile_name, "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()


def _create_v1_database(
    path: Path,
    *,
    profile_names: tuple[str, ...] = ("Legacy User",),
    run_device_ids: dict[str, str] | None = None,
) -> None:
    """Create a frozen version-1 fixture without invoking current migrations."""
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE flows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data BLOB NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE run_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                browser TEXT NOT NULL DEFAULT '',
                source_profile TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                account_email TEXT NOT NULL DEFAULT '',
                engine TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE visual_rules (
                name TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE flow_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flow_name TEXT NOT NULL,
                data TEXT NOT NULL,
                change_type TEXT NOT NULL DEFAULT 'update',
                saved_at TEXT NOT NULL
            );
            CREATE INDEX idx_flow_versions_name_saved
                ON flow_versions (flow_name, saved_at DESC, id DESC);
            PRAGMA user_version = 1;
            """
        )
        for name in profile_names:
            connection.execute(
                "INSERT INTO profiles (name, created_at) VALUES (?, ?)",
                (name, "2026-01-01T00:00:00+00:00"),
            )
        for name, device_id in (run_device_ids or {}).items():
            data = {
                "name": name,
                "flow_name": "Example Flow",
                "device_id": device_id,
            }
            connection.execute(
                """
                INSERT INTO run_configs (name, data, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    name,
                    json.dumps(data),
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        connection.commit()


def _create_v2_database(path: Path) -> tuple[str, str]:
    """Create a frozen version-2 fixture without invoking current migrations."""
    _create_v1_database(path, profile_names=("Legacy User",))
    profile_id = str(uuid.uuid4())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE profiles ADD COLUMN profile_id TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE profiles "
            "ADD COLUMN legacy_storage_name TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            """
            UPDATE profiles
               SET profile_id = ?,
                   legacy_storage_name = ?,
                   browser = 'Chrome',
                   source_profile = 'Profile 4',
                   account_name = 'Jane',
                   account_email = 'jane@example.com',
                   engine = 'chromium'
            """,
            (profile_id, "Legacy User"),
        )
        connection.execute(
            "CREATE UNIQUE INDEX idx_profiles_profile_id ON profiles (profile_id)"
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    return profile_id, "2026-01-01T00:00:00+00:00"


def _create_v3_database(path: Path) -> None:
    """Create a frozen version-3 fixture without invoking current migrations."""
    _create_v2_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE profiles "
            "ADD COLUMN identity_checked_at TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE profiles "
            "ADD COLUMN identity_stale_mask INTEGER NOT NULL DEFAULT 7"
        )
        connection.execute("PRAGMA user_version = 3")
        connection.commit()


def _create_v4_database(path: Path) -> None:
    """Create a frozen version-4 fixture without invoking current migrations."""
    _create_v3_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
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
            );
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
            );
            CREATE INDEX idx_schedules_due
                ON schedules (enabled, next_due_at_utc);
            CREATE INDEX idx_run_history_scheduled
                ON run_history (scheduled_for_utc DESC, run_id DESC);
            PRAGMA user_version = 4;
            """
        )
        connection.commit()


def _create_v6_database(path: Path) -> None:
    """Create a frozen version-6 fixture with one metadata-free image."""
    database = DatabaseService(str(path))
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE images DROP COLUMN source_surface_width")
        connection.execute("ALTER TABLE images DROP COLUMN source_surface_height")
        connection.execute("ALTER TABLE images DROP COLUMN source_coordinate_space")
        connection.execute(
            "INSERT INTO images (name, data, width, height, created_at) "
            "VALUES ('Legacy', X'89504E47', 10, 20, '2026-01-01')"
        )
        connection.execute("PRAGMA user_version = 6")
        connection.commit()


def test_v6_images_migrate_with_unknown_source_surface(tmp_path: Path) -> None:
    path = tmp_path / "v6.db"
    _create_v6_database(path)

    database = DatabaseService(str(path))
    try:
        asset = database.load_image_asset("Legacy")
        assert database.schema_version == CURRENT_SCHEMA_VERSION
        assert asset is not None
        assert asset.source_surface_size is None
        assert asset.source_coordinate_space == ""
    finally:
        database.close()


def test_v7_retirement_removes_moving_target_from_flows_hooks_and_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v7.db"
    retired = FlowCommand("click_moving_target", params={"lead_time_ms": 700})
    flow = AutomationFlow(
        "Animated",
        commands=[
            retired,
            FlowCommand("group", children=[FlowCommand("click_moving_target")]),
            FlowCommand(
                "if_value",
                params={"operator": "is_empty"},
                else_children=[FlowCommand("click_moving_target")],
            ),
        ],
        hooks=[
            AutomationHook(
                name="Chest",
                condition=VisualImageHookCondition(("chest.png",)),
                actions=[FlowCommand("click_moving_target")],
            )
        ],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database = DatabaseService(str(path))
    database.save_flow(flow.name, flow)
    flow.commands[0].comment = "still used"
    database.save_flow(flow.name, flow)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 7")
        connection.commit()

    migrated = DatabaseService(str(path))
    try:
        current = migrated.load_flow("Animated")
        assert current is not None
        assert [command.id for command in current.commands] == ["group", "if_value"]
        assert current.commands[0].children == []
        assert current.commands[1].else_children == []
        assert current.hooks[0].actions == []

        versions = migrated.list_flow_versions("Animated")
        assert versions
        historical = migrated.load_flow_version(versions[0]["id"])
        assert historical is not None
        assert all(command.id != "click_moving_target" for command in historical.commands)
        assert historical.hooks[0].actions == []

        report = json.loads(
            migrated.get_app_setting("retired_command_v8_report") or "{}"
        )
        assert report["flows"]["Animated"] == 8
    finally:
        migrated.close()


def _create_current_database(path: Path, marker: str) -> None:
    database = DatabaseService(str(path))
    try:
        database.set_app_setting("marker", marker)
    finally:
        database.close()


def _marker(path: Path) -> str | None:
    database = DatabaseService(str(path))
    try:
        return database.get_app_setting("marker")
    finally:
        database.close()


def test_new_and_reopened_active_database_use_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "automadin.db"

    first = DatabaseService(str(path))
    first.set_app_setting("marker", "ready")
    assert first.schema_version == CURRENT_SCHEMA_VERSION
    first.close()

    reopened = DatabaseService(str(path))
    try:
        assert reopened.schema_version == CURRENT_SCHEMA_VERSION
        assert reopened.get_app_setting("marker") == "ready"
    finally:
        reopened.close()
    assert _schema_version(path) == CURRENT_SCHEMA_VERSION


def test_normal_startup_transactionally_migrates_legacy_v0_database(tmp_path: Path) -> None:
    path = tmp_path / "automadin.db"
    _create_legacy_v0_database(path)
    assert "visual_rules" not in _table_names(path)

    database = DatabaseService(str(path))
    try:
        assert database.schema_version == CURRENT_SCHEMA_VERSION
        assert database.list_profiles() == ["Legacy User"]
        assert database.list_visual_rules() == []
    finally:
        database.close()

    assert _schema_version(path) == CURRENT_SCHEMA_VERSION
    assert "visual_rules" in _table_names(path)


def test_legacy_v0_runs_each_schema_migration_in_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    _create_legacy_v0_database(path)
    calls: list[str] = []
    migrate_v0_to_v1 = DatabaseService._migrate_v0_to_v1
    migrate_v1_to_v2 = DatabaseService._migrate_v1_to_v2
    migrate_v2_to_v3 = DatabaseService._migrate_v2_to_v3
    migrate_v3_to_v4 = DatabaseService._migrate_v3_to_v4
    migrate_v4_to_v5 = DatabaseService._migrate_v4_to_v5
    migrate_v5_to_v6 = DatabaseService._migrate_v5_to_v6
    migrate_v6_to_v7 = DatabaseService._migrate_v6_to_v7
    migrate_v7_to_v8 = DatabaseService._migrate_v7_to_v8

    def tracked_v0_to_v1(database: DatabaseService) -> None:
        calls.append("0->1")
        migrate_v0_to_v1(database)

    def tracked_v1_to_v2(database: DatabaseService) -> None:
        calls.append("1->2")
        migrate_v1_to_v2(database)

    def tracked_v2_to_v3(database: DatabaseService) -> None:
        calls.append("2->3")
        migrate_v2_to_v3(database)

    def tracked_v3_to_v4(database: DatabaseService) -> None:
        calls.append("3->4")
        migrate_v3_to_v4(database)

    def tracked_v4_to_v5(database: DatabaseService) -> None:
        calls.append("4->5")
        migrate_v4_to_v5(database)

    def tracked_v5_to_v6(database: DatabaseService) -> None:
        calls.append("5->6")
        migrate_v5_to_v6(database)

    def tracked_v6_to_v7(database: DatabaseService) -> None:
        calls.append("6->7")
        migrate_v6_to_v7(database)

    def tracked_v7_to_v8(database: DatabaseService) -> None:
        calls.append("7->8")
        migrate_v7_to_v8(database)

    monkeypatch.setattr(DatabaseService, "_migrate_v0_to_v1", tracked_v0_to_v1)
    monkeypatch.setattr(DatabaseService, "_migrate_v1_to_v2", tracked_v1_to_v2)
    monkeypatch.setattr(DatabaseService, "_migrate_v2_to_v3", tracked_v2_to_v3)
    monkeypatch.setattr(DatabaseService, "_migrate_v3_to_v4", tracked_v3_to_v4)
    monkeypatch.setattr(DatabaseService, "_migrate_v4_to_v5", tracked_v4_to_v5)
    monkeypatch.setattr(DatabaseService, "_migrate_v5_to_v6", tracked_v5_to_v6)
    monkeypatch.setattr(DatabaseService, "_migrate_v6_to_v7", tracked_v6_to_v7)
    monkeypatch.setattr(DatabaseService, "_migrate_v7_to_v8", tracked_v7_to_v8)

    database = DatabaseService(str(path))
    database.close()

    assert calls == [
        "0->1",
        "1->2",
        "2->3",
        "3->4",
        "4->5",
        "5->6",
        "6->7",
        "7->8",
    ]
    assert _schema_version(path) == CURRENT_SCHEMA_VERSION


def test_managed_emulator_profile_table_is_added_after_flow_identity_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    _create_v4_database(path)

    database = DatabaseService(str(path))
    try:
        assert database.schema_version == CURRENT_SCHEMA_VERSION
        assert database.list_managed_emulator_profiles() == []
    finally:
        database.close()

    assert "managed_emulator_profiles" in _table_names(path)


def test_v2_to_v3_preserves_identity_and_marks_never_checked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    profile_id, created_at = _create_v2_database(path)

    database = DatabaseService(str(path))
    try:
        record = database.get_profile(profile_id)
        assert record is not None
        assert record.name == "Legacy User"
        assert record.created_at == created_at
        assert record.legacy_storage_name == "Legacy User"
        assert record.identity.browser == "Chrome"
        assert record.identity.source_profile == "Profile 4"
        assert record.identity.account_name == "Jane"
        assert record.identity.account_email == "jane@example.com"
        assert record.engine == "chromium"
        assert record.identity_checked_at == ""
        assert record.identity_stale_mask == 7
    finally:
        database.close()

    assert _schema_version(path) == CURRENT_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1]): (str(row[2]), int(row[3]), row[4])
            for row in connection.execute("PRAGMA table_info(profiles)")
        }
    assert columns["identity_checked_at"] == ("TEXT", 1, "''")
    assert columns["identity_stale_mask"] == ("INTEGER", 1, "7")


def test_v3_database_is_inspectable_and_migrates_to_schedule_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    _create_v3_database(path)

    inspection = inspect_database(path)
    assert inspection.schema_version == 3
    assert "schedules" not in inspection.table_names

    database = DatabaseService(str(path))
    database.close()

    assert _schema_version(path) == CURRENT_SCHEMA_VERSION
    assert {"schedules", "run_history"} <= _table_names(path)


def test_failed_v3_to_v4_migration_rolls_back_tables_and_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    _create_v3_database(path)
    migrate_v3_to_v4 = DatabaseService._migrate_v3_to_v4

    def fail_after_v4_changes(database: DatabaseService) -> None:
        migrate_v3_to_v4(database)
        raise sqlite3.OperationalError("simulated v4 migration failure")

    monkeypatch.setattr(DatabaseService, "_migrate_v3_to_v4", fail_after_v4_changes)

    with pytest.raises(DatabaseMigrationError, match="simulated v4 migration failure"):
        DatabaseService(str(path))

    assert _schema_version(path) == 3
    assert "schedules" not in _table_names(path)
    assert "run_history" not in _table_names(path)


def test_v4_to_v5_assigns_stable_current_and_orphan_history_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    _create_v4_database(path)
    # Exercise the 4->5 identity migration in isolation. Normal startup then
    # runs the legacy value-model migration, which intentionally removes
    # version-0 history that cannot be converted safely.
    monkeypatch.setattr(
        DatabaseService,
        "_migrate_legacy_value_models",
        lambda _database: None,
    )
    legacy_data = {"name": "Reused", "commands": [], "parameters": []}
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO flows (name, data, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "Reused",
                json.dumps(legacy_data),
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO flow_versions (flow_name, data, change_type, saved_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "Reused",
                json.dumps(legacy_data),
                "delete",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO flow_versions (flow_name, data, change_type, saved_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "Reused",
                json.dumps(legacy_data),
                "update",
                "2026-03-01T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO flow_versions (flow_name, data, change_type, saved_at)
            VALUES (?, ?, ?, ?)
            """,
            ("Reused", json.dumps(legacy_data), "legacy", "not-a-timestamp"),
        )
        connection.commit()

    database = DatabaseService(str(path))
    try:
        current = database.load_flow("Reused")
        assert current is not None
        visible_versions = database.list_flow_versions("Reused")
        assert len(visible_versions) == 1
        current_version = database.load_flow_version(visible_versions[0]["id"])
        assert current_version is not None
        assert current_version.flow_id == current.flow_id
    finally:
        database.close()

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        flow_row = connection.execute(
            "SELECT flow_id, data FROM flows WHERE name = 'Reused'"
        ).fetchone()
        version_rows = connection.execute(
            "SELECT flow_id, data, saved_at FROM flow_versions ORDER BY id"
        ).fetchall()
    assert flow_row is not None
    assert str(uuid.UUID(flow_row["flow_id"])) == flow_row["flow_id"]
    assert version_rows[0]["flow_id"] != flow_row["flow_id"]
    assert version_rows[1]["flow_id"] == flow_row["flow_id"]
    assert version_rows[2]["flow_id"] == version_rows[0]["flow_id"]
    for row in (flow_row, *version_rows):
        payload = json.loads(row["data"])
        assert payload["flow_id"] == row["flow_id"]
        assert payload["value_model_version"] == 0


def test_v4_to_v5_repairs_legacy_version_payload_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    _create_v4_database(path)
    # Keep this test scoped to 4->5; the later legacy value-model migration
    # intentionally removes unconvertible version-0 history at startup.
    monkeypatch.setattr(
        DatabaseService,
        "_migrate_legacy_value_models",
        lambda _database: None,
    )
    current_data = {"name": "Renamed", "commands": [], "parameters": []}
    historical_data = {
        "name": "Original",
        "commands": [{"id": "comment", "params": {}}],
        "parameters": [],
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO flows (name, data, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "Renamed",
                json.dumps(current_data),
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO flow_versions (flow_name, data, change_type, saved_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "Renamed",
                json.dumps(historical_data),
                "rename",
                "2026-03-01T00:00:00+00:00",
            ),
        )
        connection.commit()

    database = DatabaseService(str(path))
    try:
        current = database.load_flow("Renamed")
        versions = database.list_flow_versions("Renamed")
        restored = database.load_flow_version(versions[0]["id"])
        assert current is not None
        assert restored is not None
        assert restored.name == "Renamed"
        assert restored.commands[0].id == "comment"
        assert restored.flow_id == current.flow_id
    finally:
        database.close()


def test_failed_v4_to_v5_flow_identity_migration_rolls_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    _create_v4_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO flows (name, data, created_at, updated_at)
            VALUES ('Broken', 'not-json', ?, ?)
            """,
            (
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()

    with pytest.raises(DatabaseMigrationError, match="invalid JSON"):
        DatabaseService(str(path))

    assert _schema_version(path) == 4
    with sqlite3.connect(path) as connection:
        flow_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(flows)")
        }
    assert "flow_id" not in flow_columns


def test_v2_to_v3_sanitizes_only_legacy_unsafe_identity_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    profile_id, _created_at = _create_v2_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE profiles
               SET source_profile = ?,
                   account_name = ?,
                   account_email = ?
             WHERE profile_id = ?
            """,
            (
                "S" * 321,
                "Legacy\nUser",
                sqlite3.Binary(b"legacy"),
                profile_id,
            ),
        )
        connection.commit()

    database = DatabaseService(str(path))
    try:
        record = database.get_profile(profile_id)
        assert record is not None
        assert record.identity.source_profile == "S" * 320
        assert record.identity.account_name == "Legacy User"
        assert record.identity.account_email == ""
        assert record.identity_checked_at == ""
        assert record.identity_stale_mask == 7
    finally:
        database.close()


def test_failed_v2_to_v3_migration_rolls_back_both_columns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    _create_v2_database(path)
    migrate_v2_to_v3 = DatabaseService._migrate_v2_to_v3

    def fail_after_v3_changes(database: DatabaseService) -> None:
        migrate_v2_to_v3(database)
        raise sqlite3.OperationalError("simulated v3 migration failure")

    monkeypatch.setattr(DatabaseService, "_migrate_v2_to_v3", fail_after_v3_changes)

    with pytest.raises(DatabaseMigrationError, match="simulated v3 migration failure"):
        DatabaseService(str(path))

    assert _schema_version(path) == 2
    with sqlite3.connect(path) as connection:
        profile_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(profiles)")
        }
    assert "identity_checked_at" not in profile_columns
    assert "identity_stale_mask" not in profile_columns


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("identity_stale_mask", 8),
        ("identity_stale_mask", sqlite3.Binary(b"1")),
        ("identity_checked_at", "2026-07-20T12:00:00"),
        ("identity_checked_at", "malformed"),
        ("account_name", sqlite3.Binary(b"42")),
        ("account_email", "x" * 321),
        ("source_profile", "bad\nprofile"),
    ],
)
def test_v3_schema_validation_rejects_unsafe_identity_content(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    path = tmp_path / "automadin.db"
    database = DatabaseService(str(path))
    record = database.create_profile("Work", engine="chromium")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE profiles SET {column} = ? WHERE profile_id = ?",
            (value, record.profile_id),
        )
        connection.commit()

    with pytest.raises(DatabaseSchemaError, match="identity"):
        DatabaseService(str(path))


def test_v1_migration_assigns_profile_ids_and_rewrites_only_unambiguous_references(
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    exact_id = BrowserDeviceConfig(
        engine=BrowserEngine.CHROMIUM,
        screen_mode=ScreenMode.MOBILE_ANDROID,
        profile_name="Work",
    ).to_id()
    unique_casefold_id = "firefox|hl|desktop|perm|PERSONAL"
    ambiguous_id = BrowserDeviceConfig(
        engine=BrowserEngine.CHROMIUM,
        profile_name="WORK",
    ).to_id()
    malformed_id = "chromium|hl|invalid-mode|perm|Work"
    _create_v1_database(
        path,
        profile_names=("Work", "work", "Personal"),
        run_device_ids={
            "Exact": exact_id,
            "Unique casefold": unique_casefold_id,
            "Ambiguous": ambiguous_id,
            "Malformed": malformed_id,
            "Android": "emulator-5554",
        },
    )

    database = DatabaseService(str(path))
    try:
        with sqlite3.connect(path) as connection:
            rows = connection.execute(
                "SELECT name, profile_id, legacy_storage_name FROM profiles ORDER BY id"
            ).fetchall()
        profile_ids = {name: profile_id for name, profile_id, _legacy in rows}
        assert len(set(profile_ids.values())) == 3
        for name, profile_id, legacy_storage_name in rows:
            assert str(uuid.UUID(profile_id)) == profile_id
            assert legacy_storage_name == name

        exact = database.load_run_config("Exact")
        unique_casefold = database.load_run_config("Unique casefold")
        ambiguous = database.load_run_config("Ambiguous")
        malformed = database.load_run_config("Malformed")
        android = database.load_run_config("Android")
        assert exact is not None
        assert unique_casefold is not None
        assert ambiguous is not None
        assert malformed is not None
        assert android is not None

        assert browser_profile_id_from_saved_device_id(exact.device_id) == profile_ids["Work"]
        exact_device = device_from_saved_id(exact.device_id)
        assert isinstance(exact_device, BrowserDevice)
        assert exact_device.config.engine == BrowserEngine.CHROMIUM
        assert exact_device.config.screen_mode == ScreenMode.MOBILE_ANDROID
        assert not exact_device.config.headless

        assert (
            browser_profile_id_from_saved_device_id(unique_casefold.device_id)
            == profile_ids["Personal"]
        )
        casefold_device = device_from_saved_id(unique_casefold.device_id)
        assert isinstance(casefold_device, BrowserDevice)
        assert casefold_device.config.engine == BrowserEngine.FIREFOX
        assert casefold_device.config.headless

        assert ambiguous.device_id == ambiguous_id
        assert browser_profile_name_from_saved_device_id(ambiguous.device_id) == "WORK"
        assert malformed.device_id == malformed_id
        assert android.device_id == "emulator-5554"
    finally:
        database.close()


def test_failed_v1_to_v2_migration_rolls_back_columns_ids_and_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "automadin.db"
    legacy_device_id = BrowserDeviceConfig(
        engine=BrowserEngine.CHROMIUM,
        profile_name="Legacy User",
    ).to_id()
    _create_v1_database(
        path,
        run_device_ids={"Legacy run": legacy_device_id},
    )
    migrate_v1_to_v2 = DatabaseService._migrate_v1_to_v2

    def fail_after_v2_changes(database: DatabaseService) -> None:
        migrate_v1_to_v2(database)
        raise sqlite3.OperationalError("simulated v2 migration failure")

    monkeypatch.setattr(DatabaseService, "_migrate_v1_to_v2", fail_after_v2_changes)

    with pytest.raises(DatabaseMigrationError, match="simulated v2 migration failure"):
        DatabaseService(str(path))

    assert _schema_version(path) == 1
    with sqlite3.connect(path) as connection:
        profile_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(profiles)")
        }
        saved_data = json.loads(
            connection.execute(
                "SELECT data FROM run_configs WHERE name = 'Legacy run'"
            ).fetchone()[0]
        )
        indexes = {
            str(row[1]) for row in connection.execute("PRAGMA index_list(profiles)")
        }
    assert "profile_id" not in profile_columns
    assert "legacy_storage_name" not in profile_columns
    assert "idx_profiles_profile_id" not in indexes
    assert saved_data["device_id"] == legacy_device_id


def test_future_active_database_is_rejected_before_mutation(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    original = path.read_bytes()

    with pytest.raises(UnsupportedDatabaseVersionError, match="newer"):
        DatabaseService(str(path))

    assert path.read_bytes() == original
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_failed_active_migration_rolls_back_schema_and_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "legacy.db"
    _create_legacy_v0_database(path)

    def fail_migration(database: DatabaseService) -> None:
        database._conn.execute("CREATE TABLE partial_migration (id INTEGER)")
        raise sqlite3.OperationalError("simulated migration failure")

    monkeypatch.setattr(DatabaseService, "_migrate_v0_to_v1", fail_migration)

    with pytest.raises(DatabaseMigrationError, match="simulated migration failure"):
        DatabaseService(str(path))

    assert _schema_version(path) == 0
    assert "partial_migration" not in _table_names(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM profiles").fetchone()[0] == "Legacy User"


def test_legacy_import_migrates_staged_copy_and_never_changes_source(tmp_path: Path) -> None:
    source = tmp_path / "legacy-export.db"
    active = tmp_path / "automadin.db"
    _create_legacy_v0_database(source, "Imported Legacy")
    _create_current_database(active, "current")
    source_bytes = source.read_bytes()

    inspection = stage_database_import(source, active)

    pending = tmp_path / PENDING_IMPORT_NAME
    assert inspection.schema_version == 0
    assert source.read_bytes() == source_bytes
    assert _schema_version(source) == 0
    assert inspect_database(pending).schema_version == CURRENT_SCHEMA_VERSION

    result = apply_pending_database_import(active)

    assert result is not None and result.applied
    imported = DatabaseService(str(active))
    try:
        assert imported.schema_version == CURRENT_SCHEMA_VERSION
        assert imported.list_profiles() == ["Imported Legacy"]
    finally:
        imported.close()
    assert source.read_bytes() == source_bytes


def test_future_import_is_rejected_without_changing_selected_source(tmp_path: Path) -> None:
    source = tmp_path / "future-export.db"
    active = tmp_path / "automadin.db"
    with sqlite3.connect(source) as connection:
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    _create_current_database(active, "current")
    source_bytes = source.read_bytes()

    with pytest.raises(UnsupportedDatabaseVersionError, match="newer"):
        stage_database_import(source, active)

    assert source.read_bytes() == source_bytes
    assert _marker(active) == "current"
    assert not (tmp_path / PENDING_IMPORT_NAME).exists()


def test_failed_pending_migration_is_rolled_back_and_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    active = tmp_path / "automadin.db"
    pending = tmp_path / PENDING_IMPORT_NAME
    _create_current_database(active, "current")
    _create_legacy_v0_database(pending)

    def fail_migration(database: DatabaseService) -> None:
        database._conn.execute("CREATE TABLE partial_migration (id INTEGER)")
        raise sqlite3.OperationalError("staged migration failed")

    monkeypatch.setattr(DatabaseService, "_migrate_v0_to_v1", fail_migration)

    result = apply_pending_database_import(active)

    assert result is not None and not result.applied
    assert result.rejected_path is not None and result.rejected_path.is_file()
    assert "staged migration failed" in str(result.error)
    assert _marker(active) == "current"
    assert _schema_version(result.rejected_path) == 0
    assert "partial_migration" not in _table_names(result.rejected_path)
