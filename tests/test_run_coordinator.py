from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from automadin.engine.browser_session import BrowserSessionHandle
from automadin.execution.run_coordinator import (
    RunCapacityError,
    RunCoordinator,
    RunPreparationError,
)
from automadin.execution.runner import RunnerService
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
    make_windows_saved_device_id,
)
from automadin.models.run_config import BrowserRunMode, SavedRunConfig
from automadin.models.managed_emulator import (
    EmulatorCleanupOverride,
    EmulatorCleanupPolicy,
    EmulatorProcessCommand,
    ManagedEmulatorProfile,
    ManagedEmulatorTargetMode,
)
from automadin.runtime import RuntimeComponentStatus
from automadin.services.database import DatabaseService
from automadin.services.windows_desktop import (
    WindowsDesktopCheck,
    WindowsDesktopState,
)


class _FakeWorker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.running = False
        self.finished = False
        self.profile_release_result = True
        self.managed_release_result = True
        self.managed_attached = False
        self.run_display_name = ""
        self.target_display_name = ""
        self.run_source = ""
        self.ctx = SimpleNamespace(stop=lambda: self.events.append("stop"))

    def configure_run_presentation(
        self,
        *,
        display_name: str,
        target_display_name: str,
        source: str,
    ) -> None:
        self.run_display_name = display_name
        self.target_display_name = target_display_name
        self.run_source = source

    def isFinished(self) -> bool:
        return self.finished

    def isRunning(self) -> bool:
        return self.running

    def attach_reserved_profile_lease(self, lease: object | None) -> None:
        self.events.append(f"attach:{lease}")

    def attach_reserved_managed_emulator_lease(self, lease: object) -> None:
        self.managed_attached = True
        self.events.append(f"attach-managed:{lease}")

    def reserve_device_operation(self) -> None:
        self.events.append("reserve-device")

    def reserve_browser_borrow(self) -> None:
        self.events.append("reserve-browser")

    def release_reserved_device_operation(self) -> bool:
        self.events.append("release-device")
        return True

    def release_reserved_profile_lease(self) -> bool:
        self.events.append("release-profile")
        return self.profile_release_result

    def release_reserved_managed_emulator_lease(self) -> bool:
        if not self.managed_attached:
            return True
        self.events.append("release-managed")
        if self.managed_release_result:
            self.managed_attached = False
        return self.managed_release_result

    def start(self) -> None:
        self.events.append("start")
        self.running = True

    def wait(self, _wait_ms: int) -> None:
        return


@pytest.fixture
def database(tmp_path) -> DatabaseService:
    value = DatabaseService(str(tmp_path / "coordinator.db"))
    yield value
    value.close()


