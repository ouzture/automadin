from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from automadin.models.device import (
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
)
from automadin.models.run_config import SavedRunConfig
from automadin.services.browser_profile_manifest import (
    BrowserProfileManifest,
    _write_profile_manifest as write_profile_manifest,
    write_managed_profile_manifest,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.services.browser_profile_metadata import BrowserProfileTarget
from automadin.services.browser_profile_paths import resolve_profile_storage_path
from automadin.services.database import DatabaseService


class BrowserProfileRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.profiles_root = self.root / "profiles"
        self._environment = patch.dict(
            os.environ,
            {
                "AUTOMADIN_DATA_DIR": str(self.root),
                "AUTOMADIN_PROFILES_DIR": str(self.profiles_root),
            },
        )
        self._environment.start()
        self.database = DatabaseService(str(self.root / "automadin.db"))
        self.lease_registry = BrowserProfileLeaseRegistry()

    def tearDown(self) -> None:
        self.database.close()
        self._environment.stop()
        self._temporary_directory.cleanup()

    def _ready_storage(self, profile_id: str, engine: str = "chromium") -> Path:
        manifest = BrowserProfileManifest.create(
            engine,
            profile_id=profile_id,
        )
        with self.lease_registry.acquire_maintenance(
            profile_id,
            ProfileUse("test", "Prepare repository profile", str(uuid.uuid4())),
        ) as lease:
            write_managed_profile_manifest(
                manifest,
                self.profiles_root,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        directory = resolve_profile_storage_path(profile_id, self.profiles_root)
        if engine in {"chromium", "chrome", "msedge"}:
            (directory / "Default").mkdir()
        return directory

    def test_crud_and_rename_keep_identity_and_storage_marker_stable(self) -> None:
        profile_id = str(uuid.uuid4())
        created = self.database.create_profile(
            "Team / Work",
            profile_id=profile_id,
            browser="Chrome",
            source_profile="Profile 2",
            account_name="Jane",
            engine="chromium",
        )

        self.assertEqual(created.profile_id, profile_id)
        self.assertEqual(created.name, "Team / Work")
        self.assertEqual(created.legacy_storage_name, "")
        self.assertEqual(
            self.database.get_profile_by_name("team / work"),
            created,
        )

        self.assertTrue(
            self.database.update_profile_metadata(
                profile_id,
                account_name="Jane Doe",
                account_email="jane@example.com",
            )
        )
        self.assertTrue(self.database.rename_profile(profile_id, "Personal / Main"))
        renamed = self.database.get_profile(profile_id)
        self.assertIsNotNone(renamed)
        assert renamed is not None
        self.assertEqual(renamed.name, "Personal / Main")
        self.assertEqual(renamed.profile_id, profile_id)
        self.assertEqual(renamed.legacy_storage_name, "")
        self.assertEqual(renamed.identity.account_name, "Jane Doe")
        self.assertEqual(renamed.identity.account_email, "jane@example.com")
        self.assertIsNone(self.database.get_profile_by_name("Team / Work"))
        self.assertEqual(
            self.database.profile_display_labels(),
            {profile_id: renamed.display_label},
        )
        self.assertEqual(
            self.database.get_profile_display_label(profile_id),
            renamed.display_label,
        )
        self.assertEqual(
            self.database.get_profile_display_label("personal / main"),
            renamed.display_label,
        )

        self.assertTrue(self.database.delete_profile_by_id(profile_id))
        self.assertIsNone(self.database.get_profile(profile_id))

    def test_create_and_rename_reject_casefold_name_collisions(self) -> None:
        first = self.database.create_profile("Work")
        second = self.database.create_profile("Personal")

        with self.assertRaisesRegex(ValueError, "already exists"):
            self.database.create_profile("wOrK")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.database.rename_profile(second.profile_id, "WORK")

        self.assertEqual(self.database.get_profile(first.profile_id).name, "Work")
        self.assertEqual(
            self.database.get_profile(second.profile_id).name,
            "Personal",
        )

    def test_compatibility_save_preserves_identity_and_unresolved_marker(self) -> None:
        self.database.save_profile(
            "Legacy Work",
            browser="Chrome",
            account_name="Before",
            engine="chromium",
        )
        profile_id = self.database.get_profile_id("legacy work")
        self.assertIsNotNone(profile_id)
        assert profile_id is not None
        first = self.database.get_profile(profile_id)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.legacy_storage_name, "Legacy Work")

        self.database.save_profile(
            "Legacy Work",
            browser="Chrome",
            account_name="After",
            engine="chromium",
        )
        updated = self.database.get_profile(profile_id)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.profile_id, profile_id)
        self.assertEqual(updated.legacy_storage_name, "Legacy Work")
        self.assertEqual(updated.identity.account_name, "After")
        self.assertEqual(self.database.list_profiles(), ["Legacy Work"])
        self.assertEqual(self.database.list_unresolved_profiles(), [updated])

        self.assertTrue(self.database.delete_profile("Legacy Work"))
        self.assertEqual(self.database.list_profiles(), [])

    def test_same_name_or_even_ready_uuid_storage_is_not_offered_while_unresolved(
        self,
    ) -> None:
        record = self.database.create_profile(
            "Imported Work",
            legacy_storage_name="Imported Work",
            engine="chromium",
        )
        (self.profiles_root / "Imported Work" / "Default").mkdir(parents=True)
        self._ready_storage(record.profile_id)

        self.assertEqual(self.database.profile_engine_specs(), [])
        self.assertEqual(
            [item.profile_id for item in self.database.list_unresolved_profiles()],
            [record.profile_id],
        )

    def test_matching_manifest_and_layout_produce_typed_ready_target(self) -> None:
        record = self.database.create_profile(
            "Managed Work",
            engine="chromium",
        )
        self._ready_storage(record.profile_id)

        self.assertEqual(
            self.database.profile_engine_specs(),
            [BrowserProfileTarget(record.profile_id, "Managed Work", "chromium")],
        )

    def test_mismatched_engine_identity_and_corrupt_manifests_are_excluded(
        self,
    ) -> None:
        wrong_engine = self.database.create_profile(
            "Wrong engine",
            engine="chromium",
        )
        self._ready_storage(wrong_engine.profile_id, "firefox")

        wrong_identity = self.database.create_profile(
            "Wrong identity",
            engine="chromium",
        )
        wrong_identity_path = resolve_profile_storage_path(
            wrong_identity.profile_id,
            self.profiles_root,
        )
        wrong_identity_path.mkdir(parents=True)
        (wrong_identity_path / "Default").mkdir()
        write_profile_manifest(
            wrong_identity_path,
            BrowserProfileManifest.create(
                "chromium",
                profile_id=str(uuid.uuid4()),
            ),
        )

        corrupt = self.database.create_profile("Corrupt", engine="chromium")
        corrupt_path = resolve_profile_storage_path(
            corrupt.profile_id,
            self.profiles_root,
        )
        (corrupt_path / "Default").mkdir(parents=True)
        (corrupt_path / "profile.json").write_text("{broken", encoding="utf-8")

        missing_layout = self.database.create_profile(
            "Missing layout",
            engine="chromium",
        )
        with self.lease_registry.acquire_maintenance(
            missing_layout.profile_id,
            ProfileUse("test", "Prepare missing layout", str(uuid.uuid4())),
        ) as lease:
            write_managed_profile_manifest(
                BrowserProfileManifest.create(
                    "chromium",
                    profile_id=missing_layout.profile_id,
                ),
                self.profiles_root,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )

        self.assertEqual(self.database.profile_engine_specs(), [])

    def test_storage_state_transitions_are_explicit_and_validate_legacy_names(
        self,
    ) -> None:
        record = self.database.create_profile(
            "Imported",
            legacy_storage_name="Old Folder",
            engine="chrome",
        )
        self.assertTrue(
            self.database.finalize_profile_storage(
                record.profile_id,
                engine="chrome",
                browser="Chrome",
                source_profile="Profile 4",
                account_name="Jane Doe",
                account_email="jane@example.com",
            )
        )
        resolved = self.database.get_profile(record.profile_id)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.legacy_storage_name, "")
        self.assertEqual(resolved.engine, "chrome")
        self.assertEqual(resolved.identity.browser, "Chrome")
        self.assertEqual(resolved.identity.source_profile, "Profile 4")
        self.assertEqual(resolved.identity.account_name, "Jane Doe")
        self.assertEqual(resolved.identity.account_email, "jane@example.com")

        self.assertTrue(
            self.database.mark_profile_storage_unresolved(
                record.profile_id,
                "Old Folder",
            )
        )
        self.assertEqual(
            self.database.list_unresolved_profiles()[0].legacy_storage_name,
            "Old Folder",
        )
        with self.assertRaises(ValueError):
            self.database.mark_profile_storage_unresolved(
                record.profile_id,
                "../outside",
            )

    def test_reference_lookup_survives_rename_and_understands_legacy_alias(self) -> None:
        record = self.database.create_profile(
            "Old Display",
            legacy_storage_name="Old Folder",
            engine="chromium",
        )
        stable = SavedRunConfig(
            name="Stable run",
            flow_name="Example",
            device_id=BrowserDeviceConfig(
                BrowserEngine.CHROMIUM,
                screen_mode=ScreenMode.DESKTOP,
                profile_id=record.profile_id,
            ).to_id(),
        )
        legacy = SavedRunConfig(
            name="Legacy run",
            flow_name="Example",
            device_id=BrowserDeviceConfig(
                BrowserEngine.CHROMIUM,
                screen_mode=ScreenMode.DESKTOP,
                profile_name="Old Folder",
            ).to_id(),
        )
        self.database.save_run_config(stable.name, stable)
        self.database.save_run_config(legacy.name, legacy)
        self.database._conn.execute(
            """
            INSERT INTO run_configs (name, data, created_at, updated_at)
            VALUES ('Malformed', ?, 'now', 'now')
            """,
            (json.dumps({"name": "Malformed", "device_id": 42}),),
        )
        self.database._conn.commit()

        self.assertTrue(self.database.rename_profile(record.profile_id, "New Display"))
        by_id = self.database.list_run_configs_referencing_profile(record.profile_id)
        by_name = self.database.list_run_configs_referencing_profile("New Display")

        self.assertEqual(
            {config.name for config in by_id},
            {"Stable run", "Legacy run"},
        )
        self.assertEqual(
            {config.name for config in by_name},
            {"Stable run", "Legacy run"},
        )


if __name__ == "__main__":
    unittest.main()
