from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from automadin.engine.emulator_discovery import (
    EmulatorAdbEndpoint,
    MumuEmulatorProvider,
    mumu_manager_candidates,
)


class MumuEmulatorProviderTests(unittest.TestCase):
    def _manager_path(self, root: str) -> Path:
        path = Path(root) / "MuMuManager.exe"
        path.touch()
        return path

    def test_returns_running_instance_endpoint(self) -> None:
        payload = json.dumps(
            {
                "0": {
                    "index": "0",
                    "name": "iom",
                    "is_process_started": True,
                    "player_state": "start_finished",
                    "adb_host_ip": "127.0.0.1",
                    "adb_port": 16384,
                }
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager_path(temp_dir)
            provider = MumuEmulatorProvider(
                manager_candidates=lambda: [manager],
                runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], 0, payload, ""
                ),
            )

            endpoints = provider.discover()

        self.assertEqual(
            endpoints,
            [
                EmulatorAdbEndpoint(
                    provider_id="mumu",
                    instance_id="0",
                    display_name="MuMu: iom",
                    host="127.0.0.1",
                    port=16384,
                )
            ],
        )
        self.assertEqual(endpoints[0].serial, "127.0.0.1:16384")

    def test_ignores_stopped_and_invalid_instances(self) -> None:
        payload = json.dumps(
            {
                "0": {
                    "index": "0",
                    "is_process_started": False,
                    "adb_host_ip": "127.0.0.1",
                    "adb_port": 16384,
                },
                "1": {
                    "index": "1",
                    "is_process_started": True,
                    "adb_host_ip": "",
                    "adb_port": 16385,
                },
                "2": {
                    "index": "2",
                    "is_process_started": True,
                    "adb_host_ip": "127.0.0.1",
                    "adb_port": 70000,
                },
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager_path(temp_dir)
            provider = MumuEmulatorProvider(
                manager_candidates=lambda: [manager],
                runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], 0, payload, ""
                ),
            )

            self.assertEqual(provider.discover(), [])

    def test_returns_empty_for_invalid_json_nonzero_exit_and_timeout(self) -> None:
        outcomes = [
            subprocess.CompletedProcess([], 0, "not-json", ""),
            subprocess.CompletedProcess([], 1, "", "failed"),
            subprocess.TimeoutExpired(["MuMuManager.exe"], 5),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager_path(temp_dir)
            for outcome in outcomes:
                with self.subTest(outcome=type(outcome).__name__):
                    def runner(*args, **kwargs):
                        if isinstance(outcome, BaseException):
                            raise outcome
                        return outcome

                    provider = MumuEmulatorProvider(
                        manager_candidates=lambda: [manager],
                        runner=runner,
                    )
                    with self.assertLogs(
                        "automadin.engine.emulator_discovery",
                        level="WARNING",
                    ):
                        self.assertEqual(provider.discover(), [])

    def test_derives_manager_from_running_nonstandard_install(self) -> None:
        process_path = Path(
            r"D:\games\MuMuPlayer\nx_device\12.0\shell\MuMuNxDevice.exe"
        )

        candidates = mumu_manager_candidates(
            process_paths=[process_path],
            environ={},
            require_existing=False,
        )

        self.assertIn(
            Path(r"D:\games\MuMuPlayer\nx_main\MuMuManager.exe"),
            candidates,
        )

    def test_derives_manager_from_registered_nonstandard_install(self) -> None:
        candidates = mumu_manager_candidates(
            process_paths=[],
            install_locations=[Path(r"D:\games\MuMuPlayer")],
            environ={},
            require_existing=False,
        )

        self.assertIn(
            Path(r"D:\games\MuMuPlayer\nx_main\MuMuManager.exe"),
            candidates,
        )


if __name__ == "__main__":
    unittest.main()
