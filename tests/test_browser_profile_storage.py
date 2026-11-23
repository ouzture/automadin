from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    BrowserProfileLeaseStateError,
    ProfileUse,
)

from automadin.services.browser_profile_manifest import (
    PROFILE_MANIFEST_FILENAME,
    BrowserProfileManifest,
    read_profile_manifest,
    _write_profile_manifest as write_profile_manifest,
)
from automadin.services.browser_profile_storage import (
    BlockedPendingProfileDelete,
    ManagedProfileStorage,
    ProfileStorageConflictError,
    PendingProfileDelete,
    PendingProfileDeleteBlockReason,
    ProfileStagingOperation,
    ProfileStagingRemnant,
    ProfileStorageInventoryError,
    ProfileStorageRecoveryError,
    ProfileStorageStatus,
    ProfileStorageValidationError,
    adopt_legacy_profile,
    begin_profile_delete,
    create_clean_profile,
    delete_verified_profile,
    discard_profile_staging_remnant,
    import_browser_profile,
    inspect_pending_profile_deletes,
    inspect_profile_storage,
    list_managed_profile_storage,
    list_pending_profile_deletes,
    list_profile_staging_remnants,
    recover_pending_profile_delete,
)

_CREATED_AT = "2026-07-18T12:30:00Z"


def _profile_id() -> str:
    return str(uuid.uuid4())


def _maintenance(profile_id: str):
    return BrowserProfileLeaseRegistry().acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", "storage test", str(uuid.uuid4())),
    )


_raw_create_clean_profile = create_clean_profile
_raw_import_browser_profile = import_browser_profile
_raw_adopt_legacy_profile = adopt_legacy_profile
_raw_begin_profile_delete = begin_profile_delete
_raw_delete_verified_profile = delete_verified_profile
_raw_recover_pending_profile_delete = recover_pending_profile_delete
_raw_discard_profile_staging_remnant = discard_profile_staging_remnant


def create_clean_profile(profile_id, *args, **kwargs):
    lease = kwargs.setdefault("maintenance_lease", _maintenance(profile_id))
    kwargs.setdefault("lease_registry", lease._registry)
    return _raw_create_clean_profile(profile_id, *args, **kwargs)


def import_browser_profile(source, profile_id, *args, **kwargs):
    lease = kwargs.setdefault("maintenance_lease", _maintenance(profile_id))
    kwargs.setdefault("lease_registry", lease._registry)
    return _raw_import_browser_profile(source, profile_id, *args, **kwargs)


def adopt_legacy_profile(legacy_name, profile_id, *args, **kwargs):
    lease = kwargs.setdefault("maintenance_lease", _maintenance(profile_id))
    kwargs.setdefault("lease_registry", lease._registry)
    return _raw_adopt_legacy_profile(legacy_name, profile_id, *args, **kwargs)


def begin_profile_delete(profile_id, *args, **kwargs):
    lease = kwargs.setdefault("maintenance_lease", _maintenance(profile_id))
    kwargs.setdefault("lease_registry", lease._registry)
    return _raw_begin_profile_delete(profile_id, *args, **kwargs)


def delete_verified_profile(profile_id, *args, **kwargs):
    lease = kwargs.setdefault("maintenance_lease", _maintenance(profile_id))
    kwargs.setdefault("lease_registry", lease._registry)
    return _raw_delete_verified_profile(profile_id, *args, **kwargs)


def recover_pending_profile_delete(profile_id, *args, **kwargs):
    lease = kwargs.setdefault("maintenance_lease", _maintenance(profile_id))
    kwargs.setdefault("lease_registry", lease._registry)
    return _raw_recover_pending_profile_delete(profile_id, *args, **kwargs)


def discard_profile_staging_remnant(remnant, profiles_root=None, **kwargs):
    lease = kwargs.setdefault("maintenance_lease", _maintenance(remnant.profile_id))
    kwargs.setdefault("lease_registry", lease._registry)
    return _raw_discard_profile_staging_remnant(
        remnant,
        profiles_root=profiles_root,
        **kwargs,
    )


def _make_chromium_profile(path: Path, marker: str = "profile") -> Path:
    (path / "Default").mkdir(parents=True)
    (path / "Default" / "Preferences").write_text(marker, encoding="utf-8")
    return path


def _make_chromium_source(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "Preferences").write_text("source-preferences", encoding="utf-8")
    (path / "Cache").mkdir()
    (path / "Cache" / "discard.txt").write_text("noise", encoding="utf-8")
    (path / "keep.txt").write_text("keep", encoding="utf-8")
    (path.parent / "Local State").write_text("local-state", encoding="utf-8")
    return path


def test_create_rejects_maintenance_lease_from_another_registry_without_mutation(
    tmp_path: Path,
) -> None:
    profile_id = _profile_id()
    profiles = tmp_path / "profiles"
    owner_registry = BrowserProfileLeaseRegistry()
    owner = owner_registry.acquire_owner(
        profile_id,
        ProfileUse("run", "owner", "owner-token"),
    )
    maintenance_registry = BrowserProfileLeaseRegistry()
    maintenance = maintenance_registry.acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", "wrong registry", "maintenance-token"),
    )

    with pytest.raises(BrowserProfileLeaseStateError, match="different registry"):
        _raw_create_clean_profile(
            profile_id,
            "chromium",
            profiles_root=profiles,
            lease_registry=owner_registry,
            maintenance_lease=maintenance,
        )

    assert not profiles.exists()
    maintenance.release()
    owner.release()


