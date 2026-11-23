from __future__ import annotations

import unittest

from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
    browser_profile_id_from_saved_device_id,
    browser_profile_name_from_saved_device_id,
    browser_profile_reference_from_saved_device_id,
    canonical_browser_profile_id,
    device_from_saved_id,
)


PROFILE_ID = "12345678-1234-4abc-9def-1234567890ab"


class BrowserProfileIdentityTests(unittest.TestCase):
    def test_versioned_profile_device_id_round_trips_all_runtime_options(self) -> None:
        config = BrowserDeviceConfig(
            engine=BrowserEngine.FIREFOX,
            headless=True,
            screen_mode=ScreenMode.DESKTOP,
            profile_id=PROFILE_ID,
        )

        saved_id = config.to_id()

        self.assertEqual(
            saved_id,
            f"browser|2|firefox|hl|desktop|profile|{PROFILE_ID}",
        )
        parsed = device_from_saved_id(saved_id)
        self.assertIsInstance(parsed, BrowserDevice)
        assert isinstance(parsed, BrowserDevice)
        self.assertEqual(parsed.config, config)
        self.assertEqual(browser_profile_id_from_saved_device_id(saved_id), PROFILE_ID)
        self.assertIsNone(browser_profile_name_from_saved_device_id(saved_id))
        self.assertEqual(
            browser_profile_reference_from_saved_device_id(saved_id).profile_id,
            PROFILE_ID,
        )

    def test_legacy_profile_references_remain_readable(self) -> None:
        current_legacy = "chromium-desktop-work-profile-eu"
        pipe_legacy = "chromium|hl|desktop|perm|work-profile-eu"

        self.assertEqual(
            browser_profile_name_from_saved_device_id(current_legacy),
            "work-profile-eu",
        )
        self.assertEqual(
            browser_profile_name_from_saved_device_id(pipe_legacy),
            "work-profile-eu",
        )
        self.assertIsNone(browser_profile_id_from_saved_device_id(current_legacy))
        self.assertIsNone(browser_profile_id_from_saved_device_id(pipe_legacy))

    def test_canonical_profile_ids_reject_alternate_or_malformed_spellings(self) -> None:
        self.assertEqual(canonical_browser_profile_id(PROFILE_ID), PROFILE_ID)
        for invalid in (
            PROFILE_ID.upper(),
            PROFILE_ID.replace("-", ""),
            "{12345678-1234-4abc-9def-1234567890ab}",
            "not-a-uuid",
            "",
            None,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                canonical_browser_profile_id(invalid)

    def test_malformed_versioned_ids_are_not_profile_references(self) -> None:
        malformed = (
            f"browser|2|chromium|maybe|desktop|profile|{PROFILE_ID}",
            f"browser|2|unknown|hd|desktop|profile|{PROFILE_ID}",
            f"browser|2|chromium|hd|invalid|profile|{PROFILE_ID}",
            f"browser|2|chromium|hd|desktop|wrong|{PROFILE_ID}",
            f"browser|2|chromium|hd|desktop|profile|{PROFILE_ID.upper()}",
            "browser|2|chromium|hd|desktop|profile",
        )
        for saved_id in malformed:
            with self.subTest(saved_id=saved_id):
                self.assertIsNone(
                    browser_profile_reference_from_saved_device_id(saved_id)
                )


if __name__ == "__main__":
    unittest.main()
