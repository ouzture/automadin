from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from automadin.engine.browser_engine import BrowserDeviceEngine
from automadin.models.device import BrowserEngine, ScreenMode
from automadin.services.browser_profile_metadata import BrowserProfileTarget
from automadin.ui.browser_profile_import_dialog import _stage_browser_profile


class BrowserProfileImportLayoutTests(unittest.TestCase):
    def test_chrome_profile_is_staged_as_user_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "User Data" / "Profile 2"
            source.mkdir(parents=True)
            (source / "Preferences").write_text("profile", encoding="utf-8")
            (source.parent / "Local State").write_text("state", encoding="utf-8")

            staging = root / "staging"
            _stage_browser_profile(source, staging, "Chrome")

            self.assertEqual(
                (staging / "Default" / "Preferences").read_text(encoding="utf-8"),
                "profile",
            )
            self.assertEqual(
                (staging / "Local State").read_text(encoding="utf-8"), "state"
            )

    def test_unsupported_chromium_variant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            source.mkdir()
            with self.assertRaisesRegex(RuntimeError, "not safely supported"):
                _stage_browser_profile(source, Path(temp_dir) / "staging", "Brave")


class BrowserDeviceCompatibilityTests(unittest.TestCase):
    def test_stable_profiles_only_generate_matching_engine_devices(self) -> None:
        personal_id = str(uuid.uuid4())
        work_id = str(uuid.uuid4())
        devices = BrowserDeviceEngine().generate_devices(
            [
                BrowserProfileTarget(personal_id, "Personal account", "chrome"),
                BrowserProfileTarget(work_id, "Work account", "firefox"),
            ]
        )
        personal = [d.config for d in devices if d.config.profile_id == personal_id]
        work = [d.config for d in devices if d.config.profile_id == work_id]

        self.assertEqual({config.engine for config in personal}, {BrowserEngine.CHROME})
        self.assertEqual({config.engine for config in work}, {BrowserEngine.FIREFOX})
        self.assertEqual({config.screen_mode for config in work}, {ScreenMode.DESKTOP})
        self.assertEqual(
            {config.profile_name for config in personal},
            {"Personal account"},
        )
        self.assertTrue(all(config.to_id().startswith("browser|2|") for config in personal))
        self.assertFalse(
            any(
                d.config.engine == BrowserEngine.FIREFOX
                and d.config.screen_mode != ScreenMode.DESKTOP
                for d in devices
            )
        )

    def test_legacy_profile_specs_are_not_implicitly_discovered(self) -> None:
        devices = BrowserDeviceEngine().generate_devices(  # type: ignore[list-item]
            [("legacy-name", "chromium")]
        )

        self.assertFalse(any(device.config.profile_id for device in devices))

    def test_failed_connect_stops_playwright(self) -> None:
        playwright = Mock()
        engine = BrowserDeviceEngine()
        device = engine.generate_devices()[0]
        with (
            patch("automadin.engine.browser_engine.sync_playwright") as starter,
            patch.object(engine, "_connect_started", side_effect=RuntimeError("boom")),
        ):
            starter.return_value.start.return_value = playwright
            with self.assertRaisesRegex(RuntimeError, "boom"):
                engine.connect(device)
        playwright.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
