from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from automadin.execution.executor import FlowExecutor
from automadin.execution.flow_values import FlowRuntimeValueError
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
)
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.flow_parameter import FlowParameterDefinition, FlowParameterType
from automadin.models.flow_value import FlowInputDefinition, FlowVariableDefinition
from automadin.models.run_config import RunConfig, SavedRunConfig
from automadin.models.typed_value import ValueType
from automadin.services.browser_profile_metadata import (
    BrowserProfileIdentity,
    BrowserProfileTarget,
    StoredBrowserProfile,
)
from automadin.ui.launcher.launcher_view import (
    LauncherRunWorker,
    _browser_device_with_headless,
    _launcher_worker_for_saved_config,
    _profile_reference_error,
    _saved_device_display_name,
)


def test_malformed_saved_variables_are_normalized_without_crashing() -> None:
    saved = SavedRunConfig.from_dict(
        {
            "name": "Imported setup",
            "flow_name": "Example",
            "device_id": "chromium-desktop",
            "variables": ["not", "a", "mapping"],
        }
    )

    assert saved.variables == {}


class _SessionService:
    def __init__(self, session: DeviceSession) -> None:
        self._session = session

    def get_session_by_device(self, _device_id: str) -> DeviceSession:
        return self._session

    def ensure_android_session(self, _device: AndroidDevice) -> DeviceSession:
        return self._session


def _capturing_executor(seen: list[tuple[str, str]]) -> FlowExecutor:
    executor = FlowExecutor()

    def capture(cmd: FlowCommand, ctx) -> None:
        seen.append(
            (
                ctx.get_variable("account"),
                ctx.interpolate(str(cmd.params["template"])),
            )
        )

    executor.register_handler("capture", capture)
    return executor


def test_launcher_worker_builds_private_v1_invocation_context() -> None:
    device = AndroidDevice(device_id="emulator-5554")
    session = DeviceSession(device=device, data={})
    flow = AutomationFlow(
        name="typed flow",
        commands=[FlowCommand("comment")],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[FlowInputDefinition("count", ValueType.INTEGER)],
        variables=[FlowVariableDefinition("total", ValueType.NUMBER, 1)],
    )
    input_id = flow.inputs[0].id
    variable_id = flow.variables[0].id

    worker = _launcher_worker_for_saved_config(
        executor=FlowExecutor(),
        flow=flow,
        saved_config=SavedRunConfig(
            name="typed run",
            flow_name=flow.name,
            device_id=device.id,
            variables={"legacy_leak": "blocked"},
        ),
        device=device,
        session_service=_SessionService(session),
        input_values={input_id: 4},
    )

    assert worker.ctx.inputs == {input_id: 4}
    assert worker.ctx.get_flow_variable(variable_id) == 1.0
    assert worker.ctx.variables == {}
    with pytest.raises(FlowRuntimeValueError, match="Legacy name-based interpolation"):
        worker.ctx.interpolate("{{legacy_leak}}")


def test_launcher_worker_seeds_actual_non_browser_context_from_saved_variables() -> None:
    device = AndroidDevice(device_id="emulator-5554")
    session = DeviceSession(device=device, data={})
    seen: list[tuple[str, str]] = []
    flow = AutomationFlow(
        name="variable flow",
        commands=[
            FlowCommand(
                id="capture",
                params={"template": "{{account}}/{{attempts}}/{{optional}}"},
            )
        ],
    )
    saved_config = SavedRunConfig(
        name="saved variable run",
        flow_name=flow.name,
        device_id=device.id,
        variables={"account": "personal", "attempts": 3, "optional": None},
    )
    worker = _launcher_worker_for_saved_config(
        executor=_capturing_executor(seen),
        flow=flow,
        saved_config=saved_config,
        device=device,
        session_service=_SessionService(session),
    )

    # The pre-session context and the real execution context must both carry
    # the values because UI controls can access worker.ctx before run() starts.
    assert worker.ctx.interpolate("{{account}}/{{attempts}}") == "personal/3"
    worker.run()

    assert seen == [("personal", "personal/3/")]
    assert worker.ctx.session is session
    assert worker.ctx.variables == {
        "account": "personal",
        "attempts": "3",
        "optional": "",
    }


def test_launcher_worker_keeps_parameters_typed_and_ahead_of_variables() -> None:
    device = AndroidDevice(device_id="emulator-5554")
    session = DeviceSession(device=device, data={})
    seen: list[tuple[str, str]] = []
    flow = AutomationFlow(
        name="parameter flow",
        commands=[
            FlowCommand(
                id="capture",
                params={"template": "{{account}}/{{attempts}}"},
            )
        ],
        parameters=[
            FlowParameterDefinition("attempts", FlowParameterType.INTEGER, 2)
        ],
    )
    worker = _launcher_worker_for_saved_config(
        executor=_capturing_executor(seen),
        flow=flow,
        saved_config=SavedRunConfig(
            name="parameter run",
            flow_name=flow.name,
            device_id=device.id,
            variables={"account": "work", "attempts": "mutable"},
        ),
        device=device,
        session_service=_SessionService(session),
        parameter_values={"attempts": 4},
    )

    worker.run()

    assert seen == [("work", "work/4")]
    assert worker.ctx.parameters == {"attempts": 4}


