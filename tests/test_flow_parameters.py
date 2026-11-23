from __future__ import annotations

from types import MappingProxyType

import pytest

from automadin.engine.session import DeviceSessionService
from automadin.execution.context import FlowContext
from automadin.execution.run_coordinator import RunCoordinator, RunPreparationError
from automadin.execution.runner import RunnerService
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.flow_parameter import (
    FlowParameterDefinition,
    FlowParameterError,
    FlowParameterType,
    canonical_parameter_text,
    resolve_parameter_values,
)
from automadin.models.run_config import SavedRunConfig
from automadin.services.database import DatabaseService


def test_parameter_declarations_round_trip_with_legacy_compatibility() -> None:
    flow = AutomationFlow(
        "Parameterized",
        [FlowCommand("wait", params={"duration": "{{wait_duration}}"})],
        parameters=[
            FlowParameterDefinition(
                "wait_duration",
                FlowParameterType.DURATION_MS,
                300,
                "Delay before continuing",
            ),
            FlowParameterDefinition(
                "enabled",
                FlowParameterType.BOOLEAN,
                True,
            ),
        ],
    )

    assert AutomationFlow.from_dict(flow.to_dict()) == flow
    assert AutomationFlow.from_dict(
        {"name": "Legacy", "commands": []}
    ).parameters == []


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        (FlowParameterType.TEXT, "account", "account"),
        (FlowParameterType.INTEGER, "-7", -7),
        (FlowParameterType.NUMBER, "1.25", 1.25),
        (FlowParameterType.BOOLEAN, "true", True),
        (FlowParameterType.BOOLEAN, "0", False),
        (FlowParameterType.DURATION_MS, "300 ms", 300),
        (FlowParameterType.DURATION_MS, "1.5 s", 1500),
        (FlowParameterType.DURATION_MS, "2 min", 120_000),
    ],
)
def test_parameter_types_coerce_ui_and_json_scalars(
    kind: FlowParameterType,
    value: object,
    expected: object,
) -> None:
    definition = FlowParameterDefinition("value", kind, value)

    assert definition.default == expected


@pytest.mark.parametrize(
    ("name", "kind", "value"),
    [
        ("2bad", FlowParameterType.TEXT, "x"),
        ("_reserved", FlowParameterType.TEXT, "x"),
        ("value", FlowParameterType.INTEGER, True),
        ("value", FlowParameterType.NUMBER, "nan"),
        ("value", FlowParameterType.DURATION_MS, "-1 ms"),
        ("value", FlowParameterType.DURATION_MS, "0.1 ms"),
    ],
)
def test_invalid_parameter_declarations_fail_closed(
    name: str,
    kind: FlowParameterType,
    value: object,
) -> None:
    with pytest.raises((FlowParameterError, ValueError)):
        FlowParameterDefinition(name, kind, value)


def test_resolution_layers_are_typed_read_only_and_reject_stale_overrides() -> None:
    definitions = [
        FlowParameterDefinition("attempts", FlowParameterType.INTEGER, 2),
        FlowParameterDefinition("dry_run", FlowParameterType.BOOLEAN, False),
    ]

    resolved = resolve_parameter_values(
        definitions,
        {"attempts": "3"},
        {"dry_run": "true"},
    )

    assert resolved == {"attempts": 3, "dry_run": True}
    assert isinstance(resolved, MappingProxyType)
    with pytest.raises(TypeError):
        resolved["attempts"] = 4  # type: ignore[index]
    with pytest.raises(FlowParameterError, match="Unknown flow parameter"):
        resolve_parameter_values(definitions, {"removed_name": 1})


def test_declared_parameter_interpolation_precedes_mutable_variables() -> None:
    context = FlowContext(
        DeviceSession(),
        parameters={"mode": "safe", "enabled": True, "attempts": 3},
    )
    context.variables.update({"mode": "unsafe", "legacy": "kept"})
    context.set_variable("mode", "changed")

    assert context.interpolate(
        "{{mode}}/{{enabled}}/{{attempts}}/{{legacy}}"
    ) == "safe/true/3/kept"
    assert context.get_parameter("mode") == "safe"
    assert canonical_parameter_text(1.25) == "1.25"


def test_saved_run_config_parameter_overrides_round_trip() -> None:
    config = SavedRunConfig(
        name="Parameterized setup",
        flow_name="Parameterized",
        device_id="android:emulator-5554",
        parameter_overrides={"attempts": 4},
    )

    restored = SavedRunConfig.from_dict(config.to_dict())

    assert restored.parameter_overrides == {"attempts": 4}
    assert SavedRunConfig.from_dict(
        {
            "name": "Legacy",
            "flow_name": "Flow",
            "device_id": "android:legacy",
        }
    ).parameter_overrides == {}


def test_run_coordinator_blocks_legacy_parameter_flow(
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "parameters.db"))
    flow = AutomationFlow(
        "Parameterized",
        [FlowCommand("comment")],
        parameters=[
            FlowParameterDefinition(
                "attempts",
                FlowParameterType.INTEGER,
                2,
            )
        ],
    )
    database.save_flow(flow.name, flow)
    config = SavedRunConfig(
        name="Setup",
        flow_name=flow.name,
        device_id="android:emulator-5554",
        parameter_overrides={"attempts": "3"},
    )
    coordinator = RunCoordinator(
        database,
        DeviceSessionService(),
        RunnerService(),
        worker_factory=lambda **kwargs: kwargs,
    )

    with pytest.raises(RunPreparationError, match="legacy value model") as blocked:
        coordinator.prepare(
            config,
            transient_parameter_overrides={"attempts": "4"},
        )

    assert blocked.value.title == "Legacy Flow Needs Repair"


def test_run_coordinator_blocks_legacy_flow_before_parameter_override_resolution(
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "stale-parameter.db"))
    flow = AutomationFlow("Parameterized", [FlowCommand("comment")])
    database.save_flow(flow.name, flow)
    coordinator = RunCoordinator(
        database,
        DeviceSessionService(),
        RunnerService(),
        worker_factory=lambda **kwargs: kwargs,
    )

    with pytest.raises(RunPreparationError, match="legacy value model") as blocked:
        coordinator.prepare(
            SavedRunConfig(
                name="Setup",
                flow_name=flow.name,
                device_id=AndroidDevice("emulator-5554").id,
                parameter_overrides={"removed": 1},
            )
        )

    assert blocked.value.title == "Legacy Flow Needs Repair"
