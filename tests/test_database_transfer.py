from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from automadin.services import database as database_module
from automadin.services import database_transfer
from automadin.services.database import DatabaseService
from automadin.services.database_transfer import (
    MAX_SCHEMA_TABLES,
    PENDING_IMPORT_NAME,
    PORTABILITY_ACTIVATION_PLAN_NAME,
    PORTABILITY_PRIVATE_DATABASE_NAME,
    DatabaseInspectionCancelledError,
    apply_pending_database_import,
    inspect_database,
    stage_database_copy,
    stage_database_import,
)


def _make_database(path: Path, marker: str) -> None:
    db = DatabaseService(str(path))
    db.set_app_setting("marker", marker)
    db.save_profile(f"profile-{marker}")
    db.close()


def _marker(path: Path) -> str | None:
    db = DatabaseService(str(path))
    try:
        return db.get_app_setting("marker")
    finally:
        db.close()


def test_stage_then_apply_preserves_current_backup(tmp_path: Path) -> None:
    active = tmp_path / "automadin.db"
    imported = tmp_path / "exported.db"
    _make_database(active, "current")
    _make_database(imported, "imported")

    inspection = stage_database_import(imported, active)

    assert inspection.profile_count == 1
    assert _marker(active) == "current"
    assert (tmp_path / PENDING_IMPORT_NAME).is_file()

    result = apply_pending_database_import(active)

    assert result is not None and result.applied
    assert result.active_database_matches_import
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _marker(active) == "imported"
    assert _marker(result.backup_path) == "current"
    assert not (tmp_path / PENDING_IMPORT_NAME).exists()


def test_staging_captures_committed_wal_content(tmp_path: Path) -> None:
    active = tmp_path / "automadin.db"
    imported = tmp_path / "live-export.db"
    _make_database(active, "current")
    source = DatabaseService(str(imported))
    source.set_app_setting("marker", "wal-import")

    stage_database_import(imported, active)
    source.close()
    result = apply_pending_database_import(active)

    assert result is not None and result.applied
    assert result.active_database_matches_import
    assert _marker(active) == "wal-import"


