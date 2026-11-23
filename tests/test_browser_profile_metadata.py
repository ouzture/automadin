from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
)
from automadin.services.browser_profile_metadata import (
    BrowserProfileIdentity,
    BrowserProfileTarget,
    StoredBrowserProfile,
    detect_browser_profile,
    profile_aware_device_name,
)
from automadin.services.browser_profile_manifest import (
    BrowserProfileManifest,
    write_managed_profile_manifest,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.services.database import DatabaseService


def _maintenance_lease(profile_id: str):
    return BrowserProfileLeaseRegistry().acquire_maintenance(
        profile_id,
        ProfileUse("test", "Browser profile metadata test", str(uuid.uuid4())),
    )


class BrowserProfileMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_detects_chromium_profile_and_account_identity(self) -> None:
        user_data = self.root / "Google" / "Chrome" / "User Data"
        profile = user_data / "Profile 1"
        profile.mkdir(parents=True)
        (profile / "Preferences").write_text(
            json.dumps(
                {
                    "profile": {"name": "Personal"},
                    "account_info": [
                        {"full_name": "Jane Doe", "email": "jane@example.com"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (user_data / "Local State").write_text(
            json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "Person 1"}}}}),
            encoding="utf-8",
        )

        identity = detect_browser_profile(profile)

        self.assertEqual(identity.browser, "Chrome")
        self.assertEqual(identity.source_profile, "Personal")
        self.assertEqual(identity.account_name, "Jane Doe")
        self.assertEqual(identity.account_email, "jane@example.com")
        self.assertEqual(
            identity.friendly_label(),
            "Chrome / Personal — Jane Doe (jane@example.com)",
        )

    def test_detects_firefox_signed_in_email_and_survives_bad_json(self) -> None:
        profile = self.root / "Mozilla" / "Firefox" / "Profiles" / "abc.default-release"
        profile.mkdir(parents=True)
        (profile / "signedInUser.json").write_text(
            json.dumps({"accountData": {"email": "fox@example.com"}}),
            encoding="utf-8",
        )
        (profile / "Preferences").write_text("not json", encoding="utf-8")

        identity = detect_browser_profile(profile)

        self.assertEqual(identity.browser, "Firefox")
        self.assertEqual(identity.source_profile, "abc.default-release")
        self.assertEqual(identity.account_email, "fox@example.com")

    def test_profile_aware_device_name_does_not_change_device_id(self) -> None:
        profile_id = str(uuid.uuid4())
        device = BrowserDevice(
            BrowserDeviceConfig(
                BrowserEngine.CHROMIUM,
                screen_mode=ScreenMode.DESKTOP,
                profile_name="My profile",
                profile_id=profile_id,
            )
        )

        display = profile_aware_device_name(
            device,
            {profile_id: "My profile — Chrome / Personal — Jane Doe"},
        )

        self.assertEqual(
            display,
            "Chromium Desktop · Profile: My profile — Chrome / Personal — Jane Doe",
        )
        self.assertEqual(
            device.id,
            f"browser|2|chromium|hd|desktop|profile|{profile_id}",
        )

    def test_friendly_label_falls_back_to_internal_name(self) -> None:
        self.assertEqual(BrowserProfileIdentity().friendly_label("legacy"), "legacy")

    def test_stored_profile_carries_stable_and_legacy_storage_identity(self) -> None:
        profile_id = str(uuid.uuid4())
        record = StoredBrowserProfile(
            profile_id=profile_id,
            name="Work",
            created_at="now",
            identity=BrowserProfileIdentity(browser="Chrome"),
            engine="chrome",
            legacy_storage_name="old-work-folder",
        )

        self.assertEqual(record.profile_id, profile_id)
        self.assertEqual(record.legacy_storage_name, "old-work-folder")
        self.assertEqual(record.display_label, "Work — Chrome")


class BrowserProfileDatabaseMigrationTests(unittest.TestCase):
    def test_old_profiles_table_is_migrated_and_metadata_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "old.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                """CREATE TABLE profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO profiles (name, created_at) VALUES ('legacy', 'now')"
            )
            connection.commit()
            connection.close()

            db = DatabaseService(str(db_path))
            try:
                self.assertEqual(db.get_profile_display_label("legacy"), "legacy")
                legacy_record = next(
                    record
                    for record in db.list_profile_records()
                    if record.name == "legacy"
                )
                self.assertEqual(
                    str(uuid.UUID(legacy_record.profile_id)),
                    legacy_record.profile_id,
                )
                self.assertEqual(legacy_record.legacy_storage_name, "legacy")
                db.save_profile(
                    "personal",
                    browser="Chrome",
                    source_profile="Personal",
                    account_name="Jane Doe",
                    account_email="jane@example.com",
                    engine="chrome",
                )
                profile_id = db.get_profile_id("personal")
                self.assertIsNotNone(profile_id)
                with _maintenance_lease(profile_id) as lease:
                    manifest_path = write_managed_profile_manifest(
                        BrowserProfileManifest.create(
                            "chrome",
                            profile_id=profile_id,
                        ),
                        Path(temp_dir) / "profiles",
                        lease_registry=lease._registry,
                        maintenance_lease=lease,
                    )
                (manifest_path.parent / "Default").mkdir()
                self.assertTrue(
                    db.mark_profile_storage_resolved(
                        profile_id,
                        engine="chrome",
                    )
                )
                self.assertEqual(
                    db.get_profile_display_label("personal"),
                    "personal — Chrome / Personal — Jane Doe (jane@example.com)",
                )
                self.assertEqual(
                    db.profile_engine_specs(),
                    [BrowserProfileTarget(profile_id, "personal", "chrome")],
                )
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
