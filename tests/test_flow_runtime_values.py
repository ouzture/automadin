from __future__ import annotations

from types import MappingProxyType

import pytest

from automadin.engine.session import DeviceSessionService
from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.flow_values import (
    FlowInvocationValues,
    FlowRuntimeValueError,
    RuntimeTypedValue,
    resolve_flow_inputs,
)
from automadin.execution.run_coordinator import RunCoordinator, RunPreparationError
from automadin.execution.runner import RunnerService
from automadin.execution.handlers.control import handle_for_each
from automadin.models.device import DeviceSession
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowValueCatalog,
    FlowVariableDefinition,
)
from automadin.models.run_config import SavedRunConfig
from automadin.models.typed_value import ValueType
from automadin.models.value_reference import (
    BuiltinValueReference,
    FlowValueTemplate,
    InputValueReference,
    LiteralValueReference,
    TemplateTextSegment,
    VariableValueReference,
)
from automadin.services.database import DatabaseService

_INPUT_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
_OPTIONAL_ID = "12345678-1234-4234-8234-123456789abc"
_VARIABLE_ID = "fedcbafe-dcba-4fed-8cba-fedcbafedcba"
_FLOW_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _catalog() -> FlowValueCatalog:
    return FlowValueCatalog.build(
        [
            FlowInputDefinition(
                "count",
                ValueType.INTEGER,
                id=_INPUT_ID,
            ),
            FlowInputDefinition(
                "enabled",
                ValueType.BOOLEAN,
                False,
                id=_OPTIONAL_ID,
            ),
        ],
        [
            FlowVariableDefinition(
                "total",
                ValueType.NUMBER,
                1,
                id=_VARIABLE_ID,
            )
        ],
        [],
    )


def test_input_resolution_uses_stable_ids_precedence_and_immutable_snapshot() -> None:
    catalog = _catalog()

    resolved = resolve_flow_inputs(
        catalog.inputs,
        {_INPUT_ID: "2", _OPTIONAL_ID: "yes"},
        {_INPUT_ID: 3},
    )

    assert resolved == {_INPUT_ID: 3, _OPTIONAL_ID: True}
    assert isinstance(resolved, MappingProxyType)
    with pytest.raises(TypeError):
        resolved[_INPUT_ID] = 4  # type: ignore[index]


def test_input_resolution_rejects_missing_stale_and_duplicate_ids() -> None:
    catalog = _catalog()

    with pytest.raises(FlowRuntimeValueError, match="missing: count"):
        resolve_flow_inputs(catalog.inputs)
    with pytest.raises(FlowRuntimeValueError, match="stale or unknown"):
        resolve_flow_inputs(
            catalog.inputs,
            {"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": 1},
        )
    with pytest.raises(FlowRuntimeValueError, match="duplicate ID"):
        resolve_flow_inputs(
            catalog.inputs,
            {_INPUT_ID: 1, _INPUT_ID.upper(): 2},
        )


def test_invocations_own_typed_variable_state_and_restart_retains_it() -> None:
    catalog = _catalog()
    inputs = resolve_flow_inputs(catalog.inputs, {_INPUT_ID: 2})
    first = FlowInvocationValues(_FLOW_ID, catalog, inputs)
    second = FlowInvocationValues(_FLOW_ID, catalog, inputs)
    context = FlowContext(DeviceSession(), flow_values=first)

    assert context.get_flow_variable(_VARIABLE_ID) == 1.0
    assert context.set_flow_variable(_VARIABLE_ID, "2.5") == 2.5
    context.reset_for_restart()

    assert context.get_flow_variable(_VARIABLE_ID) == 2.5
    assert second.variables.get(_VARIABLE_ID) == 1.0
    with pytest.raises(FlowRuntimeValueError, match="requires a number"):
        context.set_flow_variable(_VARIABLE_ID, "not-a-number")