def test_staging_rejects_existing_pending_import_without_replacing_it(
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _make_database(active, "current")
    _make_database(first, "first")
    _make_database(second, "second")

    stage_database_import(first, active)

    with pytest.raises(FileExistsError, match="already exists"):
        stage_database_import(second, active)

    assert _marker(tmp_path / PENDING_IMPORT_NAME) == "first"
    assert _marker(active) == "current"
    assert not list(tmp_path.glob(f"{PENDING_IMPORT_NAME}.*.tmp"))


@pytest.mark.parametrize(
    "portability_name",
    [PORTABILITY_ACTIVATION_PLAN_NAME, PORTABILITY_PRIVATE_DATABASE_NAME],
)
def test_database_only_staging_refuses_pending_portability_operation(
    portability_name: str,
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    imported = tmp_path / "imported.db"
    _make_database(active, "current")
    _make_database(imported, "imported")
    owner = tmp_path / portability_name
    owner.write_bytes(b"first operation remains owned")

    with pytest.raises(FileExistsError, match="portability-bundle activation"):
        stage_database_import(imported, active)

    assert owner.read_bytes() == b"first operation remains owned"
    assert _marker(active) == "current"
    assert not (tmp_path / PENDING_IMPORT_NAME).exists()


def test_database_only_apply_refuses_pending_owned_by_portability_journal(
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    pending = tmp_path / PENDING_IMPORT_NAME
    _make_database(active, "current")
    _make_database(pending, "pending")
    journal = tmp_path / PORTABILITY_ACTIVATION_PLAN_NAME
    journal.write_bytes(b"portability owns the standard pending name")

    result = apply_pending_database_import(active)

    assert result is not None and not result.applied
    assert "belongs to a portability-bundle" in str(result.error)
    assert pending.is_file()
    assert _marker(pending) == "pending"
    assert _marker(active) == "current"


def test_exact_destination_staging_requires_explicit_replacement(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    staged = tmp_path / "bundle-staging" / "database.db"
    _make_database(first, "first")
    _make_database(second, "second")

    stage_database_copy(first, staged)
    with pytest.raises(FileExistsError, match="already exists"):
        stage_database_copy(second, staged)

    assert _marker(staged) == "first"

    stage_database_copy(second, staged, replace_existing=True)

    assert _marker(staged) == "second"


def test_windows_staging_publish_translates_a_destination_creation_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if database_transfer.os.name != "nt":
        pytest.skip("Windows rename provides the no-replace publication primitive.")
    source = tmp_path / "source.db"
    staged = tmp_path / "staged.db"
    _make_database(source, "source")

    def race_publish(_temporary: Path, destination: Path) -> None:
        destination.write_bytes(b"racing owner")
        raise FileExistsError("simulated race")

    monkeypatch.setattr(database_transfer.os, "rename", race_publish)
    monkeypatch.setattr(
        database_transfer.os,
        "link",
        lambda *_args: pytest.fail("Windows staging must not require hard links"),
    )

    with pytest.raises(FileExistsError, match="already exists"):
        stage_database_copy(source, staged)

    assert staged.read_bytes() == b"racing owner"
    assert not list(tmp_path.glob("staged.db.*.tmp*"))


def test_invalid_database_cannot_be_staged(tmp_path: Path) -> None:
    active = tmp_path / "automadin.db"
    invalid = tmp_path / "not-a-database.db"
    _make_database(active, "current")
    invalid.write_text("not sqlite", encoding="utf-8")

    with pytest.raises(ValueError, match="SQLite"):
        stage_database_import(invalid, active)

    assert _marker(active) == "current"
    assert not (tmp_path / PENDING_IMPORT_NAME).exists()


def test_invalid_pending_file_is_rejected_without_touching_active(tmp_path: Path) -> None:
    active = tmp_path / "automadin.db"
    _make_database(active, "current")
    (tmp_path / PENDING_IMPORT_NAME).write_text("broken", encoding="utf-8")

    result = apply_pending_database_import(active)

    assert result is not None and not result.applied
    assert not result.active_database_matches_import
    assert result.rejected_path is not None and result.rejected_path.is_file()
    assert _marker(active) == "current"


def test_same_active_database_is_rejected(tmp_path: Path) -> None:
    active = tmp_path / "automadin.db"
    _make_database(active, "current")

    with pytest.raises(ValueError, match="already the active"):
        stage_database_import(active, active)


def test_inspection_reports_database_contents(tmp_path: Path) -> None:
    database = tmp_path / "automadin.db"
    _make_database(database, "sample")

    inspection = inspect_database(database)

    assert inspection.profile_count == 1
    assert inspection.flow_count == 0
    assert inspection.image_count == 0


def test_inspection_can_cancel_during_integrity_work(tmp_path: Path) -> None:
    database = tmp_path / "automadin.db"
    _make_database(database, "sample")
    with sqlite3.connect(database) as connection:
        for index in range(300):
            connection.execute(f"CREATE TABLE extra_{index} (value TEXT)")
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(DatabaseInspectionCancelledError, match="cancelled"):
        inspect_database(database, cancel=cancel)

    assert calls >= 2


def test_inspection_rejects_excessive_schema_table_count(tmp_path: Path) -> None:
    database = tmp_path / "automadin.db"
    _make_database(database, "sample")
    with sqlite3.connect(database) as connection:
        for index in range(MAX_SCHEMA_TABLES + 1):
            connection.execute(f"CREATE TABLE excessive_{index} (value TEXT)")

    with pytest.raises(ValueError, match="too many schema tables"):
        inspect_database(database)


def test_pending_database_symlink_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    external = tmp_path / "external.db"
    pending = tmp_path / PENDING_IMPORT_NAME
    _make_database(active, "current")
    _make_database(external, "external")
    original = external.read_bytes()
    try:
        os.symlink(external, pending)
    except OSError:
        pytest.skip("File links are unavailable on this Windows configuration.")

    result = apply_pending_database_import(active)

    assert result is not None and not result.applied
    assert "real, private, direct file" in str(result.error)
    assert external.read_bytes() == original
    assert os.path.lexists(pending)
    assert _marker(active) == "current"


def test_pending_database_hardlink_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    external = tmp_path / "external.db"
    pending = tmp_path / PENDING_IMPORT_NAME
    _make_database(active, "current")
    _make_database(external, "external")
    original = external.read_bytes()
    os.link(external, pending)

    result = apply_pending_database_import(active)

    assert result is not None and not result.applied
    assert "real, private, direct file" in str(result.error)
    assert external.read_bytes() == original
    assert pending.is_file()
    assert _marker(active) == "current"


def test_redirected_active_database_is_rejected_before_sibling_paths_are_used(
    tmp_path: Path,
) -> None:
    external_dir = tmp_path / "external"
    data_dir = tmp_path / "data"
    external_dir.mkdir()
    data_dir.mkdir()
    external = external_dir / "automadin.db"
    imported = tmp_path / "imported.db"
    _make_database(external, "external")
    _make_database(imported, "imported")
    active_link = data_dir / "automadin.db"
    original = external.read_bytes()
    try:
        os.symlink(external, active_link)
    except OSError:
        pytest.skip("File links are unavailable on this Windows configuration.")

    with pytest.raises(ValueError, match="active Automadin database"):
        stage_database_import(imported, active_link)

    (data_dir / PENDING_IMPORT_NAME).write_bytes(imported.read_bytes())
    result = apply_pending_database_import(active_link)

    assert result is not None and not result.applied
    assert "active Automadin database" in str(result.error)
    assert external.read_bytes() == original
    assert os.path.lexists(active_link)
    assert (data_dir / PENDING_IMPORT_NAME).is_file()
    assert not (external_dir / PENDING_IMPORT_NAME).exists()


def test_failed_post_swap_validation_restores_previous_database(
    monkeypatch, tmp_path: Path
) -> None:
    active = tmp_path / "automadin.db"
    imported = tmp_path / "exported.db"
    _make_database(active, "current")
    _make_database(imported, "imported")
    stage_database_import(imported, active)
    original_inspect = database_transfer.inspect_database

    def fail_only_for_active(path):
        if Path(path).resolve() == active.resolve():
            raise ValueError("simulated post-swap failure")
        return original_inspect(path)

    monkeypatch.setattr(database_transfer, "inspect_database", fail_only_for_active)

    result = apply_pending_database_import(active)

    assert result is not None and not result.applied
    assert not result.active_database_matches_import
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _marker(active) == "current"


def test_failed_restore_preserves_active_file_and_reports_safety_backup(
    monkeypatch, tmp_path: Path
) -> None:
    active = tmp_path / "automadin.db"
    imported = tmp_path / "exported.db"
    _make_database(active, "current")
    _make_database(imported, "imported")
    stage_database_import(imported, active)
    original_inspect = database_transfer.inspect_database
    original_backup = database_transfer._sqlite_backup

    def fail_only_for_active(path):
        if Path(path).resolve() == active.resolve():
            raise ValueError("simulated post-swap failure")
        return original_inspect(path)

    def fail_restore(source, destination):
        if Path(source).name.startswith("automadin-before-import-"):
            raise OSError("simulated restore disk failure")
        return original_backup(source, destination)

    monkeypatch.setattr(database_transfer, "inspect_database", fail_only_for_active)
    monkeypatch.setattr(database_transfer, "_sqlite_backup", fail_restore)

    result = apply_pending_database_import(active)

    assert result is not None and not result.applied
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _marker(result.backup_path) == "current"
    assert active.is_file()
    assert _marker(active) == "imported"
    assert result.active_database_matches_import
    assert "Automatic restore also failed" in str(result.error)
    assert str(result.backup_path) in str(result.error)
    assert not list(tmp_path.glob(".automadin.db.restore-*.tmp"))


def test_stale_wal_sidecars_are_removed_before_swap(tmp_path: Path) -> None:
    active = tmp_path / "automadin.db"
    imported = tmp_path / "exported.db"
    _make_database(active, "current")
    _make_database(imported, "imported")
    stage_database_import(imported, active)
    wal = active.with_name(active.name + "-wal")
    shm = active.with_name(active.name + "-shm")
    wal.write_bytes(b"stale")
    shm.write_bytes(b"stale")

    result = apply_pending_database_import(active)

    assert result is not None and result.applied
    assert not wal.exists() or wal.read_bytes() != b"stale"
    assert not shm.exists() or shm.read_bytes() != b"stale"


def test_failed_swap_after_wal_removal_restores_committed_logical_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    imported = tmp_path / "exported.db"
    _make_database(active, "base")
    _make_database(imported, "imported")
    base_bytes = active.read_bytes()
    connection = sqlite3.connect(active)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "UPDATE app_settings SET value = ? WHERE key = ?",
        ("committed-in-wal", "marker"),
    )
    connection.commit()
    wal = active.with_name(active.name + "-wal")
    shm = active.with_name(active.name + "-shm")
    wal_bytes = wal.read_bytes()
    shm_bytes = shm.read_bytes()
    connection.close()
    active.write_bytes(base_bytes)
    wal.write_bytes(wal_bytes)
    shm.write_bytes(shm_bytes)
    stage_database_import(imported, active)
    pending = tmp_path / PENDING_IMPORT_NAME
    original_replace = database_transfer.os.replace

    def fail_only_pending_swap(source, destination):
        if Path(source) == pending and Path(destination) == active:
            raise OSError("simulated pending swap failure")
        return original_replace(source, destination)

    monkeypatch.setattr(database_transfer.os, "replace", fail_only_pending_swap)

    result = apply_pending_database_import(active)

    assert result is not None and not result.applied
    assert not result.active_database_matches_import
    assert "simulated pending swap failure" in str(result.error)
    assert result.backup_path is not None
    assert _marker(active) == "committed-in-wal"
    assert _marker(result.backup_path) == "committed-in-wal"
    assert pending.is_file()


def test_export_is_wal_consistent_and_explicitly_replaces_destination(
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    destination = tmp_path / "export.db"
    _make_database(destination, "old-export")
    database = DatabaseService(str(active))
    database.set_app_setting("marker", "current-wal")

    database.export_database(str(destination))

    assert _marker(destination) == "current-wal"
    database.close()


def test_export_publish_failure_preserves_destination_and_cleans_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    destination = tmp_path / "export.db"
    _make_database(destination, "old-export")
    database = DatabaseService(str(active))
    database.set_app_setting("marker", "new-export")

    def fail_publish(_source: str, _destination: str) -> None:
        raise OSError("simulated export publish failure")

    monkeypatch.setattr(database_module.os, "replace", fail_publish)

    with pytest.raises(OSError, match="publish failure"):
        database.export_database(str(destination))

    assert _marker(destination) == "old-export"
    assert not list(tmp_path.glob(".export.db.*.tmp*"))
    database.close()


def test_export_validation_failure_preserves_destination_and_cleans_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "automadin.db"
    destination = tmp_path / "export.db"
    _make_database(destination, "old-export")
    database = DatabaseService(str(active))
    database.set_app_setting("marker", "new-export")
    original_initialize = DatabaseService._initialize_schema

    def fail_only_for_export_staging(service: DatabaseService) -> None:
        if Path(service.db_path).name.startswith(".export.db."):
            raise ValueError("simulated export validation failure")
        original_initialize(service)

    monkeypatch.setattr(DatabaseService, "_initialize_schema", fail_only_for_export_staging)

    with pytest.raises(ValueError, match="validation failure"):
        database.export_database(str(destination))

    assert _marker(destination) == "old-export"
    assert not list(tmp_path.glob(".export.db.*.tmp*"))
    database.close()
