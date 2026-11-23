from __future__ import annotations

import uuid

import pytest

from automadin.models.managed_emulator import (
    EmulatorCleanupOverride,
    EmulatorCleanupPolicy,
    EmulatorProcessCommand,
    ManagedEmulatorProfile,
    ManagedEmulatorTargetMode,
)
from automadin.models.run_config import SavedRunConfig
from automadin.services.database import DatabaseService


def _generic_profile(**overrides: object) -> ManagedEmulatorProfile:
    values: dict[str, object] = {
        "profile_id": str(uuid.uuid4()),
        "name": "Local emulator",
        "launch_command": EmulatorProcessCommand(
            r"C:\Emulator\player.exe",
            ("--instance", "main"),
        ),
        "target_mode": ManagedEmulatorTargetMode.EXACT_ADB_ENDPOINT,
        "adb_endpoint": "127.0.0.1:5555",
        "default_cleanup_policy": EmulatorCleanupPolicy.LEAVE_RUNNING,
    }
    values.update(overrides)
    return ManagedEmulatorProfile(**values)  # type: ignore[arg-type]


def test_managed_emulator_profile_round_trips_exact_target_and_commands() -> None:
    profile = _generic_profile(
        shutdown_command=EmulatorProcessCommand(
            r"C:\Emulator\manager.exe",
            ("stop", "main"),
        ),
        default_cleanup_policy=EmulatorCleanupPolicy.ALWAYS_CLOSE,
        force_terminate_owned_process=True,
    )

    restored = ManagedEmulatorProfile.from_dict(profile.to_dict())

    assert restored == profile
    assert restored.target_key == "adb:127.0.0.1:5555"


def test_provider_profile_has_stable_target_key_and_ignores_stale_endpoint() -> None:
    profile = ManagedEmulatorProfile(
        profile_id=str(uuid.uuid4()),
        name="MuMu main",
        launch_command=EmulatorProcessCommand(r"C:\MuMu\MuMuManager.exe"),
        target_mode=ManagedEmulatorTargetMode.PROVIDER_INSTANCE,
        provider_id="MuMu",
        provider_instance_id="0",
        adb_endpoint="127.0.0.1:16384",
        default_cleanup_policy=EmulatorCleanupPolicy.LEAVE_RUNNING,
    )

    assert profile.target_key == "provider:mumu:0"
    assert profile.adb_endpoint == ""


def test_always_close_requires_safe_generic_shutdown() -> None:
    with pytest.raises(ValueError, match="Always close requires"):
        _generic_profile(default_cleanup_policy=EmulatorCleanupPolicy.ALWAYS_CLOSE)


def test_close_if_started_requires_a_safe_cleanup_path() -> None:
    with pytest.raises(ValueError, match="Close if started requires"):
        _generic_profile(
            default_cleanup_policy=EmulatorCleanupPolicy.CLOSE_IF_STARTED
        )


def test_cleanup_override_resolves_against_profile_default() -> None:
    profile = _generic_profile(
        default_cleanup_policy=EmulatorCleanupPolicy.LEAVE_RUNNING
    )

    assert (
        profile.effective_cleanup_policy(EmulatorCleanupOverride.INHERIT)
        is EmulatorCleanupPolicy.LEAVE_RUNNING
    )
    assert (
        profile.effective_cleanup_policy(EmulatorCleanupOverride.CLOSE_IF_STARTED)
        is EmulatorCleanupPolicy.CLOSE_IF_STARTED
    )


def test_saved_run_config_round_trips_managed_profile_reference() -> None:
    profile_id = str(uuid.uuid4())
    config = SavedRunConfig(
        name="Managed run",
        flow_name="flow",
        device_id="",
        managed_emulator_profile_id=profile_id,
        emulator_cleanup_override=EmulatorCleanupOverride.LEAVE_RUNNING,
    )

    restored = SavedRunConfig.from_dict(config.to_dict())

    assert restored.managed_emulator_profile_id == profile_id
    assert (
        restored.emulator_cleanup_override
        is EmulatorCleanupOverride.LEAVE_RUNNING
    )


def test_database_preserves_profile_identity_across_rename(tmp_path) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"))
    profile = _generic_profile()
    try:
        database.save_managed_emulator_profile(profile)
        renamed = ManagedEmulatorProfile.from_dict(
            {**profile.to_dict(), "name": "Renamed emulator"}
        )
        database.save_managed_emulator_profile(renamed)

        assert database.get_managed_emulator_profile(profile.profile_id) == renamed
        assert database.get_managed_emulator_profile_by_name("RENAMED EMULATOR") == renamed
        assert database.list_managed_emulator_profiles() == [renamed]
    finally:
        database.close()


def test_database_rejects_duplicate_managed_target(tmp_path) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"))
    first = _generic_profile(name="First")
    second = _generic_profile(name="Second")
    try:
        database.save_managed_emulator_profile(first)

        with pytest.raises(ValueError, match="already managed by profile 'First'"):
            database.save_managed_emulator_profile(second)
    finally:
        database.close()


def test_referenced_managed_profile_cannot_be_deleted(tmp_path) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"))
    profile = _generic_profile()
    run = SavedRunConfig(
        name="Managed run",
        flow_name="flow",
        device_id="",
        managed_emulator_profile_id=profile.profile_id,
    )
    try:
        database.save_managed_emulator_profile(profile)
        database.save_run_config(run.name, run)

        assert database.list_run_configs_referencing_managed_emulator(
            profile.profile_id
        ) == [run]
        assert not database.delete_managed_emulator_profile(profile.profile_id)
        assert database.get_managed_emulator_profile(profile.profile_id) == profile
    finally:
        database.close()
