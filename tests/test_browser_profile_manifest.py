from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from automadin.services.browser_profile_manifest import (
    PROFILE_MANIFEST_FILENAME,
    BrowserProfileManifest,
    InvalidBrowserProfileManifestError,
    UnsupportedBrowserProfileManifestVersionError,
    read_managed_profile_manifest,
    read_profile_manifest,
    write_managed_profile_manifest,
    _write_profile_manifest as write_profile_manifest,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    BrowserProfileLeaseStateError,
    ProfileUse,
)
from automadin.services.browser_profile_paths import ensure_profile_storage_directory
from automadin.services.browser_profile_paths import (
    InvalidBrowserProfileIdError,
    UnsafeBrowserProfileNameError,
    UnsafeBrowserProfilePathError,
    resolve_legacy_profile_path,
    resolve_profile_storage_path,
)


def _maintenance_lease(profile_id: str):
    return BrowserProfileLeaseRegistry().acquire_maintenance(
        profile_id,
        ProfileUse("test", "Browser profile manifest test", str(uuid.uuid4())),
    )


class BrowserProfileManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name) / "profiles"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_managed_round_trip_uses_uuid_directory_and_non_secret_schema(self) -> None:
        manifest = BrowserProfileManifest.create(
            "chromium",
            created_at=datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
        )

        with _maintenance_lease(manifest.profile_id) as lease:
            path = write_managed_profile_manifest(
                manifest,
                self.root,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        stored = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, PROFILE_MANIFEST_FILENAME)
        self.assertEqual(path.parent.name, manifest.profile_id)
        self.assertEqual(str(uuid.UUID(path.parent.name)), path.parent.name)
        self.assertEqual(
            set(stored),
            {
                "format",
                "manifest_version",
                "profile_id",
                "engine",
                "created_at",
            },
        )
        self.assertEqual(stored["format"], "automadin.browser-profile")
        self.assertEqual(stored["manifest_version"], 1)
        self.assertEqual(stored["profile_id"], manifest.profile_id)
        self.assertEqual(stored["created_at"], "2026-07-18T12:30:00Z")
        self.assertNotIn("name", stored)
        self.assertNotIn("email", stored)
        self.assertEqual(
            read_managed_profile_manifest(manifest.profile_id, self.root),
            manifest,
        )

    def test_validation_rejects_unknown_fields_and_noncanonical_identity(self) -> None:
        identity = str(uuid.uuid4())
        valid = {
            "format": "automadin.browser-profile",
            "manifest_version": 1,
            "profile_id": identity,
            "engine": "firefox",
            "created_at": "2026-07-18T12:30:00Z",
        }

        with self.assertRaisesRegex(
            InvalidBrowserProfileManifestError,
            "unexpected fields: account_email",
        ):
            BrowserProfileManifest.from_mapping(
                {**valid, "account_email": "not-allowed@example.com"}
            )
        with self.assertRaisesRegex(
            InvalidBrowserProfileManifestError,
            "lowercase, hyphenated",
        ):
            BrowserProfileManifest.from_mapping(
                {**valid, "profile_id": identity.upper()}
            )
        with self.assertRaisesRegex(
            InvalidBrowserProfileManifestError,
            "engine must be one of",
        ):
            BrowserProfileManifest.from_mapping({**valid, "engine": "safari"})
        with self.assertRaisesRegex(
            InvalidBrowserProfileManifestError,
            "include a timezone",
        ):
            BrowserProfileManifest.from_mapping(
                {**valid, "created_at": "2026-07-18T12:30:00"}
            )
        with self.assertRaises(UnsupportedBrowserProfileManifestVersionError):
            BrowserProfileManifest.from_mapping(
                {**valid, "manifest_version": 2}
            )
        with self.assertRaisesRegex(
            InvalidBrowserProfileManifestError,
            "not an Automadin browser-profile manifest",
        ):
            BrowserProfileManifest.from_mapping(
                {**valid, "format": "some.vendor.profile"}
            )

    def test_read_rejects_duplicate_json_fields(self) -> None:
        identity = str(uuid.uuid4())
        with _maintenance_lease(identity) as lease:
            directory = ensure_profile_storage_directory(
                identity,
                self.root,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        (directory / PROFILE_MANIFEST_FILENAME).write_text(
            (
                '{"format":"automadin.browser-profile","manifest_version":1,'
                f'"profile_id":"{identity}",'
                '"engine":"chromium","engine":"firefox",'
                '"created_at":"2026-07-18T12:30:00Z"}'
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            InvalidBrowserProfileManifestError,
            "repeats field 'engine'",
        ):
            read_profile_manifest(directory)

    def test_atomic_replace_failure_preserves_previous_manifest(self) -> None:
        identity = str(uuid.uuid4())
        original = BrowserProfileManifest.create(
            "chromium",
            profile_id=identity,
            created_at="2026-07-18T12:30:00Z",
        )
        replacement = BrowserProfileManifest.create(
            "firefox",
            profile_id=identity,
            created_at="2026-07-18T12:31:00Z",
        )
        with _maintenance_lease(identity) as lease:
            path = write_managed_profile_manifest(
                original,
                self.root,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )

        with (
            patch(
                "automadin.services.browser_profile_manifest.os.replace",
                side_effect=OSError("simulated replace failure"),
            ),
            self.assertRaisesRegex(OSError, "simulated replace failure"),
        ):
            write_profile_manifest(path.parent, replacement, overwrite=True)

        self.assertEqual(read_profile_manifest(path.parent), original)
        self.assertEqual(list(path.parent.glob(".profile.*.tmp")), [])

    def test_existing_manifest_is_not_replaced_without_explicit_overwrite(self) -> None:
        manifest = BrowserProfileManifest.create("webkit")
        with _maintenance_lease(manifest.profile_id) as lease:
            path = write_managed_profile_manifest(
                manifest,
                self.root,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )

        with self.assertRaises(FileExistsError):
            write_profile_manifest(path.parent, manifest)

        self.assertEqual(read_profile_manifest(path.parent), manifest)

    def test_managed_read_rejects_directory_manifest_identity_mismatch(self) -> None:
        directory_identity = str(uuid.uuid4())
        different_manifest = BrowserProfileManifest.create("chromium")
        with _maintenance_lease(directory_identity) as lease:
            directory = ensure_profile_storage_directory(
                directory_identity,
                self.root,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        write_profile_manifest(directory, different_manifest)

        with self.assertRaisesRegex(
            InvalidBrowserProfileManifestError,
            "does not match its storage directory",
        ):
            read_managed_profile_manifest(directory_identity, self.root)


class BrowserProfilePathFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name) / "profiles"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_uuid_resolution_does_not_fall_back_to_legacy_display_name(self) -> None:
        legacy = self.root / "Jane - Work Account"
        legacy.mkdir(parents=True)
        profile_id = str(uuid.uuid4())

        self.assertEqual(
            resolve_legacy_profile_path("Jane - Work Account", self.root),
            legacy,
        )
        self.assertEqual(
            resolve_profile_storage_path(profile_id, self.root),
            self.root / profile_id,
        )
        self.assertFalse(resolve_profile_storage_path(profile_id, self.root).exists())

    def test_uuid_resolution_rejects_noncanonical_aliases(self) -> None:
        identity = str(uuid.uuid4())
        for invalid_id in (
            identity.upper(),
            identity.replace("-", ""),
            f"{{{identity}}}",
            "../outside",
            "Jane - Work Account",
            "",
        ):
            with self.subTest(profile_id=invalid_id), self.assertRaises(
                InvalidBrowserProfileIdError
            ):
                resolve_profile_storage_path(invalid_id, self.root)

    def test_uuid_directory_rejects_non_directory_collision(self) -> None:
        profile_id = str(uuid.uuid4())
        self.root.mkdir()
        (self.root / profile_id).write_text("not a profile", encoding="utf-8")

        with self.assertRaisesRegex(
            UnsafeBrowserProfilePathError,
            "not a directory",
        ):
            with _maintenance_lease(profile_id) as lease:
                ensure_profile_storage_directory(
                    profile_id,
                    self.root,
                    lease_registry=lease._registry,
                    maintenance_lease=lease,
                )

    def test_uuid_and_legacy_resolvers_reject_redirected_children(self) -> None:
        profile_id = str(uuid.uuid4())
        self.root.mkdir()
        outside = self.root.parent / "outside"
        outside.mkdir()
        uuid_link = self.root / profile_id
        legacy_link = self.root / "Legacy profile"
        try:
            uuid_link.symlink_to(outside, target_is_directory=True)
            legacy_link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")

        with self.assertRaisesRegex(
            UnsafeBrowserProfilePathError,
            "redirected",
        ):
            resolve_profile_storage_path(profile_id, self.root)
        with self.assertRaisesRegex(
            UnsafeBrowserProfileNameError,
            "redirected",
        ):
            resolve_legacy_profile_path("Legacy profile", self.root)

    def test_manifest_reader_rejects_redirected_profile_json(self) -> None:
        profile_id = str(uuid.uuid4())
        with _maintenance_lease(profile_id) as lease:
            directory = ensure_profile_storage_directory(
                profile_id,
                self.root,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        outside = self.root.parent / "outside-profile.json"
        outside.write_text("{}", encoding="utf-8")
        manifest_path = directory / PROFILE_MANIFEST_FILENAME
        try:
            manifest_path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"File symlinks are unavailable: {exc}")

        with self.assertRaisesRegex(
            InvalidBrowserProfileManifestError,
            "may not be a symlink",
        ):
            read_profile_manifest(directory)

    def test_released_or_wrong_profile_lease_cannot_create_managed_storage(self) -> None:
        profile_id = str(uuid.uuid4())
        released = _maintenance_lease(profile_id)
        self.assertTrue(released.release())

        with self.assertRaisesRegex(BrowserProfileLeaseStateError, "released"):
            ensure_profile_storage_directory(
                profile_id,
                self.root,
                lease_registry=released._registry,
                maintenance_lease=released,
            )
        self.assertFalse(self.root.exists())

        other_profile_id = str(uuid.uuid4())
        with _maintenance_lease(other_profile_id) as wrong_lease:
            with self.assertRaisesRegex(
                BrowserProfileLeaseStateError,
                "different profile",
            ):
                write_managed_profile_manifest(
                    BrowserProfileManifest.create(
                        "chromium",
                        profile_id=profile_id,
                    ),
                    self.root,
                    lease_registry=wrong_lease._registry,
                    maintenance_lease=wrong_lease,
                )
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
