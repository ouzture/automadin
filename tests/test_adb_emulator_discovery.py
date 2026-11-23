from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from automadin.engine.adb_engine import AdbDeviceEngine
from automadin.engine.emulator_discovery import EmulatorAdbEndpoint
from automadin.models.device import AndroidDevice


class FakeProvider:
    provider_id = "mumu"
    provider_display_name = "MuMu"

    def __init__(self, endpoints=None, error: Exception | None = None) -> None:
        self.endpoints = list(endpoints or [])
        self.error = error

    def discover(self):
        if self.error is not None:
            raise self.error
        return list(self.endpoints)


def _endpoint(port: int = 16384) -> EmulatorAdbEndpoint:
    return EmulatorAdbEndpoint(
        provider_id="mumu",
        instance_id="0",
        display_name="MuMu: iom",
        host="127.0.0.1",
        port=port,
    )


def _completed(args, stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class AdbEmulatorDiscoveryTests(unittest.TestCase):
    def test_connects_provider_endpoint_then_returns_stable_device(self) -> None:
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[1:] == ["devices", "-l"] and len(
                [call for call in calls if call[1:] == ["devices", "-l"]]
            ) == 1:
                return _completed(args, "List of devices attached\n")
            if args[1:3] == ["connect", "127.0.0.1:16384"]:
                return _completed(args, "connected to 127.0.0.1:16384\n")
            if args[1:] == ["devices", "-l"]:
                return _completed(
                    args,
                    "List of devices attached\n"
                    "127.0.0.1:16384 device product:b5q model:SM_F731B device:b5q\n",
                )
            raise AssertionError(args)

        engine = AdbDeviceEngine(
            adb_path="adb",
            emulator_providers=[FakeProvider([_endpoint()])],
        )
        with patch("automadin.engine.adb_engine.subprocess.run", side_effect=run):
            devices = engine.list_devices()

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].device_id, "127.0.0.1:16384")
        self.assertEqual(devices[0].id, "android-emulator|mumu|0")
        self.assertEqual(devices[0].name, "MuMu: iom (SM F731B)")
        self.assertEqual(
            calls,
            [
                ["adb", "devices", "-l"],
                ["adb", "connect", "127.0.0.1:16384"],
                ["adb", "devices", "-l"],
            ],
        )

    def test_does_not_reconnect_already_connected_endpoint(self) -> None:
        listing = (
            "List of devices attached\n"
            "127.0.0.1:16384 device product:b5q model:SM_F731B device:b5q\n"
        )
        engine = AdbDeviceEngine(
            adb_path="adb",
            emulator_providers=[FakeProvider([_endpoint()])],
        )
        with patch(
            "automadin.engine.adb_engine.subprocess.run",
            return_value=_completed(["adb", "devices", "-l"], listing),
        ) as run:
            devices = engine.list_devices()

        self.assertEqual(devices[0].id, "android-emulator|mumu|0")
        run.assert_called_once()

    def test_provider_failure_preserves_existing_usb_devices(self) -> None:
        listing = "List of devices attached\nusb-1 device model:Pixel_8\n"
        engine = AdbDeviceEngine(
            adb_path="adb",
            emulator_providers=[FakeProvider(error=RuntimeError("manager failed"))],
        )
        with (
            patch(
                "automadin.engine.adb_engine.subprocess.run",
                return_value=_completed(["adb", "devices", "-l"], listing),
            ),
            self.assertLogs("automadin.engine.adb_engine", level="WARNING"),
        ):
            devices = engine.list_devices()

        self.assertEqual([(device.id, device.model) for device in devices], [("usb-1", "Pixel 8")])

    def test_deduplicates_exact_serials(self) -> None:
        listing = (
            "List of devices attached\n"
            "usb-1 device model:Pixel_8\n"
            "usb-1 device model:Pixel_8\n"
        )
        engine = AdbDeviceEngine(adb_path="adb", emulator_providers=[])
        with patch(
            "automadin.engine.adb_engine.subprocess.run",
            return_value=_completed(["adb", "devices", "-l"], listing),
        ):
            devices = engine.list_devices()

        self.assertEqual([device.id for device in devices], ["usb-1"])

    def test_connect_resolves_stable_instance_to_current_port(self) -> None:
        endpoint = _endpoint(port=20000)
        engine = AdbDeviceEngine(
            adb_path="adb",
            emulator_providers=[FakeProvider([endpoint])],
        )
        target = AndroidDevice(
            device_id="",
            provider_id="mumu",
            provider_instance_id="0",
        )

        def run(args, **kwargs):
            if args[1:] == ["connect", "127.0.0.1:20000"]:
                return _completed(args, "connected to 127.0.0.1:20000\n")
            if args[1:] == ["-s", "127.0.0.1:20000", "get-state"]:
                return _completed(args, "device\n")
            raise AssertionError(args)

        with patch("automadin.engine.adb_engine.subprocess.run", side_effect=run):
            data = engine.connect(target)

        self.assertEqual(data["device_id"], "127.0.0.1:20000")
        self.assertEqual(data["adb_path"], "adb")

    def test_managed_target_reconciles_to_provider_current_endpoint(self) -> None:
        endpoint = _endpoint(port=20000)
        listing = (
            "List of devices attached\n"
            "127.0.0.1:20000 device product:b5q model:SM_F731B device:b5q\n"
        )
        engine = AdbDeviceEngine(
            adb_path="adb",
            emulator_providers=[FakeProvider([endpoint])],
        )
        with patch(
            "automadin.engine.adb_engine.subprocess.run",
            return_value=_completed(["adb", "devices", "-l"], listing),
        ):
            device = engine.discover_managed_emulator_target("mumu", "0")

        self.assertIsNotNone(device)
        assert device is not None
        self.assertIsNotNone(device.device)
        assert device.device is not None
        self.assertEqual(device.device.device_id, "127.0.0.1:20000")
        self.assertEqual(device.device.provider_instance_id, "0")

    def test_managed_target_rejects_a_different_provider_instance(self) -> None:
        engine = AdbDeviceEngine(
            adb_path="adb",
            emulator_providers=[FakeProvider([_endpoint()])],
        )

        with patch(
            "automadin.engine.adb_engine.subprocess.run",
            return_value=_completed(["adb", "devices", "-l"]),
        ) as run:
            device = engine.discover_managed_emulator_target("mumu", "1")

        self.assertIsNone(device)
        run.assert_not_called()

    def test_connect_reports_unavailable_stable_instance(self) -> None:
        engine = AdbDeviceEngine(
            adb_path="adb",
            emulator_providers=[FakeProvider([])],
        )
        target = AndroidDevice(
            device_id="",
            provider_id="mumu",
            provider_instance_id="0",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "MuMu instance 0 is not running or discoverable",
        ):
            engine.connect(target)


if __name__ == "__main__":
    unittest.main()