def test_references_templates_and_nested_builtin_scopes_resolve_typed_values() -> None:
    catalog = _catalog()
    values = FlowInvocationValues(
        _FLOW_ID,
        catalog,
        resolve_flow_inputs(catalog.inputs, {_INPUT_ID: 7}),
    )
    template = FlowValueTemplate(
        (
            TemplateTextSegment("count="),
            InputValueReference(_INPUT_ID),
            TemplateTextSegment(", total="),
            VariableValueReference(_VARIABLE_ID),
            TemplateTextSegment(", item="),
            BuiltinValueReference("item"),
        )
    )

    assert values.resolve(LiteralValueReference(ValueType.BOOLEAN, True)) is True
    context = FlowContext(DeviceSession(), flow_values=values)
    with values.builtin_scope(
        {"item": RuntimeTypedValue(ValueType.TEXT, "outer")}
    ):
        assert context.interpolate_value_template(template) == (
            "count=7, total=1, item=outer"
        )
        with values.builtin_scope(
            {"item": RuntimeTypedValue(ValueType.INTEGER, 9)}
        ):
            assert values.interpolate(template) == "count=7, total=1, item=9"
        assert values.resolve(BuiltinValueReference("item")) == "outer"

    with pytest.raises(FlowRuntimeValueError, match="unavailable in this scope"):
        values.resolve(BuiltinValueReference("item"))


def test_v1_command_interpolation_accepts_literals_but_rejects_legacy_tokens() -> None:
    catalog = _catalog()
    context = FlowContext(
        DeviceSession(),
        flow_values=FlowInvocationValues(
            _FLOW_ID,
            catalog,
            resolve_flow_inputs(catalog.inputs, {_INPUT_ID: 7}),
        ),
    )

    assert context.interpolate("https://example.test/literal") == (
        "https://example.test/literal"
    )
    assert context.interpolate(12) == "12"
    with pytest.raises(FlowRuntimeValueError, match="Legacy name-based interpolation"):
        context.interpolate("https://example.test/{{count}}")
    assert context.interpolate(
        {
            "kind": "template",
            "segments": [
                {"kind": "text", "text": "count="},
                {"kind": "input", "id": _INPUT_ID},
            ],
        }
    ) == "count=7"
    with pytest.raises(FlowRuntimeValueError, match="invalid fields"):
        context.interpolate(
            {
                "kind": "template",
                "segments": [],
                "legacy": True,
            }
        )


def test_real_for_each_handler_exposes_typed_builtins_only_inside_iteration() -> None:
    catalog = _catalog()
    flow = AutomationFlow(
        "Typed loop",
        [
            FlowCommand(
                "for_each",
                params={"values": ["first", "second"]},
                children=[FlowCommand("capture")],
            )
        ],
        flow_id=_FLOW_ID,
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=list(catalog.inputs),
        variables=list(catalog.variables),
    )
    context = FlowContext(
        DeviceSession(),
        flow_values=FlowInvocationValues(
            flow.flow_id,
            flow.value_catalog,
            resolve_flow_inputs(flow.value_catalog.inputs, {_INPUT_ID: 1}),
        ),
    )
    seen: list[tuple[object, object]] = []
    executor = FlowExecutor()
    executor.register_handler("for_each", handle_for_each)
    executor.register_handler(
        "capture",
        lambda _cmd, ctx: seen.append(
            (
                ctx.resolve_value_reference(BuiltinValueReference("item")),
                ctx.resolve_value_reference(BuiltinValueReference("index")),
            )
        ),
    )

    executor.execute(flow, context)

    assert seen == [("first", 0), ("second", 1)]
    assert "item" not in context.variables
    assert "index" not in context.variables
    with pytest.raises(FlowRuntimeValueError, match="unavailable in this scope"):
        context.resolve_value_reference(BuiltinValueReference("item"))


def test_executor_rejects_mismatched_v1_contexts() -> None:
    flow = AutomationFlow(
        "Typed flow",
        [FlowCommand("comment")],
        flow_id=_FLOW_ID,
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )

    with pytest.raises(RuntimeError, match="matching isolated invocation context"):
        FlowExecutor().execute(flow, FlowContext(DeviceSession()))