def test_launcher_worker_seeds_browser_context_created_on_run_thread() -> None:
    device = BrowserDevice(config=BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM))
    seen: list[tuple[str, str]] = []

    class _BrowserSessionData:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    browser_data = _BrowserSessionData()
    worker = LauncherRunWorker(
        executor=_capturing_executor(seen),
        flow=AutomationFlow(
            name="browser variable flow",
            commands=[
                FlowCommand(
                    id="capture",
                    params={"template": "https://example.test/{{account}}"},
                )
            ],
        ),
        config=RunConfig(),
        device=device,
        session_service=_SessionService(DeviceSession(device=device)),
        variables={"account": "work"},
    )

    with patch(
        "automadin.engine.browser_engine.BrowserDeviceEngine.connect",
        return_value=browser_data,
    ):
        worker.run()

    assert seen == [("work", "https://example.test/work")]
    assert worker.ctx.variables == {"account": "work"}
    assert browser_data.closed


def _stored_profile(
    profile_id: str,
    *,
    legacy_storage_name: str = "",
    engine: str = "chromium",
) -> StoredBrowserProfile:
    return StoredBrowserProfile(
        profile_id=profile_id,
        name="Work profile",
        created_at="2026-07-18T00:00:00+00:00",
        identity=BrowserProfileIdentity(browser="Chromium"),
        engine=engine,
        legacy_storage_name=legacy_storage_name,
    )


def test_profile_preflight_rejects_legacy_missing_unbound_and_unusable_profiles() -> None:
    legacy_device = BrowserDevice(
        config=BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_name="deleted-profile",
        )
    )
    assert "older unresolved reference" in (
        _profile_reference_error(object(), legacy_device) or ""  # type: ignore[arg-type]
    )

    profile_id = str(uuid.uuid4())
    device = BrowserDevice(
        config=BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_name="Work profile",
            profile_id=profile_id,
        )
    )

    class _MissingProfileDb:
        @staticmethod
        def get_profile(_profile_id: str) -> None:
            return None

        @staticmethod
        def profile_engine_specs() -> list[BrowserProfileTarget]:
            raise AssertionError("layout lookup should not run for a missing record")

    assert "no longer exists" in (
        _profile_reference_error(_MissingProfileDb(), device) or ""  # type: ignore[arg-type]
    )

    class _UnboundProfileDb:
        @staticmethod
        def get_profile(_profile_id: str) -> StoredBrowserProfile:
            return _stored_profile(profile_id, legacy_storage_name="Old Work")

        @staticmethod
        def profile_engine_specs() -> list[BrowserProfileTarget]:
            raise AssertionError("layout lookup should not run for an unbound record")

    assert "still needs setup" in (
        _profile_reference_error(_UnboundProfileDb(), device) or ""  # type: ignore[arg-type]
    )

    class _MissingStorageDb:
        @staticmethod
        def get_profile(_profile_id: str) -> StoredBrowserProfile:
            return _stored_profile(profile_id)

        @staticmethod
        def profile_engine_specs() -> list[BrowserProfileTarget]:
            return []

    assert "identity manifest is missing" in (
        _profile_reference_error(_MissingStorageDb(), device) or ""  # type: ignore[arg-type]
    )


def test_profile_preflight_accepts_only_matching_manifest_target() -> None:
    profile_id = str(uuid.uuid4())
    device = BrowserDevice(
        config=BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_name="Work profile",
            profile_id=profile_id,
        )
    )

    class _ReadyDb:
        @staticmethod
        def get_profile(_profile_id: str) -> StoredBrowserProfile:
            return _stored_profile(profile_id)

        @staticmethod
        def profile_engine_specs() -> list[BrowserProfileTarget]:
            return [BrowserProfileTarget(profile_id, "Work profile", "chromium")]

    assert _profile_reference_error(_ReadyDb(), device) is None  # type: ignore[arg-type]

    class _WrongEngineDb(_ReadyDb):
        @staticmethod
        def profile_engine_specs() -> list[BrowserProfileTarget]:
            return [BrowserProfileTarget(profile_id, "Work profile", "firefox")]

    assert "now requires firefox" in (
        _profile_reference_error(_WrongEngineDb(), device) or ""  # type: ignore[arg-type]
    )


def test_headless_override_and_saved_label_preserve_stable_profile_identity() -> None:
    profile_id = str(uuid.uuid4())
    device = BrowserDevice(
        BrowserDeviceConfig(
            BrowserEngine.CHROMIUM,
            profile_name="Old label",
            profile_id=profile_id,
        )
    )

    updated = _browser_device_with_headless(device, True)

    assert updated.config.headless
    assert updated.config.profile_id == profile_id
    assert updated.config.profile_name == "Old label"

    standard = _browser_device_with_headless(
        updated,
        True,
        headless_uses_headed_user_agent=False,
    )
    assert not standard.config.headless_uses_headed_user_agent

    class _Labels:
        @staticmethod
        def get_profile_display_label(_profile_id: str) -> str:
            return "Jane — Chrome / Personal"

    assert _saved_device_display_name(_Labels(), updated.id) == (  # type: ignore[arg-type]
        "Chromium Desktop · Profile: Jane — Chrome / Personal"
    )
    assert _saved_device_display_name(_Labels(), 42) == (  # type: ignore[arg-type]
        "Invalid saved target"
    )
