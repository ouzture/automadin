from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from automadin.services.database import DatabaseService
from automadin.services.setup_center import (
    CURRENT_SETUP_VERSION,
    DEFAULT_ACKNOWLEDGED_VERSION,
    SETUP_ACKNOWLEDGED_VERSION_SETTING,
    SETUP_SELECTED_TRACK_SETTING,
    SetupCenterPreferences,
    SetupCenterService,
    SetupTrack,
)


class SetupCenterServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.database = DatabaseService(
            str(Path(self._temp_dir.name) / "setup-center.db")
        )
        self.service = SetupCenterService(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self._temp_dir.cleanup()

    def test_absent_preferences_use_safe_defaults_and_auto_open(self) -> None:
        self.assertEqual(
            self.service.load_preferences(),
            SetupCenterPreferences(
                acknowledged_version=DEFAULT_ACKNOWLEDGED_VERSION,
                selected_track=None,
            ),
        )
        self.assertTrue(self.service.should_auto_open())

    def test_malformed_and_negative_versions_use_safe_default(self) -> None:
        for value in ("not-a-version", "", "-1", "-25"):
            with self.subTest(value=value):
                self.database.set_app_setting(
                    SETUP_ACKNOWLEDGED_VERSION_SETTING,
                    value,
                )
                self.assertEqual(
                    self.service.load_preferences().acknowledged_version,
                    DEFAULT_ACKNOWLEDGED_VERSION,
                )
                self.assertTrue(self.service.should_auto_open())

    def test_old_current_and_future_versions_control_auto_open(self) -> None:
        cases = (
            (CURRENT_SETUP_VERSION - 1, True),
            (CURRENT_SETUP_VERSION, False),
            (CURRENT_SETUP_VERSION + 3, False),
        )
        for version, expected in cases:
            with self.subTest(version=version):
                self.database.set_app_setting(
                    SETUP_ACKNOWLEDGED_VERSION_SETTING,
                    str(version),
                )
                self.assertEqual(
                    self.service.load_preferences().acknowledged_version,
                    version,
                )
                self.assertEqual(self.service.should_auto_open(), expected)

    def test_selected_track_round_trips_as_plain_value_and_can_be_cleared(self) -> None:
        for track in SetupTrack:
            with self.subTest(track=track):
                self.service.set_selected_track(track)
                self.assertEqual(
                    self.database.get_app_setting(SETUP_SELECTED_TRACK_SETTING),
                    track.value,
                )
                self.assertEqual(
                    self.service.load_preferences().selected_track,
                    track,
                )

        self.service.clear_selected_track()
        self.assertIsNone(self.service.load_preferences().selected_track)

    def test_invalid_stored_track_is_ignored(self) -> None:
        self.database.set_app_setting(
            SETUP_SELECTED_TRACK_SETTING,
            "unsupported",
        )

        self.assertIsNone(self.service.load_preferences().selected_track)

    def test_invalid_track_cannot_be_persisted(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported setup track"):
            self.service.set_selected_track("browser")  # type: ignore[arg-type]
        self.assertIsNone(
            self.database.get_app_setting(SETUP_SELECTED_TRACK_SETTING)
        )

    def test_acknowledge_current_never_downgrades_future_version(self) -> None:
        future_version = CURRENT_SETUP_VERSION + 5
        self.database.set_app_setting(
            SETUP_ACKNOWLEDGED_VERSION_SETTING,
            str(future_version),
        )

        self.service.acknowledge_current()

        self.assertEqual(
            self.database.get_app_setting(SETUP_ACKNOWLEDGED_VERSION_SETTING),
            str(future_version),
        )

    def test_acknowledge_current_persists_current_version(self) -> None:
        self.service.acknowledge_current()

        self.assertEqual(
            self.database.get_app_setting(SETUP_ACKNOWLEDGED_VERSION_SETTING),
            str(CURRENT_SETUP_VERSION),
        )
        self.assertFalse(self.service.should_auto_open())

    def test_reset_acknowledgement_preserves_selected_track(self) -> None:
        self.service.set_selected_track(SetupTrack.ANDROID)
        self.service.acknowledge_current()

        self.service.reset_acknowledgement()

        self.assertEqual(
            self.service.load_preferences(),
            SetupCenterPreferences(
                acknowledged_version=DEFAULT_ACKNOWLEDGED_VERSION,
                selected_track=SetupTrack.ANDROID,
            ),
        )
        self.assertIsNone(
            self.database.get_app_setting(SETUP_ACKNOWLEDGED_VERSION_SETTING)
        )


if __name__ == "__main__":
    unittest.main()