def test_executor_runs_isolated_command_with_v1_invocation_context() -> None:
    catalog = _catalog()
    values = FlowInvocationValues(
        _FLOW_ID,
        catalog,
        resolve_flow_inputs(catalog.inputs, {_INPUT_ID: 7}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    seen: list[int] = []
    executor = FlowExecutor()
    executor.register_handler(
        "capture",
        lambda command, ctx: seen.append(
            ctx.resolve_command_scalar(command.params["value"], ValueType.INTEGER)
        ),
    )

    executor.execute_command(
        FlowCommand(
            "capture",
            params={"value": InputValueReference(_INPUT_ID).to_dict()},
        ),
        context,
    )

    assert seen == [7]


def test_runtime_rejects_stale_references_and_mismatched_input_snapshots() -> None:
    catalog = _catalog()
    values = FlowInvocationValues(
        _FLOW_ID,
        catalog,
        resolve_flow_inputs(catalog.inputs, {_INPUT_ID: 1}),
    )

    with pytest.raises(FlowRuntimeValueError, match="stale"):
        values.resolve(
            InputValueReference("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        )
    with pytest.raises(FlowRuntimeValueError, match="does not match"):
        FlowInvocationValues(_FLOW_ID, catalog, {_INPUT_ID: 1})
    with pytest.raises(FlowRuntimeValueError, match="requires an integer"):
        FlowInvocationValues(
            _FLOW_ID,
            catalog,
            {_INPUT_ID: "bad", _OPTIONAL_ID: False},
        )
    with pytest.raises(FlowRuntimeValueError, match="flow ID must be a UUID"):
        FlowInvocationValues(None, catalog, {})  # type: ignore[arg-type]


def test_saved_run_config_round_trips_uuid_keyed_input_overrides() -> None:
    config = SavedRunConfig(
        name="Typed run",
        flow_name="Typed flow",
        device_id="android:emulator-5554",
        parameter_overrides={"legacy_name": 1},
        input_overrides={_INPUT_ID: 2},
    )

    restored = SavedRunConfig.from_dict(config.to_dict())

    assert restored.parameter_overrides == {"legacy_name": 1}
    assert restored.input_overrides == {_INPUT_ID: 2}


def test_coordinator_resolves_v1_inputs_without_reinterpreting_legacy_overrides(
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "flow-inputs.db"))
    flow = AutomationFlow(
        "Typed flow",
        [FlowCommand("comment")],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=list(_catalog().inputs),
        variables=list(_catalog().variables),
    )
    database.save_flow(flow.name, flow)
    coordinator = RunCoordinator(
        database,
        DeviceSessionService(),
        RunnerService(),
        worker_factory=lambda **kwargs: kwargs,
    )
    config = SavedRunConfig(
        name="Typed run",
        flow_name=flow.name,
        device_id="android:emulator-5554",
        parameter_overrides={"must_not_be_used": 99},
        input_overrides={_INPUT_ID: "4"},
    )

    prepared = coordinator.prepare(
        config,
        transient_input_overrides={_INPUT_ID: "5"},
    )

    assert prepared.parameter_values == {}
    assert prepared.input_values == {_INPUT_ID: 5, _OPTIONAL_ID: False}
    assert isinstance(prepared.input_values, MappingProxyType)
    registered = coordinator.register(prepared, run_id="typed-run")
    assert registered.worker["parameter_values"] == {}  # type: ignore[index]
    assert registered.worker["input_values"] == prepared.input_values  # type: ignore[index]

    bad_config = SavedRunConfig(
        name="Bad typed run",
        flow_name=flow.name,
        device_id="android:emulator-5554",
        input_overrides={"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": 1},
    )
    with pytest.raises(RunPreparationError) as error:
        coordinator.prepare(bad_config)
    assert error.value.title == "Invalid Flow Inputs"
