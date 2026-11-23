from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.managed_emulator import (
    EmulatorCleanupOverride,
    EmulatorCleanupPolicy,
    EmulatorProcessCommand,
    ManagedEmulatorProfile,
    ManagedEmulatorTargetMode,
)
from automadin.models.run_config import SavedRunConfig
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.execution.executor import FlowExecutor
from automadin.models.run_config import RunConfig, RunStatus
from automadin.services.managed_emulator_lifecycle import (
    ManagedEmulatorAcquisition,
    ManagedEmulatorCleanupResult,
)
from automadin.services.database import DatabaseService
from automadin.ui.launcher.launcher_view import LauncherRunWorker
from automadin.ui.launcher.run_config_dialog import RunConfigDialog
from automadin.ui.managed_emulators_dialog import ManagedEmulatorsDialog


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def database(tmp_path) -> DatabaseService:
    db = DatabaseService(str(tmp_path / "managed-emulator-ui.db"))
    db.save_flow(
        "Android flow",
        AutomationFlow(
            "Android flow",
            [FlowCommand("wait", params={"ms": 1})],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        ),
    )
    yield db
    db.close()


def _profile() -> ManagedEmulatorProfile:
    return ManagedEmulatorProfile(
        name="MuMu game",
        launch_command=EmulatorProcessCommand("manager.exe", ("start", "2")),
        target_mode=ManagedEmulatorTargetMode.PROVIDER_INSTANCE,
        provider_id="mumu",
        provider_instance_id="2",
        default_cleanup_policy=EmulatorCleanupPolicy.LEAVE_RUNNING,
    )


def test_run_setup_can_select_profile_and_cleanup_override(
    qapp: QApplication,
    database: DatabaseService,
) -> None:
    del qapp
    profile = _profile()
    database.save_managed_emulator_profile(profile)
    existing = SavedRunConfig(
        name="Managed run",
        flow_name="Android flow",
        device_id="",
        managed_emulator_profile_id=profile.profile_id,
        emulator_cleanup_override=EmulatorCleanupOverride.CLOSE_IF_STARTED,
    )

    dialog = RunConfigDialog(database, existing=existing)
    config = dialog.get_saved_run_config()

    assert dialog._target_type_combo.currentData() == "managed"
    assert config.managed_emulator_profile_id == profile.profile_id
    assert (
        config.emulator_cleanup_override
        is EmulatorCleanupOverride.CLOSE_IF_STARTED
    )
    dialog.close()


def test_profile_editor_saves_exact_endpoint_and_argument_boundaries(
    qapp: QApplication,
    database: DatabaseService,
) -> None:
    del qapp
    dialog = ManagedEmulatorsDialog(database)
    dialog._new_profile()
    dialog._name.setText("Exact endpoint")
    dialog._launch_executable.setText("launcher.exe")
    dialog._launch_arguments.setPlainText("start\ninstance with spaces")
    dialog._target_mode.setCurrentIndex(
        dialog._target_mode.findData(ManagedEmulatorTargetMode.EXACT_ADB_ENDPOINT)
    )
    dialog._adb_endpoint.setText("127.0.0.1:5555")
    dialog._cleanup.setCurrentIndex(
        dialog._cleanup.findData(EmulatorCleanupPolicy.LEAVE_RUNNING)
    )

    dialog._save_profile()

    profile = database.get_managed_emulator_profile_by_name("Exact endpoint")
    assert profile is not None
    assert profile.launch_command.arguments == ("start", "instance with spaces")
    assert profile.adb_endpoint == "127.0.0.1:5555"
    assert dialog.profiles_changed
    dialog.close()


def test_launcher_worker_acquires_session_then_cleans_managed_emulator(
    qapp: QApplication,
) -> None:
    del qapp
    profile = _profile()
    ready_device = AndroidDevice(
        "127.0.0.1:16384",
        provider_id="mumu",
        provider_instance_id="2",
    )
    events: list[str] = []

    class _Lifecycle:
        def acquire(self, selected, **_kwargs):
            events.append("acquire")
            return ManagedEmulatorAcquisition(selected, ready_device, True)

        def cleanup(self, acquisition, policy, **_kwargs):
            assert acquisition.device is ready_device
            assert policy is EmulatorCleanupPolicy.CLOSE_IF_STARTED
            _kwargs["status"]("Cleaning up")
            events.append("cleanup")
            return ManagedEmulatorCleanupResult()

    class _Sessions:
        def __init__(self) -> None:
            self.session: DeviceSession | None = None

        def ensure_android_session(self, device: AndroidDevice):
            events.append("session")
            self.session = DeviceSession(device=device, data={})
            return self.session

        def close_session(self, session_id: str):
            assert self.session is not None
            assert session_id == self.session.session_id
            events.append("close-session")
            self.session = None

    executor = FlowExecutor()
    executor.register_handler("comment", lambda _command, _context: None)
    worker = LauncherRunWorker(
        executor,
        AutomationFlow(
            "Managed worker",
            [FlowCommand("comment")],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        ),
        RunConfig(),
        AndroidDevice(""),
        _Sessions(),
        managed_emulator_profile=profile,
        emulator_cleanup_policy=EmulatorCleanupPolicy.CLOSE_IF_STARTED,
        managed_emulator_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
    )

    worker.run()

    assert events == ["acquire", "session", "close-session", "cleanup"]
    assert worker.status is RunStatus.COMPLETED
    assert worker.ctx.session.device is ready_device
