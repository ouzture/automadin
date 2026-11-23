from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from automadin.engine.browser_engine import (
    BrowserDeviceEngine,
    BrowserSessionData,
    BrowserSessionTeardownError,
)
from automadin.models.device import BrowserDevice, BrowserDeviceConfig, BrowserEngine
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    ProfileUse,
)


def _session_data() -> tuple[BrowserSessionData, MagicMock, MagicMock, MagicMock]:
    context = MagicMock()
    playwright = MagicMock()
    process = MagicMock()
    playwright._impl_obj._connection._transport._proc = process
    data = BrowserSessionData(
        playwright=playwright,
        browser=None,
        context=context,
    )
    return data, context, playwright, process


def test_windows_process_tree_termination_confirms_teardown() -> None:
    data, context, playwright, process = _session_data()
    context.close.side_effect = RuntimeError("context stuck")
    playwright.stop.side_effect = RuntimeError("driver stop failed")
    process.pid = 4321
    completed = MagicMock(returncode=0, stdout=b"", stderr=b"")

    with (
        patch("automadin.engine.browser_engine.Path.is_file", return_value=True),
        patch(
            "automadin.engine.browser_engine.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        data.close()

    command = run.call_args.args[0]
    assert command[0].lower().endswith(r"\system32\taskkill.exe")
    assert command[1:] == ["/PID", "4321", "/T", "/F"]
    assert run.call_args.kwargs["creationflags"] != 0


def test_unconfirmed_teardown_is_reported_instead_of_silently_succeeding() -> None:
    data, context, playwright, process = _session_data()
    context.close.side_effect = RuntimeError("context stuck")
    playwright.stop.side_effect = RuntimeError("driver stop failed")
    process.kill.side_effect = OSError("process could not be terminated")

    with pytest.raises(BrowserSessionTeardownError, match="could not be confirmed"):
        data.close()


def test_partial_persistent_startup_reports_uncertain_cleanup() -> None:
    profile_id = "8ac2be77-8b13-4cac-8bd8-feb0d171e4ea"
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(
        profile_id,
        ProfileUse("test", "Partial startup", "startup-1"),
    )
    device = BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_name="Work",
            profile_id=profile_id,
        )
    )
    engine = BrowserDeviceEngine()
    playwright = MagicMock()
    process = MagicMock()
    playwright._impl_obj._connection._transport._proc = process
    playwright.stop.side_effect = RuntimeError("driver stop failed")
    process.kill.side_effect = OSError("driver kill failed")

    with (
        patch.object(engine, "_profile_path"),
        patch.object(
            engine,
            "_connect_started",
            side_effect=RuntimeError("page initialization failed"),
        ),
        patch(
            "automadin.engine.browser_engine.sync_playwright"
        ) as start_playwright,
        pytest.raises(BrowserSessionTeardownError, match="remain locked"),
    ):
        start_playwright.return_value.start.return_value = playwright
        engine.connect(device, profile_lease=owner)

    assert registry.snapshot(profile_id) is not None
    owner.release()
