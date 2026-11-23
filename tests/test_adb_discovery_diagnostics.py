from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from automadin.engine.adb_engine import (
    AdbDeviceEngine,
    AdbDiscoveryDiagnosticKind,
)
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


def _engine() -> AdbDeviceEngine:
    return AdbDeviceEngine(adb_path="selected-adb.exe", emulator_providers=[])


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (FileNotFoundError(2, "missing"), AdbDiscoveryDiagnosticKind.MISSING_EXECUTABLE),
        (
            subprocess.TimeoutExpired(["selected-adb.exe", "devices"], 10),
            AdbDiscoveryDiagnosticKind.TIMEOUT,
        ),
        (PermissionError(13, "denied"), AdbDiscoveryDiagnosticKind.COMMAND_FAILED),
        (OSError(193, "not an executable"), AdbDiscoveryDiagnosticKind.COMMAND_FAILED),
    ],
)
def test_adb_discovery_preserves_start_failure(error: OSError, kind) -> None:
    with patch("automadin.engine.adb_engine.subprocess.run", side_effect=error) as run:
        result = _engine().discover_devices()

    assert result.devices == ()
    assert result.diagnostics[0].kind == kind
    assert result.diagnostics[0].action
    assert not result.command_succeeded
    assert run.call_args.kwargs["creationflags"] == HIDDEN_PROCESS_CREATION_FLAGS


def test_adb_discovery_preserves_nonzero_command_failure() -> None:
    completed = subprocess.CompletedProcess(
        ["selected-adb.exe", "devices", "-l"],
        1,
        "",
        "cannot start daemon",
    )
    with patch("automadin.engine.adb_engine.subprocess.run", return_value=completed):
        result = _engine().discover_devices()

    diagnostic = result.diagnostics[0]
    assert diagnostic.kind == AdbDiscoveryDiagnosticKind.COMMAND_FAILED
    assert "exit code 1" in diagnostic.message
    assert diagnostic.detail == "cannot start daemon"


def test_adb_discovery_reports_ready_unauthorized_and_offline_devices() -> None:
    listing = (
        "List of devices attached\n"
        "ready-1 device product:panther model:Pixel_7 device:panther\n"
        "locked-1 unauthorized usb:1-1 transport_id:2\n"
        "stale-1 offline transport_id:3\n"
    )
    completed = subprocess.CompletedProcess(
        ["selected-adb.exe", "devices", "-l"], 0, listing, ""
    )
    with patch("automadin.engine.adb_engine.subprocess.run", return_value=completed):
        engine = _engine()
        result = engine.discover_devices()
        compatible_devices = engine.list_devices()

    assert [(device.id, device.model) for device in result.devices] == [
        ("ready-1", "Pixel 7")
    ]
    diagnostics = {diagnostic.kind: diagnostic for diagnostic in result.diagnostics}
    assert set(diagnostics) == {
        AdbDiscoveryDiagnosticKind.SUCCESS,
        AdbDiscoveryDiagnosticKind.UNAUTHORIZED,
        AdbDiscoveryDiagnosticKind.OFFLINE,
    }
    assert diagnostics[AdbDiscoveryDiagnosticKind.UNAUTHORIZED].device_ids == (
        "locked-1",
    )
    assert diagnostics[AdbDiscoveryDiagnosticKind.OFFLINE].device_ids == ("stale-1",)
    assert result.command_succeeded
    assert [device.id for device in compatible_devices] == ["ready-1"]
    assert compatible_devices.discovery_result.command_succeeded


def test_successful_empty_adb_probe_explains_next_step() -> None:
    completed = subprocess.CompletedProcess(
        ["selected-adb.exe", "devices", "-l"],
        0,
        "List of devices attached\n",
        "",
    )
    with patch("automadin.engine.adb_engine.subprocess.run", return_value=completed):
        result = _engine().discover_devices()

    assert result.devices == ()
    assert result.diagnostics[0].kind == AdbDiscoveryDiagnosticKind.SUCCESS
    assert "enable USB debugging" in result.diagnostics[0].action