def test_delayed_storage_receipt_rejects_registry_mismatch_before_rollback(
    tmp_path: Path,
) -> None:
    profile_id = _profile_id()
    profiles = tmp_path / "profiles"
    lease_registry = BrowserProfileLeaseRegistry()
    maintenance = lease_registry.acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", "create", "create-token"),
    )
    change = _raw_create_clean_profile(
        profile_id,
        "chromium",
        profiles_root=profiles,
        lease_registry=lease_registry,
        maintenance_lease=maintenance,
    )
    change._lease_registry = BrowserProfileLeaseRegistry()  # noqa: SLF001

    with pytest.raises(BrowserProfileLeaseStateError, match="different registry"):
        change.rollback()

    assert change.path.is_dir()
    change._lease_registry = lease_registry  # noqa: SLF001
    change.rollback()
    maintenance.release()


def test_clean_profile_uses_uuid_storage_exact_manifest_and_required_layout(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()

    change = create_clean_profile(
        profile_id,
        "chromium",
        maintenance_lease=_maintenance(profile_id),
        profiles_root=profiles,
        created_at=_CREATED_AT,
    )

    assert change.path == profiles.resolve() / profile_id
    assert (change.path / "Default").is_dir()
    assert read_profile_manifest(change.path) == BrowserProfileManifest.create(
        "chromium",
        profile_id=profile_id,
        created_at=_CREATED_AT,
    )
    stored = json.loads(
        (change.path / PROFILE_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert set(stored) == {
        "created_at",
        "engine",
        "format",
        "manifest_version",
        "profile_id",
    }
    assert "name" not in stored

    change.commit()
    assert change.committed
    assert change.path.is_dir()


def test_clean_firefox_profile_uses_root_layout_and_context_rolls_back(
    tmp_path: Path,
) -> None:
    profile_id = _profile_id()
    profiles = tmp_path / "profiles"

    with create_clean_profile(
        profile_id,
        "firefox",
        maintenance_lease=_maintenance(profile_id),
        profiles_root=profiles,
        created_at=_CREATED_AT,
    ) as change:
        assert change.path.is_dir()
        assert not (change.path / "Default").exists()

    assert change.rolled_back
    assert not change.path.exists()


def test_managed_profile_inventory_is_typed_strict_and_read_only(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    chromium_id = _profile_id()
    firefox_id = _profile_id()
    chromium = create_clean_profile(
        chromium_id,
        "chromium",
        maintenance_lease=_maintenance(chromium_id),
        profiles_root=profiles,
    )
    chromium.commit()
    firefox = create_clean_profile(
        firefox_id,
        "firefox",
        maintenance_lease=_maintenance(firefox_id),
        profiles_root=profiles,
    )
    firefox.commit()
    before = {
        path.relative_to(profiles): path.read_bytes()
        for path in profiles.rglob("*")
        if path.is_file()
    }

    entries = list_managed_profile_storage(profiles)

    assert entries == sorted(
        [
            ManagedProfileStorage(
                profile_id=chromium_id,
                engine="chromium",
                path=(profiles / chromium_id).resolve(),
            ),
            ManagedProfileStorage(
                profile_id=firefox_id,
                engine="firefox",
                path=(profiles / firefox_id).resolve(),
            ),
        ],
        key=lambda item: item.profile_id,
    )
    assert before == {
        path.relative_to(profiles): path.read_bytes()
        for path in profiles.rglob("*")
        if path.is_file()
    }


def test_managed_profile_inventory_ignores_invalid_uuid_storage(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()

    mismatch_id = _profile_id()
    mismatch = _make_chromium_profile(profiles / mismatch_id)
    write_profile_manifest(
        mismatch,
        BrowserProfileManifest.create("chromium", profile_id=_profile_id()),
    )

    unsupported_id = _profile_id()
    unsupported = _make_chromium_profile(profiles / unsupported_id)
    write_profile_manifest(
        unsupported,
        BrowserProfileManifest.create("webkit", profile_id=unsupported_id),
    )

    incompatible_id = _profile_id()
    incompatible = profiles / incompatible_id
    incompatible.mkdir()
    write_profile_manifest(
        incompatible,
        BrowserProfileManifest.create("chromium", profile_id=incompatible_id),
    )

    malformed_id = _profile_id()
    malformed = _make_chromium_profile(profiles / malformed_id)
    (malformed / PROFILE_MANIFEST_FILENAME).write_text("{", encoding="utf-8")

    uppercase_id = _profile_id()
    uppercase = _make_chromium_profile(profiles / uppercase_id.upper())
    write_profile_manifest(
        uppercase,
        BrowserProfileManifest.create("chromium", profile_id=uppercase_id),
    )

    file_id = _profile_id()
    occupied_file = profiles / file_id
    occupied_file.write_text("not a directory", encoding="utf-8")

    assert list_managed_profile_storage(profiles) == []
    for candidate in (
        mismatch,
        unsupported,
        incompatible,
        malformed,
        uppercase,
        occupied_file,
    ):
        assert candidate.exists()


def test_managed_profile_inventory_rejects_redirected_required_layout(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile_id = _profile_id()
    directory = profiles / profile_id
    directory.mkdir()
    outside_default = tmp_path / "outside-default"
    outside_default.mkdir()
    redirected_id = _profile_id()
    outside_profile = _make_chromium_profile(tmp_path / "outside-profile")
    write_profile_manifest(
        outside_profile,
        BrowserProfileManifest.create(
            "chromium",
            profile_id=redirected_id,
        ),
    )
    try:
        (directory / "Default").symlink_to(
            outside_default,
            target_is_directory=True,
        )
        (profiles / redirected_id).symlink_to(
            outside_profile,
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")
    write_profile_manifest(
        directory,
        BrowserProfileManifest.create("chromium", profile_id=profile_id),
    )

    assert list_managed_profile_storage(profiles) == []
    assert outside_default.is_dir()
    assert (outside_profile / "Default" / "Preferences").is_file()


def test_create_never_reuses_existing_uuid_or_same_name_legacy_folder(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    collision = profiles / profile_id
    collision.mkdir(parents=True)
    (collision / "sentinel.txt").write_text("existing", encoding="utf-8")
    legacy = _make_chromium_profile(profiles / "Visible account")

    with pytest.raises(ProfileStorageConflictError, match="already exists"):
        create_clean_profile(profile_id, "chromium", profiles_root=profiles)

    assert (collision / "sentinel.txt").read_text(encoding="utf-8") == "existing"
    assert legacy.is_dir()
    assert not (legacy / PROFILE_MANIFEST_FILENAME).exists()


def test_import_chrome_stages_layout_manifest_and_preserves_source(
    tmp_path: Path,
) -> None:
    source = _make_chromium_source(tmp_path / "User Data" / "Profile 2")
    profiles = tmp_path / "managed"
    profile_id = _profile_id()
    source_before = {
        path.relative_to(source.parent): path.read_bytes()
        for path in source.parent.rglob("*")
        if path.is_file()
    }

    change = import_browser_profile(
        source,
        profile_id,
        "chrome",
        profiles_root=profiles,
        created_at=_CREATED_AT,
    )

    assert (change.path / "Default" / "Preferences").read_text(
        encoding="utf-8"
    ) == "source-preferences"
    assert (change.path / "Default" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (change.path / "Default" / "Cache").exists()
    assert (change.path / "Local State").read_text(encoding="utf-8") == "local-state"
    assert read_profile_manifest(change.path).profile_id == profile_id
    assert read_profile_manifest(change.path).engine == "chrome"
    assert source_before == {
        path.relative_to(source.parent): path.read_bytes()
        for path in source.parent.rglob("*")
        if path.is_file()
    }

    change.rollback()
    assert not change.path.exists()
    assert source.is_dir()


def test_import_writes_manifest_before_atomic_publish(tmp_path: Path) -> None:
    source = _make_chromium_source(tmp_path / "User Data" / "Default")
    profiles = tmp_path / "managed"
    profile_id = _profile_id()

    from automadin.services import browser_profile_storage as storage

    real_rename = storage._rename_directory_no_replace
    observed: list[BrowserProfileManifest] = []

    def inspect_then_rename(staging: Path, destination: Path) -> None:
        observed.append(read_profile_manifest(staging))
        real_rename(staging, destination)

    with patch.object(storage, "_rename_directory_no_replace", inspect_then_rename):
        change = import_browser_profile(
            source,
            profile_id,
            "msedge",
            profiles_root=profiles,
            created_at=_CREATED_AT,
        )

    assert [(item.profile_id, item.engine) for item in observed] == [
        (profile_id, "msedge")
    ]
    change.rollback()


def test_import_failure_cleans_staging_without_touching_source(tmp_path: Path) -> None:
    source = _make_chromium_source(tmp_path / "User Data" / "Default")
    profiles = tmp_path / "managed"
    profile_id = _profile_id()

    with (
        patch(
            "automadin.services.browser_profile_storage._write_profile_manifest",
            side_effect=OSError("simulated manifest failure"),
        ),
        pytest.raises(OSError, match="simulated manifest failure"),
    ):
        import_browser_profile(
            source,
            profile_id,
            "chrome",
            profiles_root=profiles,
        )

    assert source.is_dir()
    assert (source / "Preferences").read_text(encoding="utf-8") == "source-preferences"
    assert not (profiles / profile_id).exists()
    assert list(profiles.glob(f".importing-{profile_id}-*")) == []


def test_import_rejects_source_that_contains_managed_root(tmp_path: Path) -> None:
    source = _make_chromium_source(tmp_path / "selected-browser-profile")
    profiles = source / "nested" / "managed-profiles"
    profile_id = _profile_id()

    with pytest.raises(ProfileStorageValidationError, match="may not contain"):
        import_browser_profile(
            source,
            profile_id,
            "chrome",
            profiles_root=profiles,
        )

    assert (source / "Preferences").read_text(encoding="utf-8") == "source-preferences"
    assert not (profiles / profile_id).exists()
    assert list(profiles.glob(f".importing-{profile_id}-*")) == []


def test_import_atomic_collision_does_not_overwrite_racing_destination(
    tmp_path: Path,
) -> None:
    source = _make_chromium_source(tmp_path / "User Data" / "Default")
    profiles = tmp_path / "managed"
    profile_id = _profile_id()

    from automadin.services import browser_profile_storage as storage

    real_rename = storage._rename_directory_no_replace

    def create_collision(staging: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "external.txt").write_text("do not replace", encoding="utf-8")
        real_rename(staging, destination)

    with (
        patch.object(storage, "_rename_directory_no_replace", create_collision),
        pytest.raises(ProfileStorageConflictError),
    ):
        import_browser_profile(
            source,
            profile_id,
            "chrome",
            profiles_root=profiles,
        )

    assert (profiles / profile_id / "external.txt").read_text(
        encoding="utf-8"
    ) == "do not replace"
    assert list(profiles.glob(f".importing-{profile_id}-*")) == []


def test_import_firefox_blocks_source_profile_json_instead_of_overwriting_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Firefox" / "abc.default-release"
    source.mkdir(parents=True)
    (source / "prefs.js").write_text("preferences", encoding="utf-8")
    (source / PROFILE_MANIFEST_FILENAME).write_text("browser-owned", encoding="utf-8")
    profiles = tmp_path / "managed"
    profile_id = _profile_id()

    with pytest.raises(ProfileStorageValidationError, match="contains profile.json"):
        import_browser_profile(
            source,
            profile_id,
            "firefox",
            profiles_root=profiles,
        )

    assert (source / PROFILE_MANIFEST_FILENAME).read_text(
        encoding="utf-8"
    ) == "browser-owned"
    assert list(profiles.glob(f".importing-{profile_id}-*")) == []


def test_staging_remnant_inventory_reports_manifest_and_layout_state(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    valid_id = _profile_id()
    missing_id = _profile_id()
    mismatch_id = _profile_id()
    incomplete_id = _profile_id()

    valid = _make_chromium_profile(
        profiles / f".creating-{valid_id}-{'1' * 32}"
    )
    write_profile_manifest(
        valid,
        BrowserProfileManifest.create("chromium", profile_id=valid_id),
    )
    missing = profiles / f".importing-{missing_id}-{'2' * 32}"
    missing.mkdir()
    mismatch = _make_chromium_profile(
        profiles / f".creating-{mismatch_id}-{'3' * 32}"
    )
    write_profile_manifest(
        mismatch,
        BrowserProfileManifest.create("chromium", profile_id=_profile_id()),
    )
    incomplete = profiles / f".importing-{incomplete_id}-{'4' * 32}"
    incomplete.mkdir()
    write_profile_manifest(
        incomplete,
        BrowserProfileManifest.create("chrome", profile_id=incomplete_id),
    )
    malformed_name = profiles / f".creating-{_profile_id()}-{'A' * 32}"
    malformed_name.mkdir()

    entries = list_profile_staging_remnants(profiles)
    by_id = {entry.profile_id: entry for entry in entries}

    assert set(by_id) == {valid_id, missing_id, mismatch_id, incomplete_id}
    assert by_id[valid_id].operation is ProfileStagingOperation.CREATING
    assert by_id[valid_id].path == valid.resolve()
    assert by_id[valid_id].has_matching_manifest
    assert by_id[valid_id].manifest_engine == "chromium"
    assert by_id[valid_id].layout_ready
    assert by_id[missing_id].operation is ProfileStagingOperation.IMPORTING
    assert not by_id[missing_id].has_matching_manifest
    assert by_id[missing_id].manifest_engine == ""
    assert not by_id[missing_id].layout_ready
    assert not by_id[mismatch_id].has_matching_manifest
    assert by_id[mismatch_id].manifest_engine == "chromium"
    assert not by_id[mismatch_id].layout_ready
    assert by_id[incomplete_id].has_matching_manifest
    assert by_id[incomplete_id].manifest_engine == "chrome"
    assert not by_id[incomplete_id].layout_ready
    for candidate in (valid, missing, mismatch, incomplete, malformed_name):
        assert candidate.is_dir()


def test_staging_remnant_inventory_ignores_redirected_exact_child(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile_id = _profile_id()
    outside = _make_chromium_profile(tmp_path / "outside-remnant")
    write_profile_manifest(
        outside,
        BrowserProfileManifest.create("chromium", profile_id=profile_id),
    )
    link = profiles / f".importing-{profile_id}-{'5' * 32}"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    assert list_profile_staging_remnants(profiles) == []
    assert (outside / "Default" / "Preferences").is_file()


def test_discard_staging_remnant_revalidates_and_never_touches_uuid_storage(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    uuid_storage = create_clean_profile(
        profile_id,
        "chromium",
        profiles_root=profiles,
    )
    uuid_storage.commit()
    remnant_id = _profile_id()
    remnant_path = _make_chromium_profile(
        profiles / f".importing-{remnant_id}-{'6' * 32}"
    )
    write_profile_manifest(
        remnant_path,
        BrowserProfileManifest.create("chromium", profile_id=remnant_id),
    )
    remnant = list_profile_staging_remnants(profiles)[0]

    discard_profile_staging_remnant(remnant, profiles)

    assert not remnant_path.exists()
    assert (profiles / profile_id / PROFILE_MANIFEST_FILENAME).is_file()
    assert list_managed_profile_storage(profiles) == [
        ManagedProfileStorage(
            profile_id=profile_id,
            engine="chromium",
            path=(profiles / profile_id).resolve(),
        )
    ]


def test_discard_staging_remnant_rejects_stale_mutated_and_forged_values(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile_id = _profile_id()
    remnant_path = _make_chromium_profile(
        profiles / f".creating-{profile_id}-{'7' * 32}"
    )
    write_profile_manifest(
        remnant_path,
        BrowserProfileManifest.create("chromium", profile_id=profile_id),
    )
    remnant = list_profile_staging_remnants(profiles)[0]
    (remnant_path / PROFILE_MANIFEST_FILENAME).write_text("{", encoding="utf-8")

    with pytest.raises(ProfileStorageValidationError, match="changed"):
        discard_profile_staging_remnant(remnant, profiles)

    forged = ProfileStagingRemnant(
        operation=remnant.operation,
        profile_id=remnant.profile_id,
        path=remnant.path,
        has_matching_manifest=False,
    )
    with pytest.raises(ProfileStorageValidationError, match="changed"):
        discard_profile_staging_remnant(forged, profiles)

    assert remnant_path.is_dir()


def test_adopt_is_explicit_and_rollback_restores_legacy_folder(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    legacy = _make_chromium_profile(profiles / "Jane - Work")
    profile_id = _profile_id()

    change = adopt_legacy_profile(
        "Jane - Work",
        profile_id,
        "chromium",
        profiles_root=profiles,
        created_at=_CREATED_AT,
    )

    assert not legacy.exists()
    assert change.path.name == profile_id
    assert read_profile_manifest(change.path).profile_id == profile_id

    change.rollback()
    assert legacy.is_dir()
    assert (legacy / "Default" / "Preferences").read_text(encoding="utf-8") == "profile"
    assert not (legacy / PROFILE_MANIFEST_FILENAME).exists()
    assert not change.path.exists()


def test_adopt_commit_keeps_uuid_storage_and_existing_matching_manifest(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    legacy = _make_chromium_profile(profiles / "Legacy")
    profile_id = _profile_id()
    original_manifest = BrowserProfileManifest.create(
        "chrome",
        profile_id=profile_id,
        created_at=_CREATED_AT,
    )
    write_profile_manifest(legacy, original_manifest)

    change = adopt_legacy_profile(
        "Legacy",
        profile_id,
        "chrome",
        profiles_root=profiles,
    )
    change.commit()

    assert not legacy.exists()
    assert read_profile_manifest(change.path) == original_manifest


@pytest.mark.parametrize(
    "manifest_payload",
    [
        "not json",
        json.dumps({"some": "browser-owned object"}),
    ],
)
def test_adopt_blocks_malformed_or_unrelated_profile_json(
    tmp_path: Path,
    manifest_payload: str,
) -> None:
    profiles = tmp_path / "profiles"
    legacy = _make_chromium_profile(profiles / "Legacy")
    (legacy / PROFILE_MANIFEST_FILENAME).write_text(manifest_payload, encoding="utf-8")
    profile_id = _profile_id()

    with pytest.raises(ProfileStorageValidationError, match="manifest"):
        adopt_legacy_profile(
            "Legacy",
            profile_id,
            "chromium",
            profiles_root=profiles,
        )

    assert legacy.is_dir()
    assert not (profiles / profile_id).exists()


def test_adopt_blocks_manifest_identity_mismatch(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    legacy = _make_chromium_profile(profiles / "Legacy")
    write_profile_manifest(
        legacy,
        BrowserProfileManifest.create("chromium", profile_id=_profile_id()),
    )
    selected_id = _profile_id()

    with pytest.raises(ProfileStorageValidationError, match="identity"):
        adopt_legacy_profile(
            "Legacy",
            selected_id,
            "chromium",
            profiles_root=profiles,
        )

    assert legacy.is_dir()
    assert not (profiles / selected_id).exists()


def test_adopt_manifest_write_failure_restores_original_without_new_manifest(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    legacy = _make_chromium_profile(profiles / "Legacy")
    profile_id = _profile_id()

    with (
        patch(
            "automadin.services.browser_profile_storage._write_profile_manifest",
            side_effect=OSError("simulated write failure"),
        ),
        pytest.raises(OSError, match="simulated write failure"),
    ):
        adopt_legacy_profile(
            "Legacy",
            profile_id,
            "chromium",
            profiles_root=profiles,
        )

    assert legacy.is_dir()
    assert not (legacy / PROFILE_MANIFEST_FILENAME).exists()
    assert not (profiles / profile_id).exists()


def test_delete_tombstone_rolls_back_when_database_step_fails(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    created = create_clean_profile(profile_id, "chromium", profiles_root=profiles)
    created.commit()

    deletion = begin_profile_delete(
        profile_id,
        engine="chromium",
        profiles_root=profiles,
    )
    assert not deletion.path.exists()
    assert deletion.working_path.is_dir()

    deletion.rollback()
    assert deletion.path.is_dir()
    assert not deletion.working_path.exists()
    assert read_profile_manifest(deletion.path).profile_id == profile_id


def test_interrupted_delete_can_be_inspected_restored_or_finished(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    created = create_clean_profile(profile_id, "chromium", profiles_root=profiles)
    created.commit()

    begin_profile_delete(profile_id, profiles_root=profiles)
    interrupted = inspect_profile_storage(
        profile_id,
        "chromium",
        profiles_root=profiles,
    )
    assert interrupted.status is ProfileStorageStatus.NEEDS_SETUP
    assert interrupted.recovery_path is not None
    assert interrupted.recovery_path.is_dir()

    recovery = recover_pending_profile_delete(
        profile_id,
        engine="chromium",
        profiles_root=profiles,
    )
    recovery.rollback()
    assert (profiles / profile_id).is_dir()

    begin_profile_delete(profile_id, profiles_root=profiles)
    recovery = recover_pending_profile_delete(profile_id, profiles_root=profiles)
    recovery.commit()
    assert not (profiles / profile_id).exists()
    assert list(profiles.glob(f".deleting-{profile_id}-*")) == []


def test_pending_delete_discovery_is_typed_verified_and_read_only(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    created = create_clean_profile(profile_id, "chromium", profiles_root=profiles)
    created.commit()
    begin_profile_delete(profile_id, profiles_root=profiles)
    tombstone = next(profiles.glob(f".deleting-{profile_id}-*"))
    before = {
        path.relative_to(profiles): path.read_bytes()
        for path in profiles.rglob("*")
        if path.is_file()
    }

    pending = list_pending_profile_deletes(profiles)
    inventory = inspect_pending_profile_deletes(profiles)

    assert pending == [
        PendingProfileDelete(
            profile_id=profile_id,
            engine="chromium",
            tombstone_path=tombstone.resolve(),
            intended_path=profiles.resolve() / profile_id,
        )
    ]
    assert inventory.pending == tuple(pending)
    assert inventory.blocked == ()
    assert before == {
        path.relative_to(profiles): path.read_bytes()
        for path in profiles.rglob("*")
        if path.is_file()
    }
    assert tombstone.is_dir()


def _manual_tombstone(
    profiles: Path,
    directory_profile_id: str,
    manifest_profile_id: str,
    *,
    nonce: str,
    engine: str = "chromium",
    ready_layout: bool = True,
) -> Path:
    tombstone = profiles / f".deleting-{directory_profile_id}-{nonce}"
    tombstone.mkdir(parents=True)
    if ready_layout and engine in {"chromium", "chrome", "msedge"}:
        (tombstone / "Default").mkdir()
    write_profile_manifest(
        tombstone,
        BrowserProfileManifest.create(engine, profile_id=manifest_profile_id),
    )
    return tombstone


def test_pending_delete_inventory_blocks_duplicate_and_invalid_exact_entries(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    duplicate_id = _profile_id()
    mismatch_id = _profile_id()
    incompatible_id = _profile_id()
    invalid_manifest_id = _profile_id()
    unsupported_id = _profile_id()
    malformed_id = _profile_id()
    malformed = _manual_tombstone(
        profiles,
        malformed_id,
        malformed_id,
        nonce="A" * 32,
    )
    duplicate_one = _manual_tombstone(
        profiles,
        duplicate_id,
        duplicate_id,
        nonce="1" * 32,
    )
    duplicate_two = _manual_tombstone(
        profiles,
        duplicate_id,
        duplicate_id,
        nonce="2" * 32,
    )
    mismatch = _manual_tombstone(
        profiles,
        mismatch_id,
        _profile_id(),
        nonce="3" * 32,
    )
    incompatible = _manual_tombstone(
        profiles,
        incompatible_id,
        incompatible_id,
        nonce="4" * 32,
        ready_layout=False,
    )
    invalid_manifest = _manual_tombstone(
        profiles,
        invalid_manifest_id,
        invalid_manifest_id,
        nonce="6" * 32,
    )
    (invalid_manifest / PROFILE_MANIFEST_FILENAME).write_text(
        "{",
        encoding="utf-8",
    )
    unsupported = _manual_tombstone(
        profiles,
        unsupported_id,
        unsupported_id,
        nonce="7" * 32,
        engine="webkit",
    )

    inventory = inspect_pending_profile_deletes(profiles)
    by_id = {item.profile_id: item for item in inventory.blocked}

    assert inventory.pending == ()
    assert set(by_id) == {
        duplicate_id,
        mismatch_id,
        incompatible_id,
        invalid_manifest_id,
        unsupported_id,
    }
    assert isinstance(by_id[duplicate_id], BlockedPendingProfileDelete)
    assert (
        by_id[duplicate_id].reason
        is PendingProfileDeleteBlockReason.MULTIPLE_CANDIDATES
    )
    assert by_id[duplicate_id].candidate_paths == tuple(
        sorted(
            (duplicate_one.resolve(), duplicate_two.resolve()),
            key=lambda path: path.name,
        )
    )
    assert (
        by_id[mismatch_id].reason
        is PendingProfileDeleteBlockReason.IDENTITY_MISMATCH
    )
    assert (
        by_id[incompatible_id].reason
        is PendingProfileDeleteBlockReason.INCOMPATIBLE_LAYOUT
    )
    assert (
        by_id[invalid_manifest_id].reason
        is PendingProfileDeleteBlockReason.INVALID_MANIFEST
    )
    assert (
        by_id[unsupported_id].reason
        is PendingProfileDeleteBlockReason.UNSUPPORTED_ENGINE
    )
    with pytest.raises(ProfileStorageInventoryError) as error:
        list_pending_profile_deletes(profiles)
    assert error.value.blocked == inventory.blocked

    duplicate_inspection = inspect_profile_storage(
        duplicate_id,
        "chromium",
        profiles_root=profiles,
    )
    assert duplicate_inspection.status is ProfileStorageStatus.CORRUPT
    assert "Multiple exact interrupted deletes" in duplicate_inspection.detail
    with pytest.raises(ProfileStorageRecoveryError, match="Multiple exact"):
        recover_pending_profile_delete(
            duplicate_id,
            profiles_root=profiles,
        )

    for candidate in (
        malformed,
        duplicate_one,
        duplicate_two,
        mismatch,
        incompatible,
        invalid_manifest,
        unsupported,
    ):
        assert candidate.is_dir()
        assert (candidate / PROFILE_MANIFEST_FILENAME).is_file()


def test_pending_delete_inventory_blocks_redirected_direct_child(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile_id = _profile_id()
    outside = _make_chromium_profile(tmp_path / "outside")
    write_profile_manifest(
        outside,
        BrowserProfileManifest.create("chromium", profile_id=profile_id),
    )
    link = profiles / f".deleting-{profile_id}-{'5' * 32}"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    inventory = inspect_pending_profile_deletes(profiles)
    assert inventory.pending == ()
    assert len(inventory.blocked) == 1
    assert (
        inventory.blocked[0].reason
        is PendingProfileDeleteBlockReason.INVALID_PATH
    )
    assert inventory.blocked[0].candidate_paths == (link.absolute(),)
    with pytest.raises(ProfileStorageInventoryError):
        list_pending_profile_deletes(profiles)
    assert outside.is_dir()
    assert (outside / "Default" / "Preferences").is_file()


def test_recovery_rejects_noncanonical_tombstone_name(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    malformed = _manual_tombstone(
        profiles,
        profile_id,
        profile_id,
        nonce="ABCDEF0123456789ABCDEF0123456789",
    )

    with pytest.raises(ProfileStorageValidationError, match="No interrupted delete"):
        recover_pending_profile_delete(profile_id, profiles_root=profiles)

    assert malformed.is_dir()


def test_inventory_enumeration_failure_is_never_reported_as_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from automadin.services import browser_profile_storage as storage

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile_id = _profile_id()
    real_scandir = storage.os.scandir

    def deny_managed_root(path: str | Path):
        if Path(path) == profiles.resolve():
            raise PermissionError("inventory denied")
        return real_scandir(path)

    monkeypatch.setattr(storage.os, "scandir", deny_managed_root)

    for inventory_call in (
        lambda: list_managed_profile_storage(profiles),
        lambda: list_profile_staging_remnants(profiles),
        lambda: inspect_pending_profile_deletes(profiles),
        lambda: list_pending_profile_deletes(profiles),
        lambda: inspect_profile_storage(
            profile_id,
            profiles_root=profiles,
        ),
    ):
        with pytest.raises(
            ProfileStorageInventoryError,
            match="could not be enumerated",
        ) as error:
            inventory_call()
        assert isinstance(error.value.__cause__, PermissionError)

    monkeypatch.setattr(storage.os, "scandir", real_scandir)
    assert profiles.is_dir()
    assert list(profiles.iterdir()) == []


def test_verified_delete_removes_only_matching_uuid_and_preserves_legacy_name(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    created = create_clean_profile(profile_id, "firefox", profiles_root=profiles)
    created.commit()
    legacy = profiles / "Visible profile"
    legacy.mkdir()
    (legacy / "keep.txt").write_text("legacy", encoding="utf-8")

    deletion = delete_verified_profile(
        profile_id,
        engine="firefox",
        profiles_root=profiles,
    )

    assert deletion.committed
    assert not (profiles / profile_id).exists()
    assert (legacy / "keep.txt").read_text(encoding="utf-8") == "legacy"


def test_delete_refuses_mismatched_manifest_without_moving_or_deleting(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    selected_id = _profile_id()
    directory = _make_chromium_profile(profiles / selected_id)
    write_profile_manifest(
        directory,
        BrowserProfileManifest.create("chromium", profile_id=_profile_id()),
    )

    with pytest.raises(ProfileStorageValidationError, match="identity"):
        delete_verified_profile(selected_id, profiles_root=profiles)

    assert directory.is_dir()
    assert (directory / "Default" / "Preferences").exists()


def test_inspection_distinguishes_missing_legacy_ready_and_incompatible(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()

    missing = inspect_profile_storage(
        profile_id,
        "chromium",
        profiles_root=profiles,
    )
    assert missing.status is ProfileStorageStatus.MISSING

    legacy = _make_chromium_profile(profiles / "Legacy")
    candidate = inspect_profile_storage(
        profile_id,
        "chromium",
        profiles_root=profiles,
        legacy_storage_name="Legacy",
    )
    assert candidate.status is ProfileStorageStatus.NEEDS_SETUP
    assert candidate.legacy_candidate == legacy.resolve()
    assert not (legacy / PROFILE_MANIFEST_FILENAME).exists()

    change = adopt_legacy_profile(
        "Legacy",
        profile_id,
        "chromium",
        profiles_root=profiles,
    )
    change.commit()
    ready = inspect_profile_storage(profile_id, "chromium", profiles_root=profiles)
    assert ready.status is ProfileStorageStatus.READY
    assert ready.ready

    incompatible_id = _profile_id()
    incompatible = _make_chromium_profile(profiles / incompatible_id)
    # Remove the only file and directory so the Chromium layout is absent.
    preferences = incompatible / "Default" / "Preferences"
    if preferences.exists():
        preferences.unlink()
    if (incompatible / "Default").exists():
        (incompatible / "Default").rmdir()
    write_profile_manifest(
        incompatible,
        BrowserProfileManifest.create("chromium", profile_id=incompatible_id),
    )
    inspected = inspect_profile_storage(
        incompatible_id,
        "chromium",
        profiles_root=profiles,
    )
    assert inspected.status is ProfileStorageStatus.INCOMPATIBLE


def test_published_adoption_requires_explicit_database_binding_after_crash(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    change = create_clean_profile(profile_id, "chromium", profiles_root=profiles)
    change.commit()

    unbound = inspect_profile_storage(
        profile_id,
        "chromium",
        legacy_storage_name="Former display name",
        profiles_root=profiles,
    )

    assert unbound.status is ProfileStorageStatus.NEEDS_SETUP
    assert "Finish setup explicitly" in unbound.detail
    assert unbound.manifest is not None
    assert unbound.manifest.profile_id == profile_id
    assert inspect_profile_storage(
        profile_id,
        "chromium",
        profiles_root=profiles,
    ).status is ProfileStorageStatus.READY


def test_inspection_distinguishes_corrupt_and_mismatched_manifest(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    corrupt_id = _profile_id()
    corrupt = _make_chromium_profile(profiles / corrupt_id)
    (corrupt / PROFILE_MANIFEST_FILENAME).write_text("not json", encoding="utf-8")

    assert inspect_profile_storage(
        corrupt_id,
        "chromium",
        profiles_root=profiles,
    ).status is ProfileStorageStatus.CORRUPT

    mismatch_id = _profile_id()
    mismatch = _make_chromium_profile(profiles / mismatch_id)
    write_profile_manifest(
        mismatch,
        BrowserProfileManifest.create("firefox", profile_id=mismatch_id),
    )
    assert inspect_profile_storage(
        mismatch_id,
        "chromium",
        profiles_root=profiles,
    ).status is ProfileStorageStatus.MISMATCH


def test_published_storage_is_recoverable_after_process_or_db_interruption(
    tmp_path: Path,
) -> None:
    """A published exact manifest lets a later run bind without recopying."""
    source = _make_chromium_source(tmp_path / "User Data" / "Default")
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()
    change = import_browser_profile(
        source,
        profile_id,
        "chrome",
        profiles_root=profiles,
    )
    # Simulate the filesystem step finishing before the database bind.  The
    # receipt is lost across a crash; inspection must still identify exact data.
    change.commit()

    recovered = inspect_profile_storage(profile_id, "chrome", profiles_root=profiles)
    assert recovered.status is ProfileStorageStatus.READY
    assert recovered.manifest is not None
    assert recovered.manifest.profile_id == profile_id
    with pytest.raises(ProfileStorageConflictError):
        import_browser_profile(
            source,
            profile_id,
            "chrome",
            profiles_root=profiles,
        )


def test_symlinked_source_is_rejected_and_external_data_is_preserved(
    tmp_path: Path,
) -> None:
    source = _make_chromium_source(tmp_path / "outside" / "Default")
    source_link = tmp_path / "selected-profile"
    try:
        source_link.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")
    profiles = tmp_path / "profiles"
    profile_id = _profile_id()

    with pytest.raises(ProfileStorageValidationError, match="symlink"):
        import_browser_profile(
            source_link,
            profile_id,
            "chrome",
            profiles_root=profiles,
        )

    assert (source / "Preferences").read_text(encoding="utf-8") == "source-preferences"
    assert not (profiles / profile_id).exists()


def test_symlinked_uuid_storage_is_never_deleted(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile_id = _profile_id()
    outside = _make_chromium_profile(tmp_path / "outside")
    write_profile_manifest(
        outside,
        BrowserProfileManifest.create("chromium", profile_id=profile_id),
    )
    link = profiles / profile_id
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    with pytest.raises((ProfileStorageValidationError, ValueError)):
        delete_verified_profile(profile_id, profiles_root=profiles)

    assert outside.is_dir()
    assert (outside / "Default" / "Preferences").exists()


def test_receipt_cannot_reverse_a_committed_change(tmp_path: Path) -> None:
    change = create_clean_profile(
        _profile_id(),
        "firefox",
        profiles_root=tmp_path / "profiles",
    )
    change.commit()

    with pytest.raises(ProfileStorageRecoveryError, match="Cannot roll back committed"):
        change.rollback()
    assert change.path.is_dir()
