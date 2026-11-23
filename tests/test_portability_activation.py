from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from automadin.services import portability_activation
from automadin.services import browser_profile_storage as profile_storage
from automadin.services import database_transfer as database_transfer_module
from automadin.services.browser_profile_manifest import (
    BrowserProfileManifest,
    _write_profile_manifest as write_profile_manifest,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseCleanupError,
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.services.browser_profile_storage import (
    ProfileStorageRecoveryError,
    ProfileStorageValidationError,
    StagedProfilePublication,
    prepare_staged_profile_publication,
    profile_tree_lstat_fingerprint,
)
from automadin.services.database import DatabaseService
from automadin.services.database_transfer import (
    DatabaseImportResult,
    PENDING_IMPORT_NAME,
    apply_pending_database_import,
    apply_portability_owned_database_import,
    stage_database_import,
)
from automadin.services.portability_activation import (
    ACTIVATION_PLAN_NAME,
    PRIVATE_DATABASE_NAME,
    KeptLocalProfile,
    PortabilityActivationError,
    StagedProfileInput,
    apply_pending_portability_activation,
    stage_portability_activation as _stage_portability_activation,
)

PROFILE_ID = "11111111-1111-4111-8111-111111111111"
SECOND_PROFILE_ID = "22222222-2222-4222-8222-222222222222"


def stage_portability_activation(
    data_dir: str | Path,
    staged_database: str | Path,
    staged_profiles: tuple[StagedProfileInput, ...],
    kept_local_profiles: tuple[KeptLocalProfile, ...] = (),
    *,
    profiles_root: str | Path | None = None,
):
    """Stage test plans with exact, live maintenance capabilities."""
    registry = BrowserProfileLeaseRegistry()
    scope = registry.acquire_maintenance_many(
        (profile.profile_id for profile in staged_profiles),
        ProfileUse("maintenance", "staging test portability plan", str(uuid.uuid4())),
    )
    try:
        return _stage_portability_activation(
            data_dir,
            staged_database,
            staged_profiles,
            kept_local_profiles,
            lease_registry=registry,
            maintenance_leases={
                profile_id: scope.for_profile(profile_id)
                for profile_id in scope.profile_ids
            },
            profiles_root=profiles_root,
        )
    finally:
        scope.release()


def _database(path: Path, marker: str, *, engine: str = "chromium") -> None:
    database = DatabaseService(str(path))
    database.set_app_setting("marker", marker)
    database.create_profile(
        "Portable profile",
        profile_id=PROFILE_ID,
        browser="Chromium",
        engine=engine,
    )
    database.close()


def _marker(path: Path) -> str | None:
    database = DatabaseService(str(path))
    try:
        return database.get_app_setting("marker")
    finally:
        database.close()


def _profile(directory: Path, *, profile_id: str = PROFILE_ID, engine: str = "chromium") -> None:
    directory.mkdir(parents=True)
    if engine in {"chromium", "chrome", "msedge"}:
        (directory / "Default").mkdir()
        (directory / "Default" / "Cookies").write_bytes(b"session")
    else:
        (directory / "prefs.js").write_text("", encoding="utf-8")
    write_profile_manifest(
        directory,
        BrowserProfileManifest.create(engine, profile_id=profile_id),
    )


def _staging(profiles: Path) -> Path:
    path = profiles / f".importing-{PROFILE_ID}-{uuid.uuid4().hex}"
    _profile(path)
    return path


@contextmanager
def _prepared_publication(
    staging: Path,
    profile_id: str,
    engine: str,
    profiles_root: Path,
) -> Iterator[StagedProfilePublication]:
    registry = BrowserProfileLeaseRegistry()
    with registry.acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", "preparing test publication", str(uuid.uuid4())),
    ) as lease:
        yield prepare_staged_profile_publication(
            staging,
            profile_id,
            engine,
            lease_registry=registry,
            maintenance_lease=lease,
            profiles_root=profiles_root,
        )