def _saved_setup(database: DatabaseService) -> SavedRunConfig:
    flow = AutomationFlow(
        "Ready",
        [FlowCommand("wait", params={"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    return SavedRunConfig(
        name="Ready setup",
        flow_name=flow.name,
        device_id="emulator-5554",
    )


def _coordinator(
    database: DatabaseService,
    worker: _FakeWorker,
    *,
    max_concurrent: int = 6,
    windows_desktop_probe=None,
) -> RunCoordinator:
    kwargs = {}
    if windows_desktop_probe is not None:
        kwargs["windows_desktop_probe"] = windows_desktop_probe
    return RunCoordinator(
        database,
        SimpleNamespace(),
        RunnerService(max_concurrent=max_concurrent),
        worker_factory=lambda **_kwargs: worker,
        **kwargs,
    )


def _windows_setup(database: DatabaseService, command_id: str = "screenshot") -> SavedRunConfig:
    flow = AutomationFlow(
        "Windows flow",
        [FlowCommand(command_id)],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    return SavedRunConfig(
        name="Windows setup",
        flow_name=flow.name,
        device_id=make_windows_saved_device_id(123, "Calculator"),
    )


def test_prepare_loads_the_current_flow_and_preflight(
    database: DatabaseService,
) -> None:
    worker = _FakeWorker([])
    coordinator = _coordinator(database, worker)
    setup = _saved_setup(database)

    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        prepared = coordinator.prepare(setup)

    assert prepared.saved_config is setup
    assert prepared.flow.name == "Ready"
    assert prepared.device.id == "emulator-5554"
    assert not prepared.preflight.errors


def test_prepare_connected_browser_resolves_live_session_without_runtime_launch_check(
    database: DatabaseService,
) -> None:
    flow = AutomationFlow(
        "Connected browser flow",
        [FlowCommand("wait", params={"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    device = BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    browser_handle = Mock(spec=BrowserSessionHandle)
    browser_handle.is_closed = False
    session = DeviceSession(
        device=device,
        data={"browser_data": browser_handle},
    )
    service = SimpleNamespace(list_sessions=lambda: [session])
    coordinator = RunCoordinator(
        database,
        service,
        RunnerService(),
        worker_factory=lambda **_kwargs: _FakeWorker([]),
    )
    setup = SavedRunConfig(
        name="Connected browser setup",
        flow_name=flow.name,
        device_id="stale-browser-target-is-ignored",
        browser_run_mode=BrowserRunMode.CONNECTED,
    )

    with patch("automadin.execution.run_coordinator.device_runtime_status") as runtime:
        prepared = coordinator.prepare_scheduled(setup)

    assert prepared.connected_session is session
    assert prepared.device is device
    assert not prepared.preflight.errors
    runtime.assert_not_called()


def test_prepare_connected_browser_blocks_when_no_browser_is_connected(
    database: DatabaseService,
) -> None:
    flow = AutomationFlow(
        "Disconnected browser flow",
        [FlowCommand("wait", params={"ms": 1})],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(flow.name, flow)
    coordinator = RunCoordinator(
        database,
        SimpleNamespace(list_sessions=lambda: []),
        RunnerService(),
        worker_factory=lambda **_kwargs: _FakeWorker([]),
    )

    with pytest.raises(RunPreparationError) as unavailable:
        coordinator.prepare_scheduled(
            SavedRunConfig(
                name="Disconnected browser setup",
                flow_name=flow.name,
                device_id="",
                browser_run_mode=BrowserRunMode.CONNECTED,
            )
        )

    assert unavailable.value.title == "Connected Browser Unavailable"
    assert "top target bar" in unavailable.value.public_message


def test_scheduled_windows_ui_preparation_blocks_a_non_interactive_desktop(
    database: DatabaseService,
) -> None:
    probe = Mock(
        return_value=WindowsDesktopCheck(
            WindowsDesktopState.NON_INTERACTIVE,
            "The current Windows desktop is 'Winlogon'.",
        )
    )
    coordinator = _coordinator(
        database,
        _FakeWorker([]),
        windows_desktop_probe=probe,
    )
    setup = _windows_setup(database, command_id="win_focus")

    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        prepared = coordinator.prepare_scheduled(setup)

    assert [issue.code for issue in prepared.preflight.errors] == [
        "WINDOWS_DESKTOP_NOT_INTERACTIVE"
    ]
    assert "locked or non-interactive" in prepared.preflight.errors[0].message
    probe.assert_called_once_with()


def test_scheduled_windows_screenshot_is_allowed_on_a_non_interactive_desktop(
    database: DatabaseService,
) -> None:
    probe = Mock(
        return_value=WindowsDesktopCheck(
            WindowsDesktopState.NON_INTERACTIVE,
            "The current Windows session is locked.",
        )
    )
    coordinator = _coordinator(
        database,
        _FakeWorker([]),
        windows_desktop_probe=probe,
    )
    setup = _windows_setup(database, command_id="screenshot")

    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        prepared = coordinator.prepare_scheduled(setup)

    assert not prepared.preflight.errors
    probe.assert_not_called()


def test_scheduled_windows_ui_preparation_allows_an_interactive_desktop(
    database: DatabaseService,
) -> None:
    probe = Mock(return_value=WindowsDesktopCheck(WindowsDesktopState.INTERACTIVE))
    coordinator = _coordinator(
        database,
        _FakeWorker([]),
        windows_desktop_probe=probe,
    )
    setup = _windows_setup(database, command_id="win_focus")

    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        prepared = coordinator.prepare_scheduled(setup)

    assert not prepared.preflight.errors
    probe.assert_called_once_with()


def test_scheduled_windows_ui_preparation_fails_closed_when_detection_fails(
    database: DatabaseService,
) -> None:
    probe = Mock(side_effect=OSError("desktop API unavailable"))
    coordinator = _coordinator(
        database,
        _FakeWorker([]),
        windows_desktop_probe=probe,
    )
    setup = _windows_setup(database, command_id="win_focus")

    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        prepared = coordinator.prepare_scheduled(setup)

    error = prepared.preflight.errors[0]
    assert error.code == "WINDOWS_DESKTOP_NOT_INTERACTIVE"
    assert "could not verify" in error.message


def test_windows_desktop_gate_is_isolated_from_non_ui_and_other_targets(
    database: DatabaseService,
) -> None:
    probe = Mock(return_value=WindowsDesktopCheck(WindowsDesktopState.NON_INTERACTIVE))
    coordinator = _coordinator(
        database,
        _FakeWorker([]),
        windows_desktop_probe=probe,
    )
    windows_background = _windows_setup(database, command_id="wait")
    android = _saved_setup(database)
    browser_flow = AutomationFlow(
        "Browser flow",
        [FlowCommand("screenshot")],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    database.save_flow(browser_flow.name, browser_flow)
    browser = SavedRunConfig(
        name="Browser setup",
        flow_name=browser_flow.name,
        device_id="chromium-desktop",
    )

    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        windows_prepared = coordinator.prepare_scheduled(windows_background)
        android_prepared = coordinator.prepare_scheduled(android)
        browser_prepared = coordinator.prepare_scheduled(browser)

    assert not windows_prepared.preflight.errors
    assert not android_prepared.preflight.errors
    assert not browser_prepared.preflight.errors
    probe.assert_not_called()


def test_prepare_reports_missing_empty_and_invalid_targets(
    database: DatabaseService,
) -> None:
    coordinator = _coordinator(database, _FakeWorker([]))

    with pytest.raises(RunPreparationError) as missing:
        coordinator.prepare(
            SavedRunConfig(
                name="Missing",
                flow_name="missing",
                device_id="emulator-5554",
            )
        )
    assert missing.value.title == "Error"

    database.save_flow("Empty", AutomationFlow("Empty"))
    with pytest.raises(RunPreparationError) as empty:
        coordinator.prepare(
            SavedRunConfig(
                name="Empty",
                flow_name="Empty",
                device_id="emulator-5554",
            )
        )
    assert empty.value.warning

    database.save_flow(
        "Invalid target",
        AutomationFlow(
            "Invalid target",
            [FlowCommand("wait")],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        ),
    )
    with pytest.raises(RunPreparationError) as invalid:
        coordinator.prepare(
            SavedRunConfig(
                name="Invalid",
                flow_name="Invalid target",
                device_id="",
            )
        )
    assert invalid.value.title == "Invalid Device Target"


def test_prepare_blocks_legacy_flows_for_repair(
    database: DatabaseService,
) -> None:
    coordinator = _coordinator(database, _FakeWorker([]))
    legacy = AutomationFlow("Legacy", [FlowCommand("wait", params={"ms": 1})])
    database.save_flow(legacy.name, legacy)

    with pytest.raises(RunPreparationError) as blocked:
        coordinator.prepare(
            SavedRunConfig(
                name="Legacy setup",
                flow_name=legacy.name,
                device_id="emulator-5554",
            )
        )

    assert blocked.value.title == "Legacy Flow Needs Repair"
    assert blocked.value.warning
    assert "Value Migration Audit" in blocked.value.public_message


def test_register_then_start_preserves_two_stage_ownership(
    database: DatabaseService,
) -> None:
    events: list[str] = []
    worker = _FakeWorker(events)
    service = RunnerService()
    coordinator = RunCoordinator(
        database,
        SimpleNamespace(),
        service,
        worker_factory=lambda **_kwargs: worker,
    )
    setup = _saved_setup(database)
    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        prepared = coordinator.prepare(setup)

    registered = coordinator.register(prepared, run_id="manual-run")
    assert service.get_worker("manual-run") is worker
    assert worker.run_display_name == "Ready setup"
    assert worker.target_display_name == "emulator-5554"
    assert worker.run_source == "manual"
    assert events == []

    with patch(
        "automadin.execution.run_coordinator.acquire_profile_lease",
        return_value="profile-owner",
    ):
        coordinator.start(registered)

    assert events == ["attach:profile-owner", "reserve-device", "start"]


def test_connected_browser_start_borrows_worker_without_attaching_new_owner(
    database: DatabaseService,
) -> None:
    events: list[str] = []
    worker = _FakeWorker(events)
    session = DeviceSession(
        device=BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM)),
        data={"browser_data": Mock(spec=BrowserSessionHandle, is_closed=False)},
    )
    prepared = SimpleNamespace(
        connected_session=session,
        managed_emulator_profile=None,
    )
    registered = SimpleNamespace(worker=worker, prepared=prepared)
    coordinator = RunCoordinator(
        database,
        SimpleNamespace(),
        RunnerService(),
        worker_factory=lambda **_kwargs: worker,
    )

    coordinator.start(registered)

    assert events == ["reserve-browser", "reserve-device", "start"]


def test_managed_emulator_prepare_and_start_use_stable_profile_ownership(
    database: DatabaseService,
) -> None:
    events: list[str] = []
    worker = _FakeWorker(events)
    coordinator = _coordinator(database, worker)
    profile = ManagedEmulatorProfile(
        name="MuMu game instance",
        launch_command=EmulatorProcessCommand("manager.exe", ("start", "3")),
        target_mode=ManagedEmulatorTargetMode.PROVIDER_INSTANCE,
        provider_id="mumu",
        provider_instance_id="3",
        default_cleanup_policy=EmulatorCleanupPolicy.LEAVE_RUNNING,
        force_terminate_owned_process=True,
    )
    database.save_managed_emulator_profile(profile)
    setup = _saved_setup(database)
    setup.managed_emulator_profile_id = profile.profile_id
    setup.emulator_cleanup_override = EmulatorCleanupOverride.CLOSE_IF_STARTED

    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        prepared = coordinator.prepare(setup)

    assert prepared.managed_emulator_profile == profile
    assert prepared.emulator_cleanup_policy is EmulatorCleanupPolicy.CLOSE_IF_STARTED
    assert prepared.device.id == "android-emulator|mumu|3"

    registered = coordinator.register(prepared, run_id="managed-run")
    assert worker.target_display_name == "MuMu game instance"
    with (
        patch(
            "automadin.execution.run_coordinator.acquire_managed_emulator_lease",
            return_value="managed-owner",
        ),
        patch(
            "automadin.execution.run_coordinator.acquire_profile_lease",
            return_value=None,
        ),
    ):
        coordinator.start(registered)

    assert events == [
        "attach-managed:managed-owner",
        "attach:None",
        "reserve-device",
        "start",
    ]


def test_prepare_reports_deleted_managed_emulator_profile(
    database: DatabaseService,
) -> None:
    setup = _saved_setup(database)
    setup.managed_emulator_profile_id = "6a38b318-4190-4af8-a5b8-e91351903285"

    with pytest.raises(RunPreparationError) as unavailable:
        _coordinator(database, _FakeWorker([])).prepare(setup)

    assert unavailable.value.title == "Managed Emulator Unavailable"


def test_capacity_is_checked_before_worker_allocation(
    database: DatabaseService,
) -> None:
    setup = _saved_setup(database)
    factory = Mock()
    coordinator = RunCoordinator(
        database,
        SimpleNamespace(),
        RunnerService(max_concurrent=0),
        worker_factory=factory,
    )
    with patch(
        "automadin.execution.run_coordinator.device_runtime_status",
        return_value=RuntimeComponentStatus(source="test", path=None, usable=True),
    ):
        prepared = coordinator.prepare(setup)

    with pytest.raises(RunCapacityError, match="Maximum concurrent runs"):
        coordinator.register(prepared)

    factory.assert_not_called()


def test_remove_releases_profile_before_service_ownership(
    database: DatabaseService,
) -> None:
    events: list[str] = []
    worker = _FakeWorker(events)
    service = RunnerService()
    service.register_run("finished", worker)
    coordinator = RunCoordinator(
        database,
        SimpleNamespace(),
        service,
        worker_factory=lambda **_kwargs: worker,
    )

    assert coordinator.remove("finished")
    assert events == ["release-profile", "release-device"]
    assert service.get_worker("finished") is None


def test_remove_retains_worker_when_profile_cleanup_is_pending(
    database: DatabaseService,
) -> None:
    events: list[str] = []
    worker = _FakeWorker(events)
    worker.profile_release_result = False
    service = RunnerService()
    service.register_run("pending", worker)
    coordinator = RunCoordinator(
        database,
        SimpleNamespace(),
        service,
        worker_factory=lambda **_kwargs: worker,
    )

    assert not coordinator.remove("pending")
    assert events == ["release-profile"]
    assert service.get_worker("pending") is worker
