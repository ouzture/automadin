from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from automadin.services import portability_bundle as portability_bundle_module
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseCleanupError,
    BrowserProfileLeaseRegistry,
    MaintenanceProfileScope,
    ProfileUse,
)
from automadin.services.browser_profile_storage import create_clean_profile
from automadin.services.database import DatabaseService
from automadin.services.database_transfer import DatabaseInspectionCancelledError
from automadin.services.portability_bundle import (
    BUNDLE_MANIFEST_PATH,
    BundleLimits,
    BundleProgress,
    LocalProfileCollision,
    PortabilityBundleCancelledError,
    PortabilityBundleValidationError,
    export_portability_bundle,
    inspect_portability_bundle,
    preflight_zip_central_directory,
)


@pytest.fixture
def workspace(tmp_path: Path):
    database = DatabaseService(str(tmp_path / "automadin.db"))
    profiles = tmp_path / "profiles"
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    with registry.acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", "create bundle fixture", "bundle-fixture-1"),
    ) as lease:
        change = create_clean_profile(
            profile_id,
            "firefox",
            lease_registry=registry,
            maintenance_lease=lease,
            profiles_root=profiles,
        )
        (change.path / "prefs.js").write_text("user_pref('test', true);", encoding="utf-8")
        change.commit()
    record = database.create_profile(
        "Primary Account",
        profile_id=profile_id,
        browser="Firefox",
        engine="firefox",
    )
    try:
        yield database, profiles, record
    finally:
        database.close()


def test_export_and_read_only_inspection_round_trip(workspace, tmp_path: Path) -> None:
    database, profiles, record = workspace
    destination = tmp_path / "portable.automadin-bundle"

    result = export_portability_bundle(
        database,
        destination,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
        source_app_version="test",
    )
    inspection = inspect_portability_bundle(destination, profiles_root=profiles)

    assert result.path == destination
    assert "not encrypted" in result.warning
    assert inspection.database.profile_count == 1
    assert inspection.database_profiles[0].display_name == "Primary Account"
    assert inspection.database_profiles[0].included
    assert inspection.manifest.profiles[0].profile_id == record.profile_id
    assert inspection.collisions[0].outcome is LocalProfileCollision.IDENTICAL