def test_exact_profile_publication_is_idempotent_and_rollback_preserves_data(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    staging = _staging(profiles)
    with _prepared_publication(
        staging, PROFILE_ID, "chromium", profiles
    ) as publication:
        assert publication.publish()
        assert not publication.publish()
        assert (profiles / PROFILE_ID / "Default" / "Cookies").read_bytes() == b"session"
        assert publication.rollback()
        assert not publication.rollback()
    assert staging.is_dir()


def test_startup_owner_conflict_blocks_publication_and_unwinds_batch_leases(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    _database(data / "automadin.db", "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    imported_database = DatabaseService(str(imported))
    imported_database.create_profile(
        "Second portable profile",
        profile_id=SECOND_PROFILE_ID,
        browser="Chromium",
        engine="chromium",
    )
    imported_database.close()
    first_staging = _staging(profiles)
    second_staging = profiles / f".importing-{SECOND_PROFILE_ID}-{uuid.uuid4().hex}"
    _profile(second_staging, profile_id=SECOND_PROFILE_ID)
    stage_portability_activation(
        data,
        imported,
        (
            StagedProfileInput(PROFILE_ID, "chromium", first_staging),
            StagedProfileInput(SECOND_PROFILE_ID, "chromium", second_staging),
        ),
    )
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(
        SECOND_PROFILE_ID,
        ProfileUse("run", "active second profile", "owner-conflict"),
    )
    try:
        result = apply_pending_portability_activation(
            data,
            lease_registry=registry,
        )

        assert result is not None and not result.applied
        assert "already in use" in str(result.error)
        assert not (profiles / PROFILE_ID).exists()
        assert not (profiles / SECOND_PROFILE_ID).exists()
        assert first_staging.is_dir()
        assert second_staging.is_dir()
        assert (data / ACTIVATION_PLAN_NAME).is_file()
        assert (data / PRIVATE_DATABASE_NAME).is_file()
        assert registry.snapshot(PROFILE_ID) is None
        assert registry.snapshot(SECOND_PROFILE_ID).use == owner.use
    finally:
        owner.release()

    retry = apply_pending_portability_activation(data, lease_registry=registry)

    assert retry is not None and retry.applied
    assert retry.database_activated
    assert _marker(data / "automadin.db") == "imported"
    assert (profiles / PROFILE_ID).is_dir()
    assert (profiles / SECOND_PROFILE_ID).is_dir()
    assert not (data / ACTIVATION_PLAN_NAME).exists()


def test_startup_acquisition_cleanup_failure_preserves_retryable_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    _database(data / "automadin.db", "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    registry = BrowserProfileLeaseRegistry()
    failed_scope = registry.acquire_maintenance_many(
        (PROFILE_ID,),
        ProfileUse("maintenance", "simulated cleanup failure", str(uuid.uuid4())),
    )

    def fail_acquisition(*_args: object, **_kwargs: object) -> object:
        raise BrowserProfileLeaseCleanupError(failed_scope)

    monkeypatch.setattr(registry, "acquire_maintenance_many", fail_acquisition)
    result = apply_pending_portability_activation(data, lease_registry=registry)

    assert result is not None and not result.applied
    assert "left in place for retry" in str(result.error)
    assert result.maintenance_cleanup_scope is failed_scope
    result.maintenance_cleanup_scope.ensure_live()
    assert (data / ACTIVATION_PLAN_NAME).is_file()
    assert (data / PRIVATE_DATABASE_NAME).is_file()
    assert staging.is_dir()
    assert not (profiles / PROFILE_ID).exists()
    assert _marker(data / "automadin.db") == "old"
    result.maintenance_cleanup_scope.release()

    retry = apply_pending_portability_activation(
        data,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    assert retry is not None and retry.applied
    assert _marker(data / "automadin.db") == "imported"


def test_publication_rejects_changed_tree_and_casefold_collision(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    staging = _staging(profiles)
    with _prepared_publication(
        staging, PROFILE_ID, "chromium", profiles
    ) as publication:
        (staging / "Default" / "Cookies").write_bytes(b"changed")
        with pytest.raises(ProfileStorageRecoveryError, match="changed"):
            publication.publish()

    other = _staging(tmp_path / "other")
    original = other / "Default" / "original"
    original.write_text("a", encoding="utf-8")
    os.link(original, other / "Default" / "linked")
    with (
        pytest.raises(ProfileStorageValidationError, match="hard-linked"),
        _prepared_publication(other, PROFILE_ID, "chromium", tmp_path / "other"),
    ):
        pass


def test_publication_rejects_casefold_equivalent_siblings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    staging = _staging(profiles)
    default = staging / "Default"
    existing = default / "Case"
    existing.write_text("value", encoding="utf-8")
    real_scandir = profile_storage.os.scandir

    class _Entries:
        def __enter__(self):
            return iter(
                [
                    SimpleNamespace(name="Case", path=str(existing)),
                    SimpleNamespace(name="case", path=str(existing)),
                ]
            )

        def __exit__(self, *_args):
            return None

    def ambiguous_scandir(path):
        if Path(path) == default:
            return _Entries()
        return real_scandir(path)

    monkeypatch.setattr(profile_storage.os, "scandir", ambiguous_scandir)

    with (
        pytest.raises(ProfileStorageValidationError, match="unsafe or ambiguous"),
        _prepared_publication(staging, PROFILE_ID, "chromium", profiles),
    ):
        pass


def test_stage_and_apply_fresh_profile_and_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    imported = tmp_path / "imported.db"
    _database(active, "old")
    _database(imported, "imported")
    staging = _staging(profiles)

    plan = stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    result = apply_pending_portability_activation(data)

    assert result is not None and result.applied
    assert result.database_activated
    assert result.backup_path is not None and _marker(result.backup_path) == "old"
    assert _marker(active) == "imported"
    assert (profiles / PROFILE_ID / "Default" / "Cookies").read_bytes() == b"session"
    assert not staging.exists()
    assert not plan.path.exists()


def test_post_apply_release_failure_preserves_committed_result_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    imported = tmp_path / "imported.db"
    _database(active, "old")
    _database(imported, "imported")
    staging = _staging(profiles)
    plan = stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    registry = BrowserProfileLeaseRegistry()
    original_release = portability_activation.MaintenanceProfileScope.release

    def release_then_report_failure(
        scope: portability_activation.MaintenanceProfileScope,
    ) -> bool:
        raise BrowserProfileLeaseCleanupError(scope)

    monkeypatch.setattr(
        portability_activation.MaintenanceProfileScope,
        "release",
        release_then_report_failure,
    )
    result = apply_pending_portability_activation(data, lease_registry=registry)

    assert result is not None and result.applied
    assert result.database_activated
    assert "maintenance ownership cleanup failed" in str(result.error)
    assert result.maintenance_cleanup_scope is not None
    result.maintenance_cleanup_scope.ensure_live()
    assert _marker(active) == "imported"
    assert (profiles / PROFILE_ID).is_dir()
    assert not plan.path.exists()
    assert registry.snapshot(PROFILE_ID) is not None
    assert original_release(result.maintenance_cleanup_scope)
    assert registry.snapshot(PROFILE_ID) is None


def test_kept_local_profile_is_explicitly_validated_and_preserved(tmp_path: Path) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    local = profiles / PROFILE_ID
    _profile(local)
    (local / "local-marker").write_text("keep", encoding="utf-8")

    stage_portability_activation(
        data,
        imported,
        (),
        (KeptLocalProfile(PROFILE_ID, "chromium"),),
    )
    result = apply_pending_portability_activation(data)

    assert result is not None and result.applied
    assert result.database_activated
    assert (local / "local-marker").read_text(encoding="utf-8") == "keep"


def test_kept_local_nested_hardlink_blocks_startup_activation(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    local = profiles / PROFILE_ID
    _profile(local)
    stage_portability_activation(
        data,
        imported,
        (),
        (KeptLocalProfile(PROFILE_ID, "chromium"),),
    )
    original = local / "Default" / "nested-token"
    original.write_bytes(b"credential")
    os.link(original, local / "Default" / "linked-token")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert "hard-linked" in str(result.error)
    assert not result.database_activated
    assert _marker(active) == "old"
    assert original.read_bytes() == b"credential"
    assert (local / "Default" / "linked-token").is_file()


def test_reviewed_identical_keep_fingerprint_must_still_match_at_restart(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    local = profiles / PROFILE_ID
    _profile(local)
    fingerprint = profile_tree_lstat_fingerprint(local)
    stage_portability_activation(
        data,
        imported,
        (),
        (KeptLocalProfile(PROFILE_ID, "chromium", fingerprint),),
    )
    (local / "Default" / "Cookies").write_bytes(b"changed-after-review")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert "changed after collision review" in str(result.error)
    assert not result.database_activated
    assert _marker(active) == "old"
    assert local.is_dir()


def test_canonical_custom_profiles_root_can_be_configured_through_link(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    external_profiles = tmp_path / "external-profiles"
    external_profiles.mkdir()
    linked_profiles = data / "profiles-link"
    try:
        os.symlink(external_profiles, linked_profiles, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links are unavailable on this Windows configuration.")
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(external_profiles)

    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
        profiles_root=linked_profiles,
    )
    result = apply_pending_portability_activation(
        data,
        profiles_root=linked_profiles,
    )

    assert result is not None and result.applied
    assert (external_profiles / PROFILE_ID).is_dir()


def test_stage_requires_explicit_decision_for_existing_local_uuid(tmp_path: Path) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    _profile(profiles / PROFILE_ID)

    with pytest.raises(PortabilityActivationError, match="explicit kept-local"):
        stage_portability_activation(data, imported, ())

    assert not (data / ACTIVATION_PLAN_NAME).exists()
    assert not (data / PRIVATE_DATABASE_NAME).exists()


def test_reviewed_absent_omitted_profile_must_remain_absent_at_restart(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    stage_portability_activation(data, imported, ())
    appeared = profiles / PROFILE_ID
    _profile(appeared)
    (appeared / "owner").write_text("unreviewed credentials", encoding="utf-8")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert "appeared after absence review" in str(result.error)
    assert not result.database_activated
    assert _marker(active) == "old"
    assert (appeared / "owner").read_text(encoding="utf-8") == "unreviewed credentials"


def test_resume_after_profile_publish_before_journal_update(tmp_path: Path) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    _database(data / "automadin.db", "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    with _prepared_publication(
        staging, PROFILE_ID, "chromium", profiles
    ) as publication:
        publication.publish()

    result = apply_pending_portability_activation(data)

    assert result is not None and result.applied
    assert (profiles / PROFILE_ID).is_dir()


def test_resume_detects_database_swap_before_journal_update(tmp_path: Path) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    plan = stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    with _prepared_publication(
        staging, PROFILE_ID, "chromium", profiles
    ) as publication:
        publication.publish()
    os.rename(plan.private_database_path, data / PENDING_IMPORT_NAME)
    assert apply_portability_owned_database_import(active).applied

    result = apply_pending_portability_activation(data)

    assert result is not None and result.applied
    assert _marker(active) == "imported"
    assert not (data / ACTIVATION_PLAN_NAME).exists()


def test_database_failure_rolls_new_profile_back_to_recovery_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    monkeypatch.setattr(
        portability_activation,
        "apply_pending_database_import",
        lambda _path: DatabaseImportResult(applied=False, error="simulated failure"),
    )

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert result.error == "simulated failure"
    assert staging in result.recovery_paths
    assert staging.is_dir()
    assert not (profiles / PROFILE_ID).exists()
    assert result.failed_plan_path is not None
    journal = json.loads(result.failed_plan_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "failed"
    assert not (data / ACTIVATION_PLAN_NAME).exists()


def test_new_local_collision_after_plan_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    _database(data / "automadin.db", "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    collision = profiles / PROFILE_ID
    _profile(collision)
    (collision / "owner").write_text("local", encoding="utf-8")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert "both or neither" in str(result.error)
    assert (collision / "owner").read_text(encoding="utf-8") == "local"
    assert staging.is_dir()


def test_stage_rejects_engine_mismatch_and_existing_pending_database(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    imported = tmp_path / "imported.db"
    _database(imported, "imported", engine="firefox")
    staging = _staging(profiles)

    with pytest.raises(PortabilityActivationError, match="database row and engine"):
        stage_portability_activation(
            data,
            imported,
            (StagedProfileInput(PROFILE_ID, "chromium", staging),),
        )
    assert not (data / ACTIVATION_PLAN_NAME).exists()

    (data / PENDING_IMPORT_NAME).write_bytes(b"owned")
    with pytest.raises(FileExistsError, match="database import is already pending"):
        stage_portability_activation(data, imported, ())


def test_orphan_private_database_is_quarantined_once(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    orphan = data / PRIVATE_DATABASE_NAME
    _database(orphan, "orphan")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert result.rejected_path is not None and result.rejected_path.is_file()
    assert _marker(result.rejected_path) == "orphan"
    assert not orphan.exists()
    assert apply_pending_portability_activation(data) is None


def test_changed_staging_is_compensated_and_plan_quarantined_without_db_swap(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    (staging / "Default" / "Cookies").write_bytes(b"changed")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert result.failed_plan_path is not None and result.failed_plan_path.is_file()
    assert _marker(active) == "old"
    assert staging.is_dir()
    assert not (profiles / PROFILE_ID).exists()
    assert not (data / ACTIVATION_PLAN_NAME).exists()


def test_duplicate_key_journal_is_quarantined_instead_of_blocking_startup(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    journal = data / ACTIVATION_PLAN_NAME
    journal.write_text(
        '{"format":"automadin.portability-activation","format":"duplicate"}',
        encoding="utf-8",
    )

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert "repeats field" in str(result.error)
    assert result.failed_plan_path is not None and result.failed_plan_path.is_file()
    assert not journal.exists()


def test_unrelated_pending_database_preserved_and_plan_database_quarantined(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    unrelated = data / PENDING_IMPORT_NAME
    _database(unrelated, "unrelated")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert not result.database_activated
    assert not unrelated.exists()
    assert result.rejected_path is not None and result.rejected_path.is_file()
    recovered_markers = {
        _marker(path)
        for path in result.recovery_paths
        if path.suffix == ".db" and path.is_file()
    }
    assert {"imported", "unrelated"} <= recovered_markers
    assert staging.is_dir()
    assert not (profiles / PROFILE_ID).exists()
    assert not (data / PRIVATE_DATABASE_NAME).exists()
    assert apply_pending_portability_activation(data) is None
    assert apply_pending_database_import(active) is None


def test_pre_swap_database_failure_quarantines_plan_pending_and_rolls_profiles_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    original_backup = database_transfer_module._create_safety_backup

    def fail_active_safety_backup(source: Path) -> Path:
        if Path(source) == active:
            raise OSError("simulated pre-swap safety-backup failure")
        return original_backup(source)

    monkeypatch.setattr(
        database_transfer_module,
        "_create_safety_backup",
        fail_active_safety_backup,
    )

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert not result.database_activated
    assert "safety-backup failure" in str(result.error)
    assert _marker(active) == "old"
    assert staging.is_dir()
    assert not (profiles / PROFILE_ID).exists()
    assert not (data / PENDING_IMPORT_NAME).exists()
    assert result.rejected_path is not None and result.rejected_path.is_file()
    assert result.rejected_path in result.recovery_paths
    assert apply_pending_portability_activation(data) is None
    assert apply_pending_database_import(active) is None


def test_exception_after_private_database_move_quarantines_owned_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    monkeypatch.setattr(
        portability_activation,
        "apply_pending_database_import",
        lambda _path: (_ for _ in ()).throw(RuntimeError("crash before apply")),
    )

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert not result.database_activated
    assert "crash before apply" in str(result.error)
    assert result.rejected_path is not None and result.rejected_path.is_file()
    assert not (data / PENDING_IMPORT_NAME).exists()
    assert staging.is_dir()
    assert not (profiles / PROFILE_ID).exists()
    assert _marker(active) == "old"


def test_portability_plan_blocks_later_database_only_staging(tmp_path: Path) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    later = tmp_path / "later.db"
    _database(imported, "imported")
    _database(later, "later")
    staging = _staging(profiles)
    plan = stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )
    private_hash = plan.private_database_path.read_bytes()
    journal_hash = plan.path.read_bytes()

    with pytest.raises(FileExistsError, match="portability-bundle activation"):
        stage_database_import(later, active)

    assert plan.private_database_path.read_bytes() == private_hash
    assert plan.path.read_bytes() == journal_hash
    assert not (data / PENDING_IMPORT_NAME).exists()


def test_malformed_journal_with_valid_pending_is_quarantined_once(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    active = data / "automadin.db"
    pending = data / PENDING_IMPORT_NAME
    _database(active, "old")
    _database(pending, "ambiguous")
    journal = data / ACTIVATION_PLAN_NAME
    journal.write_text(
        '{"format":"automadin.portability-activation","format":"duplicate"}',
        encoding="utf-8",
    )

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert not result.database_activated
    assert not pending.exists()
    recovered_databases = [
        path
        for path in result.recovery_paths
        if path.suffix == ".db" and path.is_file()
    ]
    assert len(recovered_databases) == 1
    assert _marker(recovered_databases[0]) == "ambiguous"
    assert _marker(active) == "old"
    assert apply_pending_portability_activation(data) is None
    assert apply_pending_database_import(active) is None


def test_post_swap_failure_reports_imported_database_active_and_keeps_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    profiles = data / "profiles"
    data.mkdir()
    active = data / "automadin.db"
    _database(active, "old")
    imported = tmp_path / "imported.db"
    _database(imported, "imported")
    staging = _staging(profiles)
    stage_portability_activation(
        data,
        imported,
        (StagedProfileInput(PROFILE_ID, "chromium", staging),),
    )

    def swap_then_report_failure(active_path: Path) -> DatabaseImportResult:
        os.replace(data / PENDING_IMPORT_NAME, active_path)
        return DatabaseImportResult(
            applied=False,
            error="simulated restore failure",
            active_database_matches_import=True,
        )

    monkeypatch.setattr(
        portability_activation,
        "apply_pending_database_import",
        swap_then_report_failure,
    )

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert result.database_activated
    assert _marker(active) == "imported"
    assert (profiles / PROFILE_ID).is_dir()
    assert not staging.exists()


@pytest.mark.parametrize(
    "base_name",
    [
        ".automadin-portability-staging-0123456789abcdef0123456789abcdef.db",
        ".automadin-portability-pending.db.0123456789abcdef0123456789abcdef.tmp",
    ],
)
def test_exact_database_staging_remnants_and_sidecars_are_recovered_once(
    base_name: str,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    paths = [
        data / base_name,
        data / f"{base_name}-wal",
        data / f"{base_name}-shm",
    ]
    for index, path in enumerate(paths):
        path.write_bytes(f"remnant-{index}".encode())

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert len(result.recovery_paths) == 3
    assert {path.read_bytes() for path in result.recovery_paths} == {
        b"remnant-0",
        b"remnant-1",
        b"remnant-2",
    }
    assert not any(path.exists() for path in paths)
    assert apply_pending_portability_activation(data) is None


def test_unsafe_and_malformed_staging_lookalikes_are_reported_once_untouched(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    malformed = data / ".automadin-portability-staging-not-a-generated-id.db"
    malformed.write_bytes(b"lookalike")
    exact = (
        data
        / ".automadin-portability-staging-abcdefabcdefabcdefabcdefabcdefab.db"
    )
    external = tmp_path / "external.db"
    external.write_bytes(b"outside")
    try:
        os.symlink(external, exact)
    except OSError:
        os.link(external, exact)

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert set(result.unresolved_paths) == {malformed, exact}
    assert malformed.read_bytes() == b"lookalike"
    assert external.read_bytes() == b"outside"
    assert os.path.lexists(exact)
    assert apply_pending_portability_activation(data) is None


@pytest.mark.parametrize(
    "artifact_name",
    [ACTIVATION_PLAN_NAME, PRIVATE_DATABASE_NAME],
)
def test_fixed_pending_directory_is_surfaced_and_left_untouched(
    artifact_name: str,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    artifact = data / artifact_name
    artifact.mkdir()

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert result.unresolved_paths == (artifact,)
    assert artifact.is_dir()


@pytest.mark.parametrize(
    "artifact_name",
    [ACTIVATION_PLAN_NAME, PRIVATE_DATABASE_NAME],
)
def test_fixed_pending_broken_symlink_is_surfaced_and_left_untouched(
    artifact_name: str,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    artifact = data / artifact_name
    missing = tmp_path / "missing-target"
    try:
        os.symlink(missing, artifact)
    except OSError:
        pytest.skip("File links are unavailable on this Windows configuration.")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert result.unresolved_paths == (artifact,)
    assert os.path.lexists(artifact)
    assert not missing.exists()


@pytest.mark.parametrize("marker_kind", ["directory", "oversized", "broken_symlink"])
def test_unsafe_remnant_notice_marker_is_bounded_and_surfaced(
    marker_kind: str,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    remnant = data / ".automadin-portability-staging-lookalike.db"
    remnant.write_bytes(b"keep")
    marker = data / portability_activation.STAGING_REMNANT_REPORTED_NAME
    if marker_kind == "directory":
        marker.mkdir()
    elif marker_kind == "oversized":
        marker.write_bytes(b"x" * 5000)
    else:
        try:
            os.symlink(tmp_path / "missing-marker-target", marker)
        except OSError:
            pytest.skip("File links are unavailable on this Windows configuration.")

    result = apply_pending_portability_activation(data)

    assert result is not None and not result.applied
    assert marker in result.unresolved_paths
    assert remnant in result.unresolved_paths
    assert os.path.lexists(marker)
    assert remnant.read_bytes() == b"keep"
