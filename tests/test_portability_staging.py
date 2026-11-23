from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from automadin.services import portability_staging as portability_staging_module
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseCleanupError,
    BrowserProfileLeaseRegistry,
    MaintenanceProfileScope,
    ProfileUse,
)
from automadin.services.browser_profile_storage import create_clean_profile
from automadin.services.database import DatabaseService
from automadin.services.portability_activation import (
    ACTIVATION_PLAN_NAME,
    apply_pending_portability_activation,
)
from automadin.services.portability_bundle import (
    BundleProgress,
    LocalProfileCollision,
    OmittedProfileCollision,
    PortabilityBundleCancelledError,
    PortabilityBundleValidationError,
    export_portability_bundle,
    inspect_portability_bundle,
)
from automadin.services.portability_staging import (
    PortabilityStagingError,
    PortabilityStagingRecoveryError,
    ProfileRestoreDecision,
    stage_portability_bundle_import,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def portable_source(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_db = DatabaseService(str(source_root / "automadin.db"))
    source_profiles = source_root / "profiles"
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    with registry.acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", "create portable source", "staging-fixture-1"),
    ) as lease:
        change = create_clean_profile(
            profile_id,
            "firefox",
            lease_registry=registry,
            maintenance_lease=lease,
            profiles_root=source_profiles,
        )
        (change.path / "prefs.js").write_text("portable-profile", encoding="utf-8")
        change.commit()
    record = source_db.create_profile(
        "Portable Firefox",
        profile_id=profile_id,
        browser="Firefox",
        engine="firefox",
    )
    archive = tmp_path / "workspace.automadin-bundle"
    export_portability_bundle(
        source_db,
        archive,
        [profile_id],
        profiles_root=source_profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    source_db.close()
    yield archive, source_root / "automadin.db", source_profiles, record


def _target(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "target"
    data.mkdir()
    active = data / "automadin.db"
    database = DatabaseService(str(active))
    database.set_app_setting("marker", "target-stays-active")
    database.close()
    profiles = data / "profiles"
    profiles.mkdir()
    return active, profiles


def test_install_stages_profile_and_durable_activation_without_mutating_live_data(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    active_before = _sha256(active)
    source_before = _sha256(archive)
    source_database_before = _sha256(_source_db)
    source_profiles_before = _tree_bytes(_source_profiles)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    assert inspection.collisions[0].outcome is LocalProfileCollision.ABSENT
    progress: list[BundleProgress] = []

    result = stage_portability_bundle_import(
        archive,
        inspection,
        active,
        {record.profile_id: ProfileRestoreDecision.INSTALL},
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
        progress=progress.append,
    )

    assert _sha256(active) == active_before
    assert _sha256(archive) == source_before
    assert _sha256(_source_db) == source_database_before
    assert _tree_bytes(_source_profiles) == source_profiles_before
    assert not (profiles / record.profile_id).exists()
    assert result.activation_plan.path == active.parent / ACTIVATION_PLAN_NAME
    assert result.activation_plan.path.is_file()
    assert result.activation_plan.private_database_path.is_file()
    assert result.installed_profile_ids == (record.profile_id,)
    assert result.staged_profile_paths[0].is_dir()
    assert (result.staged_profile_paths[0] / "prefs.js").read_text(
        encoding="utf-8"
    ) == "portable-profile"
    assert progress[-1].phase == "complete"


def test_keep_local_hashes_payload_but_preserves_existing_profile(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    shutil.copytree(source_profiles / record.profile_id, profiles / record.profile_id)
    (profiles / record.profile_id / "prefs.js").write_text(
        "local-is-deliberately-different",
        encoding="utf-8",
    )
    local_before = _tree_bytes(profiles / record.profile_id)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    assert inspection.collisions[0].outcome is LocalProfileCollision.DIFFERENT

    result = stage_portability_bundle_import(
        archive,
        inspection,
        active,
        {record.profile_id: ProfileRestoreDecision.KEEP_LOCAL},
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )

    assert result.kept_local_profile_ids == (record.profile_id,)
    assert result.staged_profile_paths == ()
    assert _tree_bytes(profiles / record.profile_id) == local_before


def test_identical_keep_requires_review_if_local_bytes_drift(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    local_profile = profiles / record.profile_id
    shutil.copytree(source_profiles / record.profile_id, local_profile)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    assert inspection.collisions[0].outcome is LocalProfileCollision.IDENTICAL
    (local_profile / "prefs.js").write_text("changed after review", encoding="utf-8")

    with pytest.raises(PortabilityStagingError, match="review.*again"):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.KEEP_LOCAL},
            profiles_root=profiles,
            lease_registry=BrowserProfileLeaseRegistry(),
        )

    assert not list(profiles.glob(".importing-*"))
    assert not list(active.parent.glob(".automadin-portability-staging-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_identical_keep_persists_reviewed_fingerprint_and_blocks_restart_drift(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    local_profile = profiles / record.profile_id
    shutil.copytree(source_profiles / record.profile_id, local_profile)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    assert inspection.collisions[0].outcome is LocalProfileCollision.IDENTICAL
    active_before = _sha256(active)

    result = stage_portability_bundle_import(
        archive,
        inspection,
        active,
        {record.profile_id: ProfileRestoreDecision.KEEP_LOCAL},
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    journal = json.loads(result.activation_plan.path.read_text(encoding="utf-8"))
    reviewed = journal["kept_local"][0]["reviewed_fingerprint"]
    assert len(reviewed) == 64
    (local_profile / "prefs.js").write_text(
        "changed after durable review",
        encoding="utf-8",
    )

    applied = apply_pending_portability_activation(
        active.parent,
        profiles_root=profiles,
    )

    assert applied is not None and not applied.applied
    assert "changed after collision review" in str(applied.error)
    assert _sha256(active) == active_before


def test_explicit_different_keep_accepts_later_safe_local_change(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    local_profile = profiles / record.profile_id
    shutil.copytree(source_profiles / record.profile_id, local_profile)
    (local_profile / "prefs.js").write_text("first local version", encoding="utf-8")
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    assert inspection.collisions[0].outcome is LocalProfileCollision.DIFFERENT
    (local_profile / "prefs.js").write_text("second local version", encoding="utf-8")

    result = stage_portability_bundle_import(
        archive,
        inspection,
        active,
        {record.profile_id: ProfileRestoreDecision.KEEP_LOCAL},
        profiles_root=profiles,
        lease_registry=BrowserProfileLeaseRegistry(),
    )

    assert result.kept_local_profile_ids == (record.profile_id,)
    assert (local_profile / "prefs.js").read_text(encoding="utf-8") == ("second local version")
    journal = json.loads(result.activation_plan.path.read_text(encoding="utf-8"))
    assert journal["kept_local"][0]["reviewed_fingerprint"] == ""


def test_kept_profile_hardlink_added_after_inspection_is_blocked(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    local_profile = profiles / record.profile_id
    shutil.copytree(source_profiles / record.profile_id, local_profile)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    shared = tmp_path / "shared-profile-data"
    shared.write_text("shared", encoding="utf-8")
    linked = local_profile / "nested" / "linked-data"
    linked.parent.mkdir()
    try:
        os.link(shared, linked)
    except OSError as exc:
        pytest.skip(f"Hard links are unavailable on this filesystem: {exc}")

    with pytest.raises(PortabilityStagingError, match="unsafe tree"):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.KEEP_LOCAL},
            profiles_root=profiles,
            lease_registry=BrowserProfileLeaseRegistry(),
        )

    assert not list(profiles.glob(".importing-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_collision_decision_mismatch_fails_before_copy(portable_source, tmp_path: Path) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    active_before = _sha256(active)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)

    with pytest.raises(PortabilityStagingError, match="must be installed"):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.KEEP_LOCAL},
            profiles_root=profiles,
        )

    assert _sha256(active) == active_before
    assert not list(profiles.glob(".importing-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_tampered_payload_is_rejected_and_all_staging_is_cleaned(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    active_before = _sha256(active)
    rewritten = tmp_path / "rewritten.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(rewritten, "w") as target:
        for info in source.infolist():
            payload = source.read(info)
            if info.filename.endswith("/prefs.js"):
                payload = b"tampered-profile"
            target.writestr(info, payload)
    rewritten.replace(archive)

    with pytest.raises(PortabilityStagingError, match="size|hash"):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
        )

    assert _sha256(active) == active_before
    assert not list(profiles.glob(".importing-*"))
    assert not list(active.parent.glob(".automadin-portability-staging-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_staging_rejects_case_equivalent_directory_prefixes_before_extraction(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    rewritten = tmp_path / "case-prefix-rewrite.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(rewritten, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info))
        prefix = f"profiles/{record.profile_id}"
        target.writestr(f"{prefix}/Folder/first.bin", b"first")
        target.writestr(f"{prefix}/folder/second.bin", b"second")
    rewritten.replace(archive)

    with pytest.raises(
        PortabilityBundleValidationError,
        match="case-equivalent.*prefix",
    ):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
        )

    assert not list(profiles.glob(".importing-*"))
    assert not list(active.parent.glob(".automadin-portability-staging-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_cancellation_cleans_profile_and_database_staging(portable_source, tmp_path: Path) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls > 4

    with pytest.raises(PortabilityBundleCancelledError):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
            cancel=cancel,
        )
    assert not list(profiles.glob(".importing-*"))
    assert not list(active.parent.glob(".automadin-portability-staging-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_final_cancellation_seam_prevents_activation_plan_publication(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    real_validate = portability_staging_module._validate_current_local_state
    validations = 0
    cancel_now = False

    def validate(*args, **kwargs) -> None:
        nonlocal validations, cancel_now
        real_validate(*args, **kwargs)
        validations += 1
        if validations == 3:
            cancel_now = True

    with (
        patch.object(
            portability_staging_module,
            "_validate_current_local_state",
            side_effect=validate,
        ),
        patch.object(
            portability_staging_module,
            "stage_portability_activation",
            side_effect=AssertionError("activation publication must not begin"),
        ),
        pytest.raises(PortabilityBundleCancelledError),
    ):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
            cancel=lambda: cancel_now,
        )

    assert validations == 3
    assert not list(profiles.glob(".importing-*"))
    assert not list(active.parent.glob(".automadin-portability-staging-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_existing_pending_activation_is_rejected_before_copy(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    pending = active.parent / ACTIVATION_PLAN_NAME
    pending.write_text("existing owner", encoding="utf-8")

    with pytest.raises(FileExistsError, match="pending"):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
        )

    assert pending.read_text(encoding="utf-8") == "existing owner"
    assert not list(profiles.glob(".importing-*"))


def test_owner_lease_blocks_staging_without_mutation(portable_source, tmp_path: Path) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(
        record.profile_id, ProfileUse("session", "live browser", "owner")
    )
    try:
        with pytest.raises(BrowserProfileInUseError):
            stage_portability_bundle_import(
                archive,
                inspection,
                active,
                {record.profile_id: ProfileRestoreDecision.INSTALL},
                profiles_root=profiles,
                lease_registry=registry,
            )
    finally:
        owner.release()
    assert not list(profiles.glob(".importing-*"))


def test_staging_release_failure_preserves_retryable_scope_and_activation_plan(
    portable_source,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    registry = BrowserProfileLeaseRegistry()
    original_release = MaintenanceProfileScope.release

    def report_cleanup_failure(scope: MaintenanceProfileScope) -> bool:
        raise BrowserProfileLeaseCleanupError(scope)

    monkeypatch.setattr(MaintenanceProfileScope, "release", report_cleanup_failure)
    result = stage_portability_bundle_import(
        archive,
        inspection,
        active,
        {record.profile_id: ProfileRestoreDecision.INSTALL},
        profiles_root=profiles,
        lease_registry=registry,
    )

    assert (active.parent / ACTIVATION_PLAN_NAME).is_file()
    assert list(profiles.glob(f".importing-{record.profile_id}-*"))
    assert result.cleanup_error
    assert result.maintenance_cleanup_scope is not None
    assert result.maintenance_cleanup_scope.profile_ids == (record.profile_id,)
    result.maintenance_cleanup_scope.ensure_live()
    assert original_release(result.maintenance_cleanup_scope)
    assert registry.snapshot(record.profile_id) is None


def test_staging_primary_failure_chains_retryable_cleanup_scope(
    portable_source, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    registry = BrowserProfileLeaseRegistry()
    original_release = MaintenanceProfileScope.release
    validations = 0

    def fail_second_validation(*args, **kwargs) -> None:
        nonlocal validations
        validations += 1
        if validations == 2:
            raise RuntimeError("primary staging failure")

    def fail_cleanup(scope: MaintenanceProfileScope) -> bool:
        raise BrowserProfileLeaseCleanupError(scope)

    monkeypatch.setattr(
        portability_staging_module,
        "_validate_current_local_state",
        fail_second_validation,
    )
    monkeypatch.setattr(MaintenanceProfileScope, "release", fail_cleanup)
    with pytest.raises(RuntimeError, match="primary staging failure") as raised:
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
            lease_registry=registry,
        )

    assert isinstance(raised.value.__cause__, BrowserProfileLeaseCleanupError)
    cleanup = raised.value.__cause__
    cleanup.scope.ensure_live()
    assert original_release(cleanup.scope)


def test_activation_publication_failure_cleans_all_private_staging(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    active_before = _sha256(active)

    with (
        patch(
            "automadin.services.portability_staging.stage_portability_activation",
            side_effect=RuntimeError("simulated publication failure"),
        ),
        pytest.raises(RuntimeError, match="publication failure"),
    ):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
        )

    assert not list(profiles.glob(".importing-*"))
    assert not list(active.parent.glob(".automadin-portability-staging-*"))
    assert _sha256(active) == active_before


def test_cleanup_uncertainty_reports_exact_recovery_paths(portable_source, tmp_path: Path) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)

    with (
        patch(
            "automadin.services.portability_staging._safe_remove_staging",
            side_effect=OSError("simulated cleanup uncertainty"),
        ),
        pytest.raises(PortabilityBundleCancelledError) as raised,
    ):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
            cancel=lambda: True,
        )

    assert isinstance(raised.value.__cause__, PortabilityStagingRecoveryError)
    recovery_paths = raised.value.__cause__.recovery_paths
    assert recovery_paths
    assert all(path.parent in {profiles, active.parent} for path in recovery_paths)


def test_shared_path_validation_rejects_overlong_component(portable_source, tmp_path: Path) -> None:
    archive, _source_db, _source_profiles, _record = portable_source
    malicious = tmp_path / "long-component.automadin-bundle"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(malicious, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info))
        target.writestr("x" * 256, b"unsafe")

    with pytest.raises(PortabilityBundleValidationError, match="Unsafe"):
        inspect_portability_bundle(malicious, profiles_root=tmp_path / "other-profiles")


def _database_only_bundle(
    source_db: Path,
    destination: Path,
) -> None:
    database = DatabaseService(str(source_db))
    try:
        export_portability_bundle(database, destination)
    finally:
        database.close()


def test_database_only_bundle_stages_on_clean_target_without_profile_decisions(
    portable_source, tmp_path: Path
) -> None:
    _archive, source_db, _source_profiles, record = portable_source
    archive = tmp_path / "database-only.automadin-bundle"
    _database_only_bundle(source_db, archive)
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)

    assert inspection.manifest.profiles == ()
    assert inspection.omitted_collisions[0].profile_id == record.profile_id
    assert inspection.omitted_collisions[0].outcome is OmittedProfileCollision.ABSENT
    result = stage_portability_bundle_import(
        archive,
        inspection,
        active,
        {},
        profiles_root=profiles,
    )

    assert result.installed_profile_ids == ()
    assert result.kept_local_profile_ids == ()
    assert result.activation_plan.path.is_file()
    applied = apply_pending_portability_activation(
        active.parent,
        profiles_root=profiles,
    )
    assert applied is not None and applied.applied


def test_database_only_same_origin_requires_explicit_keep_local(
    portable_source, tmp_path: Path
) -> None:
    _archive, source_db, source_profiles, record = portable_source
    archive = tmp_path / "database-only-local.automadin-bundle"
    _database_only_bundle(source_db, archive)
    active, profiles = _target(tmp_path)
    shutil.copytree(source_profiles / record.profile_id, profiles / record.profile_id)
    local_before = _tree_bytes(profiles / record.profile_id)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    assert inspection.omitted_collisions[0].outcome is OmittedProfileCollision.READY

    with pytest.raises(PortabilityStagingError, match="required restore action"):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {},
            profiles_root=profiles,
        )

    result = stage_portability_bundle_import(
        archive,
        inspection,
        active,
        {record.profile_id: ProfileRestoreDecision.KEEP_LOCAL},
        profiles_root=profiles,
    )
    assert result.kept_local_profile_ids == (record.profile_id,)
    journal = json.loads(result.activation_plan.path.read_text(encoding="utf-8"))
    assert journal["kept_local"][0]["reviewed_fingerprint"] == ""
    assert _tree_bytes(profiles / record.profile_id) == local_before
    applied = apply_pending_portability_activation(
        active.parent,
        profiles_root=profiles,
    )
    assert applied is not None and applied.applied
    assert _tree_bytes(profiles / record.profile_id) == local_before


def test_database_only_omitted_profile_with_nested_hardlink_is_unsafe(
    portable_source, tmp_path: Path
) -> None:
    _archive, source_db, source_profiles, record = portable_source
    archive = tmp_path / "database-only-hardlink.automadin-bundle"
    _database_only_bundle(source_db, archive)
    _active, profiles = _target(tmp_path)
    local_profile = profiles / record.profile_id
    shutil.copytree(source_profiles / record.profile_id, local_profile)
    shared = tmp_path / "shared-omitted-data"
    shared.write_text("shared", encoding="utf-8")
    linked = local_profile / "nested" / "linked-data"
    linked.parent.mkdir()
    try:
        os.link(shared, linked)
    except OSError as exc:
        pytest.skip(f"Hard links are unavailable on this filesystem: {exc}")

    inspection = inspect_portability_bundle(archive, profiles_root=profiles)

    assert inspection.omitted_collisions[0].outcome is OmittedProfileCollision.UNSAFE
    assert "hard-linked" in inspection.omitted_collisions[0].detail


@pytest.mark.parametrize("unsafe_kind", ["engine", "manifest"])
def test_database_only_omitted_mismatch_or_unsafe_storage_blocks(
    portable_source, tmp_path: Path, unsafe_kind: str
) -> None:
    _archive, source_db, _source_profiles, record = portable_source
    archive = tmp_path / f"database-only-{unsafe_kind}.automadin-bundle"
    _database_only_bundle(source_db, archive)
    active, profiles = _target(tmp_path)
    if unsafe_kind == "engine":
        registry = BrowserProfileLeaseRegistry()
        with registry.acquire_maintenance(
            record.profile_id,
            ProfileUse("maintenance", "create mismatch profile", "staging-fixture-2"),
        ) as lease:
            change = create_clean_profile(
                record.profile_id,
                "chromium",
                lease_registry=registry,
                maintenance_lease=lease,
                profiles_root=profiles,
            )
            change.commit()
        expected = OmittedProfileCollision.ENGINE_MISMATCH
    else:
        corrupt = profiles / record.profile_id
        corrupt.mkdir()
        (corrupt / "profile.json").write_text("not-json", encoding="utf-8")
        expected = OmittedProfileCollision.UNSAFE
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    assert inspection.omitted_collisions[0].outcome is expected

    with pytest.raises(PortabilityStagingError, match="blocked"):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {},
            profiles_root=profiles,
        )

    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_noncanonical_active_database_path_is_rejected(portable_source, tmp_path: Path) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    data = tmp_path / "noncanonical-target"
    data.mkdir()
    wrong_active = data / "another-name.db"
    database = DatabaseService(str(wrong_active))
    database.close()
    profiles = data / "profiles"
    profiles.mkdir()
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    before = _sha256(wrong_active)

    with pytest.raises(PortabilityStagingError, match="automadin.db"):
        stage_portability_bundle_import(
            archive,
            inspection,
            wrong_active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
        )

    assert _sha256(wrong_active) == before
    assert not (data / ACTIVATION_PLAN_NAME).exists()


def test_free_space_estimate_includes_existing_active_database_wal(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    wal_size = 2 * 1024**2
    active.with_name(f"{active.name}-wal").write_bytes(b"x" * wal_size)
    database_size = inspection.manifest.database.size
    install_size = inspection.manifest.profiles[0].total_size
    old_data_required = max(
        database_size * 2,
        database_size + active.stat().st_size,
    )
    old_required = old_data_required + install_size
    old_required += max(64 * 1024**2, old_required // 20)
    free = old_required + wal_size // 2
    assert free > old_required

    with (
        patch(
            "automadin.services.portability_staging.shutil.disk_usage",
            return_value=SimpleNamespace(free=free),
        ),
        pytest.raises(PortabilityStagingError, match="free space"),
    ):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
        )

    assert not list(profiles.glob(".importing-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_hardlinked_active_database_wal_is_rejected(portable_source, tmp_path: Path) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)
    shared = tmp_path / "shared-wal"
    shared.write_bytes(b"unsafe wal")
    wal = active.with_name(f"{active.name}-wal")
    try:
        os.link(shared, wal)
    except OSError as exc:
        pytest.skip(f"Hard links are unavailable on this filesystem: {exc}")

    with pytest.raises(
        PortabilityStagingError,
        match="database WAL.*hard-linked",
    ):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
        )

    assert not list(profiles.glob(".importing-*"))
    assert not (active.parent / ACTIVATION_PLAN_NAME).exists()


def test_read_only_hardlinked_bundle_container_is_supported(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    linked_archive = tmp_path / "linked-container.automadin-bundle"
    try:
        os.link(archive, linked_archive)
    except OSError as exc:
        pytest.skip(f"Hard links are unavailable on this filesystem: {exc}")
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(linked_archive, profiles_root=profiles)

    result = stage_portability_bundle_import(
        linked_archive,
        inspection,
        active,
        {record.profile_id: ProfileRestoreDecision.INSTALL},
        profiles_root=profiles,
    )

    assert result.activation_plan.path.is_file()


def test_staging_reuses_raw_preflight_before_constructing_zipfile(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)

    with (
        patch(
            "automadin.services.portability_staging.bundle.preflight_zip_central_directory",
            side_effect=PortabilityBundleValidationError("raw preflight blocked"),
        ),
        patch(
            "automadin.services.portability_staging.zipfile.ZipFile",
            side_effect=AssertionError("ZipFile must not be constructed"),
        ),
        pytest.raises(PortabilityBundleValidationError, match="raw preflight"),
    ):
        stage_portability_bundle_import(
            archive,
            inspection,
            active,
            {record.profile_id: ProfileRestoreDecision.INSTALL},
            profiles_root=profiles,
        )

    assert not list(profiles.glob(".importing-*"))
    assert not list(active.parent.glob(".automadin-portability-staging-*"))


def test_progress_exceptions_cannot_escape_after_activation_plan_publication(
    portable_source, tmp_path: Path
) -> None:
    archive, _source_db, _source_profiles, record = portable_source
    active, profiles = _target(tmp_path)
    inspection = inspect_portability_bundle(archive, profiles_root=profiles)

    def raising_progress(_progress: BundleProgress) -> None:
        raise RuntimeError("Qt receiver was deleted")

    result = stage_portability_bundle_import(
        archive,
        inspection,
        active,
        {record.profile_id: ProfileRestoreDecision.INSTALL},
        profiles_root=profiles,
        progress=raising_progress,
    )

    assert result.activation_plan.path.is_file()
    assert result.activation_plan.path == active.parent / ACTIVATION_PLAN_NAME