def test_nested_profile_json_is_payload_not_profile_identity(workspace, tmp_path: Path) -> None:
    database, profiles, record = workspace
    nested = profiles / record.profile_id / "nested" / "profile.json"
    nested.parent.mkdir()
    nested.write_text("ordinary nested browser data", encoding="utf-8")
    destination = tmp_path / "nested-profile-json.automadin-bundle"

    export_portability_bundle(
        database,
        destination,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    inspection = inspect_portability_bundle(
        destination,
        profiles_root=tmp_path / "other-profiles",
    )

    assert inspection.manifest.profiles[0].profile_id == record.profile_id


def test_nested_profile_json_cannot_mask_invalid_canonical_manifest(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    profile_root = profiles / record.profile_id
    nested = profile_root / "nested" / "profile.json"
    nested.parent.mkdir()
    nested.write_bytes((profile_root / "profile.json").read_bytes())
    original = tmp_path / "root-manifest.automadin-bundle"
    export_portability_bundle(
        database,
        original,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )

    rewritten = tmp_path / "invalid-root-manifest.automadin-bundle"
    root_member = f"profiles/{record.profile_id}/profile.json"
    invalid_root = b"{}"
    with zipfile.ZipFile(original) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    manifest = json.loads(
        next(payload for info, payload in entries if info.filename == BUNDLE_MANIFEST_PATH)
    )
    root_file = next(
        item for item in manifest["profiles"][0]["files"] if item["path"] == "profile.json"
    )
    root_file["size"] = len(invalid_root)
    root_file["sha256"] = hashlib.sha256(invalid_root).hexdigest()
    manifest["profiles"][0]["total_size"] = sum(
        item["size"] for item in manifest["profiles"][0]["files"]
    )
    manifest_payload = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with zipfile.ZipFile(rewritten, "w", allowZip64=True) as target:
        for info, payload in entries:
            if info.filename == root_member:
                payload = invalid_root
            elif info.filename == BUNDLE_MANIFEST_PATH:
                payload = manifest_payload
            target.writestr(info, payload)

    with pytest.raises(
        PortabilityBundleValidationError,
        match="Embedded profile.json is invalid",
    ):
        inspect_portability_bundle(
            rewritten,
            profiles_root=tmp_path / "other-profiles",
        )


def test_exact_profile_manifests_are_validated_before_database_inspection(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    second_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    with registry.acquire_maintenance(
        second_id,
        ProfileUse("maintenance", "create second bundle profile", "bundle-fixture-2"),
    ) as lease:
        change = create_clean_profile(
            second_id,
            "firefox",
            lease_registry=registry,
            maintenance_lease=lease,
            profiles_root=profiles,
        )
        change.commit()
    database.create_profile(
        "Second Account",
        profile_id=second_id,
        browser="Firefox",
        engine="firefox",
    )
    destination = tmp_path / "immediate-manifest-validation.automadin-bundle"
    export_portability_bundle(
        database,
        destination,
        [record.profile_id, second_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    expected_ids = {record.profile_id, second_id}
    validated_ids: set[str] = set()
    real_validate = portability_bundle_module._validate_embedded_profile_manifest
    real_inspect_database = portability_bundle_module.inspect_database

    def validate_immediately(profile, payload) -> None:
        real_validate(profile, payload)
        validated_ids.add(profile.profile_id)

    def inspect_after_manifests(path, *, cancel=None):
        assert validated_ids == expected_ids
        return real_inspect_database(path, cancel=cancel)

    with (
        patch.object(
            portability_bundle_module,
            "_validate_embedded_profile_manifest",
            side_effect=validate_immediately,
        ),
        patch.object(
            portability_bundle_module,
            "inspect_database",
            side_effect=inspect_after_manifests,
        ),
    ):
        inspection = inspect_portability_bundle(
            destination,
            profiles_root=tmp_path / "other-profiles",
            classify_local_profiles=False,
        )

    assert {profile.profile_id for profile in inspection.manifest.profiles} == expected_ids


def test_database_profile_count_mismatch_rejects_before_row_query(
    workspace, tmp_path: Path
) -> None:
    database, profiles, _record = workspace
    original = tmp_path / "database-only-count.automadin-bundle"
    export_portability_bundle(database, original, profiles_root=profiles)
    rewritten = tmp_path / "database-count-mismatch.automadin-bundle"
    with zipfile.ZipFile(original) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    manifest = json.loads(
        next(payload for info, payload in entries if info.filename == BUNDLE_MANIFEST_PATH)
    )
    assert len(manifest["omitted_profiles"]) == 1
    manifest["omitted_profiles"] = []
    manifest_payload = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with zipfile.ZipFile(rewritten, "w", allowZip64=True) as target:
        for info, payload in entries:
            if info.filename == BUNDLE_MANIFEST_PATH:
                payload = manifest_payload
            target.writestr(info, payload)

    with (
        patch.object(
            portability_bundle_module,
            "_database_profiles",
            side_effect=AssertionError("profile rows must not be queried"),
        ),
        pytest.raises(PortabilityBundleValidationError, match="profile count"),
    ):
        inspect_portability_bundle(
            rewritten,
            profiles_root=tmp_path / "other-profiles",
            classify_local_profiles=False,
        )


def test_manifest_profile_limit_accepts_boundary_and_rejects_one_more(
    workspace, tmp_path: Path
) -> None:
    database, profiles, _record = workspace
    original = tmp_path / "profile-limit-boundary.automadin-bundle"
    export_portability_bundle(database, original, profiles_root=profiles)
    boundary = BundleLimits(max_profiles=1)

    accepted = inspect_portability_bundle(
        original,
        profiles_root=tmp_path / "other-profiles",
        classify_local_profiles=False,
        limits=boundary,
    )
    assert len(accepted.manifest.omitted_profiles) == 1

    rewritten = tmp_path / "profile-limit-exceeded.automadin-bundle"
    with zipfile.ZipFile(original) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    manifest = json.loads(
        next(payload for info, payload in entries if info.filename == BUNDLE_MANIFEST_PATH)
    )
    manifest["omitted_profiles"].append({"profile_id": str(uuid.uuid4()), "reason": "not selected"})
    manifest_payload = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with zipfile.ZipFile(rewritten, "w", allowZip64=True) as target:
        for info, payload in entries:
            if info.filename == BUNDLE_MANIFEST_PATH:
                payload = manifest_payload
            target.writestr(info, payload)

    with pytest.raises(PortabilityBundleValidationError, match="profile-count limit"):
        inspect_portability_bundle(
            rewritten,
            profiles_root=tmp_path / "other-profiles",
            classify_local_profiles=False,
            limits=boundary,
        )


def test_database_profile_row_iteration_is_capped_by_manifest(workspace, tmp_path: Path) -> None:
    database, profiles, record = workspace
    archive = tmp_path / "one-profile.automadin-bundle"
    export_portability_bundle(
        database,
        archive,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    manifest = inspect_portability_bundle(
        archive,
        profiles_root=tmp_path / "other-profiles",
        classify_local_profiles=False,
    ).manifest
    oversized = tmp_path / "too-many-profile-rows.db"
    with sqlite3.connect(oversized) as connection:
        connection.execute("CREATE TABLE profiles (profile_id TEXT, name TEXT, engine TEXT)")
        connection.executemany(
            "INSERT INTO profiles (profile_id, name, engine) VALUES (?, ?, ?)",
            [
                (record.profile_id, "Primary Account", "firefox"),
                (str(uuid.uuid4()), "Unexpected Account", "firefox"),
            ],
        )

    with pytest.raises(PortabilityBundleValidationError, match="exceeds bundle.json"):
        portability_bundle_module._database_profiles(oversized, manifest)


def test_database_profile_text_is_sql_bounded_before_materialization(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    archive = tmp_path / "bounded-db-text.automadin-bundle"
    export_portability_bundle(
        database,
        archive,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    manifest = inspect_portability_bundle(
        archive,
        profiles_root=tmp_path / "other-profiles",
        classify_local_profiles=False,
    ).manifest

    class BoundedConnection:
        profile_query = ""

        def setlimit(self, category: int, limit: int) -> int:
            assert category == sqlite3.SQLITE_LIMIT_LENGTH
            assert limit == 64 * 1024
            return 1_000_000_000

        def execute(self, query: str):
            if query.startswith("PRAGMA"):
                return [(0, "profile_id"), (1, "name"), (2, "engine")]
            self.profile_query = query
            return iter(
                [
                    (
                        "text",
                        36,
                        record.profile_id,
                        "text",
                        10_000_000,
                        "x" * 201,
                        "text",
                        7,
                        "firefox",
                    )
                ]
            )

        def close(self) -> None:
            return None

    connection = BoundedConnection()
    with (
        patch.object(
            portability_bundle_module.sqlite3,
            "connect",
            return_value=connection,
        ),
        pytest.raises(PortabilityBundleValidationError, match="safe length"),
    ):
        portability_bundle_module._database_profiles(tmp_path / "fake.db", manifest)

    assert "substr(profile_id, 1, 37)" in connection.profile_query
    assert "substr(name, 1, 201)" in connection.profile_query
    assert "substr(engine, 1, 33)" in connection.profile_query


def test_database_profile_sqlite_length_limit_is_translated_to_validation(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    archive = tmp_path / "sqlite-length-limit.automadin-bundle"
    export_portability_bundle(
        database,
        archive,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    manifest = inspect_portability_bundle(
        archive,
        profiles_root=tmp_path / "other-profiles",
        classify_local_profiles=False,
    ).manifest
    oversized = tmp_path / "oversized-profile-text.db"
    with sqlite3.connect(oversized) as connection:
        connection.execute("CREATE TABLE profiles (profile_id TEXT, name TEXT, engine TEXT)")
        connection.execute(
            "INSERT INTO profiles (profile_id, name, engine) VALUES (?, ?, ?)",
            (record.profile_id, "x" * (128 * 1024), "firefox"),
        )

    with pytest.raises(PortabilityBundleValidationError, match="SQLite limits"):
        portability_bundle_module._database_profiles(oversized, manifest)


@pytest.mark.parametrize(
    "field",
    ["display_name", "engine", "reason", "source_app_version"],
)
def test_manifest_rejects_oversized_user_facing_text(workspace, tmp_path: Path, field: str) -> None:
    database, profiles, record = workspace
    archive = tmp_path / f"oversized-{field}.automadin-bundle"
    export_portability_bundle(
        database,
        archive,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read(BUNDLE_MANIFEST_PATH))
    if field == "display_name":
        manifest["profiles"][0]["display_name"] = "x" * 201
    elif field == "engine":
        manifest["profiles"][0]["engine"] = "x" * 33
    elif field == "reason":
        manifest["omitted_profiles"].append(
            {
                "profile_id": str(uuid.uuid4()),
                "reason": "x" * 201,
            }
        )
    else:
        manifest["source_app_version"] = "x" * 201

    with pytest.raises(PortabilityBundleValidationError, match="exceeds"):
        portability_bundle_module._parse_manifest(  # noqa: SLF001
            json.dumps(manifest).encode("utf-8"),
            BundleLimits(),
        )


def test_inspection_classifies_absent_and_different_local_storage(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    destination = tmp_path / "portable.automadin-bundle"
    export_portability_bundle(
        database,
        destination,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )

    absent = inspect_portability_bundle(destination, profiles_root=tmp_path / "other")
    assert absent.collisions[0].outcome is LocalProfileCollision.ABSENT

    (profiles / record.profile_id / "prefs.js").write_text("changed", encoding="utf-8")
    different = inspect_portability_bundle(destination, profiles_root=profiles)
    assert different.collisions[0].outcome is LocalProfileCollision.DIFFERENT


def test_export_uses_consistent_sqlite_backup_with_wal(workspace, tmp_path: Path) -> None:
    database, profiles, _record = workspace
    database._conn.execute(  # noqa: SLF001
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("portable-test", "present", "2026-07-18T00:00:00Z"),
    )
    database._conn.commit()  # noqa: SLF001
    destination = tmp_path / "portable.automadin-bundle"

    export_portability_bundle(database, destination, profiles_root=profiles)

    with zipfile.ZipFile(destination) as archive:
        bundled_db = tmp_path / "bundled.db"
        bundled_db.write_bytes(archive.read("database/automadin.db"))
    with sqlite3.connect(bundled_db) as connection:
        value = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'portable-test'"
        ).fetchone()
    assert value == ("present",)


def test_export_holds_sorted_maintenance_leases_and_releases_on_failure(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(
        record.profile_id, ProfileUse("session", "live browser", "owner")
    )
    with pytest.raises(BrowserProfileInUseError):
        export_portability_bundle(
            database,
            tmp_path / "blocked.automadin-bundle",
            [record.profile_id],
            profiles_root=profiles,
            lease_registry=registry,
        )
    assert not (tmp_path / "blocked.automadin-bundle").exists()
    owner.release()
    replacement = registry.acquire_maintenance(
        record.profile_id, ProfileUse("maintenance", "after failure", "replacement")
    )
    replacement.release()


def test_export_release_failure_preserves_retryable_scope_and_final_archive(
    workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, profiles, record = workspace
    destination = tmp_path / "cleanup-uncertain.automadin-bundle"
    registry = BrowserProfileLeaseRegistry()
    original_release = MaintenanceProfileScope.release

    def report_cleanup_failure(scope: MaintenanceProfileScope) -> bool:
        raise BrowserProfileLeaseCleanupError(scope)

    monkeypatch.setattr(MaintenanceProfileScope, "release", report_cleanup_failure)
    result = export_portability_bundle(
        database,
        destination,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=registry,
    )

    assert destination.is_file()
    assert result.cleanup_error
    assert result.maintenance_cleanup_scope is not None
    assert result.maintenance_cleanup_scope.profile_ids == (record.profile_id,)
    result.maintenance_cleanup_scope.ensure_live()
    assert original_release(result.maintenance_cleanup_scope)
    assert registry.snapshot(record.profile_id) is None


def test_export_primary_failure_chains_retryable_cleanup_scope(
    workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, profiles, record = workspace
    registry = BrowserProfileLeaseRegistry()
    original_release = MaintenanceProfileScope.release

    def fail_inspection(*args, **kwargs):
        raise RuntimeError("primary export failure")

    def fail_cleanup(scope: MaintenanceProfileScope) -> bool:
        raise BrowserProfileLeaseCleanupError(scope)

    monkeypatch.setattr(portability_bundle_module, "inspect_profile_storage", fail_inspection)
    monkeypatch.setattr(MaintenanceProfileScope, "release", fail_cleanup)
    with pytest.raises(RuntimeError, match="primary export failure") as raised:
        export_portability_bundle(
            database,
            tmp_path / "failed.automadin-bundle",
            [record.profile_id],
            profiles_root=profiles,
            lease_registry=registry,
        )

    assert isinstance(raised.value.__cause__, BrowserProfileLeaseCleanupError)
    cleanup = raised.value.__cause__
    cleanup.scope.ensure_live()
    assert original_release(cleanup.scope)


def test_cancellation_removes_temporary_and_final_archives(workspace, tmp_path: Path) -> None:
    database, profiles, record = workspace
    destination = tmp_path / "cancelled.automadin-bundle"
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    with pytest.raises(PortabilityBundleCancelledError):
        export_portability_bundle(
            database,
            destination,
            [record.profile_id],
            profiles_root=profiles,
            lease_registry=BrowserProfileLeaseRegistry(),
            cancel=cancel,
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


@pytest.mark.parametrize(
    "member_name",
    ["../escape", "/absolute", "C:/drive", "profiles\\bad", "safe/../escape", "x:NTS"],
)
def test_inspection_rejects_unsafe_member_paths(
    workspace, tmp_path: Path, member_name: str
) -> None:
    database, profiles, _record = workspace
    valid = tmp_path / "valid.automadin-bundle"
    export_portability_bundle(database, valid, profiles_root=profiles)
    malicious = tmp_path / "malicious.automadin-bundle"
    with zipfile.ZipFile(valid) as source, zipfile.ZipFile(malicious, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item))
        target.writestr(member_name, b"bad")

    with pytest.raises(PortabilityBundleValidationError, match="Unsafe|member set"):
        inspect_portability_bundle(malicious, profiles_root=profiles)


def test_inspection_rejects_casefold_duplicate_and_symlink(workspace, tmp_path: Path) -> None:
    database, profiles, _record = workspace
    valid = tmp_path / "valid.automadin-bundle"
    export_portability_bundle(database, valid, profiles_root=profiles)
    malicious = tmp_path / "malicious.automadin-bundle"
    with zipfile.ZipFile(valid) as source, zipfile.ZipFile(malicious, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item))
        target.writestr("BUNDLE.JSON", b"duplicate")
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        target.writestr(link, b"target")

    with pytest.raises(PortabilityBundleValidationError):
        inspect_portability_bundle(malicious, profiles_root=profiles)


def test_inspection_rejects_hash_tampering(workspace, tmp_path: Path) -> None:
    database, profiles, _record = workspace
    valid = tmp_path / "valid.automadin-bundle"
    export_portability_bundle(database, valid, profiles_root=profiles)
    malicious = tmp_path / "malicious.automadin-bundle"
    with zipfile.ZipFile(valid) as source, zipfile.ZipFile(malicious, "w") as target:
        for item in source.infolist():
            payload = source.read(item)
            if item.filename == "database/automadin.db":
                payload += b"tamper"
            target.writestr(item, payload)

    with pytest.raises(PortabilityBundleValidationError, match="size|hash"):
        inspect_portability_bundle(malicious, profiles_root=profiles)


def test_inspection_enforces_archive_and_manifest_limits(workspace, tmp_path: Path) -> None:
    database, profiles, _record = workspace
    valid = tmp_path / "valid.automadin-bundle"
    export_portability_bundle(database, valid, profiles_root=profiles)

    with pytest.raises(PortabilityBundleValidationError, match="archive limit"):
        inspect_portability_bundle(
            valid,
            profiles_root=profiles,
            limits=BundleLimits(max_archive_bytes=1),
        )
    with zipfile.ZipFile(valid) as archive:
        manifest_size = archive.getinfo(BUNDLE_MANIFEST_PATH).file_size
    with pytest.raises(PortabilityBundleValidationError, match="bundle.json"):
        inspect_portability_bundle(
            valid,
            profiles_root=profiles,
            limits=BundleLimits(max_manifest_bytes=manifest_size - 1),
        )


def test_export_detects_source_file_change(workspace, tmp_path: Path) -> None:
    database, profiles, record = workspace
    from automadin.services import portability_bundle

    real_signature = portability_bundle._lstat_signature
    target = profiles / record.profile_id / "prefs.js"
    reads = 0

    def mutating_signature(path: Path):
        nonlocal reads
        signature = real_signature(path)
        if path == target:
            reads += 1
            if reads == 2:
                target.write_text("mutated during export", encoding="utf-8")
        return signature

    with (
        patch.object(portability_bundle, "_lstat_signature", mutating_signature),
        pytest.raises(PortabilityBundleValidationError, match="changed"),
    ):
        export_portability_bundle(
            database,
            tmp_path / "changed.automadin-bundle",
            [record.profile_id],
            profiles_root=profiles,
            lease_registry=BrowserProfileLeaseRegistry(),
        )


def test_export_rejects_destination_inside_managed_profiles(
    workspace,
) -> None:
    database, profiles, _record = workspace
    destination = profiles / "backup.automadin-bundle"

    with pytest.raises(PortabilityBundleValidationError, match="inside managed"):
        export_portability_bundle(database, destination, profiles_root=profiles)

    assert not destination.exists()


def test_export_profile_traversal_uses_custom_limits(workspace, tmp_path: Path) -> None:
    database, profiles, record = workspace
    with pytest.raises(PortabilityBundleValidationError, match="Unsafe"):
        export_portability_bundle(
            database,
            tmp_path / "limited.automadin-bundle",
            [record.profile_id],
            profiles_root=profiles,
            lease_registry=BrowserProfileLeaseRegistry(),
            limits=BundleLimits(max_path_length=8),
        )


def test_progress_callbacks_are_headless_and_bounded(workspace, tmp_path: Path) -> None:
    database, profiles, record = workspace
    export_progress: list[BundleProgress] = []
    destination = tmp_path / "progress.automadin-bundle"

    export_portability_bundle(
        database,
        destination,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
        progress=export_progress.append,
    )
    inspect_progress: list[BundleProgress] = []
    inspect_portability_bundle(
        destination,
        profiles_root=profiles,
        progress=inspect_progress.append,
    )

    assert export_progress[-1].phase == "complete"
    assert inspect_progress[-1].phase == "complete"
    assert all(item.completed_bytes <= item.total_bytes for item in export_progress)
    assert all(item.completed_bytes <= item.total_bytes for item in inspect_progress)


def test_export_self_validation_does_not_hash_local_collision_again(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    with patch(
        "automadin.services.portability_bundle._classify_collision",
        side_effect=AssertionError("local collision scan should be skipped"),
    ):
        export_portability_bundle(
            database,
            tmp_path / "single-pass-local.automadin-bundle",
            [record.profile_id],
            profiles_root=profiles,
            lease_registry=BrowserProfileLeaseRegistry(),
        )


def test_export_fails_before_archive_write_when_free_space_is_insufficient(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    destination = tmp_path / "no-space.automadin-bundle"
    with (
        patch(
            "automadin.services.portability_bundle.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ),
        pytest.raises(PortabilityBundleValidationError, match="free space"),
    ):
        export_portability_bundle(
            database,
            destination,
            [record.profile_id],
            profiles_root=profiles,
            lease_registry=BrowserProfileLeaseRegistry(),
        )
    assert not destination.exists()


def test_inspection_supports_cooperative_cancellation(workspace, tmp_path: Path) -> None:
    database, profiles, _record = workspace
    destination = tmp_path / "inspect-cancel.automadin-bundle"
    export_portability_bundle(database, destination, profiles_root=profiles)

    with pytest.raises(PortabilityBundleCancelledError):
        inspect_portability_bundle(
            destination,
            profiles_root=profiles,
            cancel=lambda: True,
        )


def test_cancellation_during_local_collision_hash_propagates(workspace, tmp_path: Path) -> None:
    database, profiles, record = workspace
    destination = tmp_path / "collision-hash-cancel.automadin-bundle"
    export_portability_bundle(
        database,
        destination,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    phase = ""
    classification_checks = 0

    def progress(update: BundleProgress) -> None:
        nonlocal phase
        phase = update.phase

    def cancel() -> bool:
        nonlocal classification_checks
        if phase != "classifying_local_profiles":
            return False
        classification_checks += 1
        return classification_checks >= 2

    with pytest.raises(PortabilityBundleCancelledError):
        inspect_portability_bundle(
            destination,
            profiles_root=profiles,
            progress=progress,
            cancel=cancel,
        )

    assert classification_checks >= 2


def test_database_inspection_cancellation_is_reported_as_bundle_cancellation(
    workspace, tmp_path: Path
) -> None:
    database, profiles, _record = workspace
    destination = tmp_path / "database-inspection-cancel.automadin-bundle"
    export_portability_bundle(database, destination, profiles_root=profiles)

    def never_cancel() -> bool:
        return False

    def cancel_database(_path, *, cancel=None):
        assert cancel is not None
        raise DatabaseInspectionCancelledError("database check cancelled")

    with (
        patch.object(
            portability_bundle_module,
            "inspect_database",
            side_effect=cancel_database,
        ),
        pytest.raises(PortabilityBundleCancelledError, match="cancelled"),
    ):
        inspect_portability_bundle(
            destination,
            profiles_root=profiles,
            cancel=never_cancel,
        )


def _standard_eocd(
    *,
    entries: int,
    central_size: int,
    central_offset: int = 0,
    disk: int = 0,
    central_disk: int = 0,
    entries_on_disk: int | None = None,
) -> bytes:
    return struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06",
        disk,
        central_disk,
        entries if entries_on_disk is None else entries_on_disk,
        entries,
        central_size,
        central_offset,
        0,
    )


def _zip64_eocd(
    *,
    entries: int,
    central_size: int,
    central_offset: int = 0,
    disk: int = 0,
    record_offset: int = 0,
) -> bytes:
    record = struct.pack(
        "<4sQHHIIQQQQ",
        b"PK\x06\x06",
        44,
        45,
        45,
        disk,
        0,
        entries,
        entries,
        central_size,
        central_offset,
    )
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, record_offset, 1)
    eocd = struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    return record + locator + eocd


@pytest.mark.parametrize(
    ("payload", "limits", "message"),
    [
        (
            _standard_eocd(entries=2, central_size=1),
            BundleLimits(max_members=1),
            "member count",
        ),
        (
            _standard_eocd(entries=1, central_size=65),
            BundleLimits(max_central_directory_bytes=64),
            "central directory",
        ),
        (
            _zip64_eocd(entries=2, central_size=1),
            BundleLimits(max_members=1),
            "member count",
        ),
        (
            _zip64_eocd(entries=1, central_size=65),
            BundleLimits(max_central_directory_bytes=64),
            "central directory",
        ),
    ],
)
def test_raw_eocd_limits_reject_before_zipfile_materializes_entries(
    tmp_path: Path,
    payload: bytes,
    limits: BundleLimits,
    message: str,
) -> None:
    archive = tmp_path / "hostile.zip"
    archive.write_bytes(payload)

    with (
        patch(
            "automadin.services.portability_bundle.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not be constructed"),
        ),
        pytest.raises(PortabilityBundleValidationError, match=message),
    ):
        inspect_portability_bundle(archive, limits=limits)


def test_raw_preflight_counts_headers_when_eocd_underreports_members(
    tmp_path: Path,
) -> None:
    central_header = b"PK\x01\x02" + (b"\x00" * 42)
    central_directory = central_header * 2
    archive = tmp_path / "lying-count.zip"
    archive.write_bytes(
        central_directory
        + _standard_eocd(
            entries=1,
            central_size=len(central_directory),
            central_offset=0,
        )
    )

    with (
        patch(
            "automadin.services.portability_bundle.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not be constructed"),
        ),
        pytest.raises(PortabilityBundleValidationError, match="member count"),
    ):
        inspect_portability_bundle(
            archive,
            limits=BundleLimits(max_members=1),
        )


def test_raw_preflight_accepts_consistent_single_disk_zip64_metadata(
    tmp_path: Path,
) -> None:
    central_header = b"PK\x01\x02" + (b"\x00" * 42)
    archive = tmp_path / "bounded-zip64.zip"
    archive.write_bytes(
        central_header
        + _zip64_eocd(
            entries=1,
            central_size=len(central_header),
            central_offset=0,
            record_offset=len(central_header),
        )
    )

    result = preflight_zip_central_directory(archive)

    assert result.zip64
    assert result.member_count == 1
    assert result.central_directory_size == len(central_header)


def test_archive_rejects_case_equivalent_directory_prefixes(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "case-prefix-collision.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("Foo/first.bin", b"first")
        target.writestr("foo/second.bin", b"second")

    with pytest.raises(
        PortabilityBundleValidationError,
        match="case-equivalent.*prefix",
    ):
        inspect_portability_bundle(archive)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-a-zip", "too small"),
        (
            _standard_eocd(
                entries=0,
                central_size=0,
                disk=1,
                central_disk=1,
            ),
            "Multi-disk",
        ),
        (
            struct.pack(
                "<4sHHHHIIH",
                b"PK\x05\x06",
                0,
                0,
                0xFFFF,
                0xFFFF,
                0xFFFFFFFF,
                0xFFFFFFFF,
                0,
            ),
            "ZIP64 locator",
        ),
        (
            _standard_eocd(entries=1, central_size=10, central_offset=20),
            "offset",
        ),
    ],
)
def test_raw_zip_preflight_rejects_malformed_or_multidisk_metadata(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    archive = tmp_path / "malformed.zip"
    archive.write_bytes(payload)

    with pytest.raises(PortabilityBundleValidationError, match=message):
        preflight_zip_central_directory(archive)


def test_export_progress_exceptions_cannot_turn_published_backup_into_failure(
    workspace, tmp_path: Path
) -> None:
    database, profiles, record = workspace
    destination = tmp_path / "progress-raises.automadin-bundle"

    def raising_progress(_progress: BundleProgress) -> None:
        raise RuntimeError("UI receiver disappeared")

    result = export_portability_bundle(
        database,
        destination,
        [record.profile_id],
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
        progress=raising_progress,
    )

    assert result.path == destination
    assert destination.is_file()
