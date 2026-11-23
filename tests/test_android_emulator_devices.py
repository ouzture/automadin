from __future__ import annotations

import unittest

from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    WindowsDevice,
    device_from_saved_id,
    display_name_for_saved_device_id,
    make_android_emulator_saved_device_id,
    parse_android_emulator_saved_device_id,
    saved_device_id_for_device,
)


class AndroidEmulatorDeviceTests(unittest.TestCase):
    def test_provider_device_uses_stable_id_and_runtime_serial(self) -> None:
        device = AndroidDevice(
            device_id="127.0.0.1:16384",
            model="SM-F731B",
            provider_id="mumu",
            provider_instance_id="0",
            provider_display_name="MuMu: iom",
        )

        self.assertEqual(device.id, "android-emulator|mumu|0")
        self.assertEqual(device.device_id, "127.0.0.1:16384")
        self.assertEqual(device.name, "MuMu: iom (SM-F731B)")
        self.assertEqual(saved_device_id_for_device(device), device.id)

    def test_saved_id_round_trip_and_fallback_display(self) -> None:
        saved_id = make_android_emulator_saved_device_id("mumu", "2")

        self.assertEqual(saved_id, "android-emulator|mumu|2")
        self.assertEqual(
            parse_android_emulator_saved_device_id(saved_id),
            ("mumu", "2"),
        )
        self.assertEqual(
            display_name_for_saved_device_id(saved_id),
            "MuMu instance 2",
        )

    def test_invalid_saved_ids_are_rejected(self) -> None:
        for value in (
            "android-emulator|",
            "android-emulator|mumu",
            "android-emulator||0",
            "android-emulator|mumu|",
            "android-emulator|mumu|0|extra",
            "usb-serial",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_android_emulator_saved_device_id(value))

    def test_regular_android_and_windows_ids_are_unchanged(self) -> None:
        android = AndroidDevice(device_id="usb-serial", model="Pixel")
        windows = WindowsDevice(hwnd=123, title="Game")

        self.assertEqual(android.id, "usb-serial")
        self.assertEqual(android.name, "Pixel (usb-serial)")
        self.assertEqual(saved_device_id_for_device(android), "usb-serial")
        self.assertEqual(saved_device_id_for_device(windows), "windows|123|Game")

    def test_launcher_parses_stable_emulator_before_browser_pipe_format(self) -> None:
        from automadin.ui.launcher.launcher_view import _device_from_id

        device = _device_from_id("android-emulator|mumu|0")

        self.assertIsInstance(device, AndroidDevice)
        self.assertEqual(device.device_id, "")
        self.assertEqual(device.provider_id, "mumu")
        self.assertEqual(device.provider_instance_id, "0")
        self.assertEqual(device.id, "android-emulator|mumu|0")

    def test_launcher_keeps_existing_pipe_formats(self) -> None:
        windows = device_from_saved_id("windows|123|Game")
        browser = device_from_saved_id("CHROMIUM|hd|desktop|temp|")

        self.assertIsInstance(windows, WindowsDevice)
        self.assertIsInstance(browser, BrowserDevice)

    def test_non_text_saved_target_is_rejected_without_attribute_error(self) -> None:
        self.assertIsNone(parse_android_emulator_saved_device_id(42))
        self.assertEqual(
            display_name_for_saved_device_id(42),
            "Invalid saved target",
        )
        with self.assertRaisesRegex(ValueError, "non-empty text"):
            device_from_saved_id(42)

    def test_malformed_known_saved_target_formats_are_rejected_safely(self) -> None:
        for value in (
            "windows|abc|Title",
            "windows|123",
            "android-emulator|mumu",
            "browser|2|chromium|bad|desktop|profile|not-a-uuid",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    display_name_for_saved_device_id(value),
                    "Invalid saved target",
                )
                with self.assertRaises(ValueError):
                    device_from_saved_id(value)


if __name__ == "__main__":
    unittest.main()
