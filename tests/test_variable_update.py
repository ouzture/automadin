from __future__ import annotations

from types import SimpleNamespace

import pytest

from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.flow_values import (
    FlowInvocationValues,
    FlowRuntimeValueError,
    RuntimeTypedValue,
    resolve_flow_inputs,
)
from automadin.execution.handlers.registry import load_command_types
from automadin.execution.handlers.variables import handle_update_variable
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
from automadin.models.typed_value import ValueType
from automadin.models.value_reference import (
    BuiltinValueReference,
    InputValueReference,
    LiteralValueReference,
    VariableValueReference,
)
from automadin.models.variable_update import (
    VariableUpdate,
    VariableUpdateError,
    VariableUpdateOperation,
)
from automadin.services.flow_preflight import preflight_flow

_FLOW_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_INPUT_ID = "10000000-0000-4000-8000-000000000001"
_INTEGER_ID = "20000000-0000-4000-8000-000000000001"
_NUMBER_ID = "20000000-0000-4000-8000-000000000002"
_DURATION_ID = "20000000-0000-4000-8000-000000000003"
_TEXT_ID = "20000000-0000-4000-8000-000000000004"
_BOOLEAN_ID = "20000000-0000-4000-8000-000000000005"


def _catalog() -> FlowValueCatalog:
    return FlowValueCatalog.build(
        [FlowInputDefinition("factor", ValueType.NUMBER, 2, id=_INPUT_ID)],
        [
            FlowVariableDefinition("integer", ValueType.INTEGER, 6, id=_INTEGER_ID),
            FlowVariableDefinition("number", ValueType.NUMBER, 1.5, id=_NUMBER_ID),
            FlowVariableDefinition(
                "duration",
                ValueType.DURATION_MS,
                1000,
                id=_DURATION_ID,
            ),
            FlowVariableDefinition("text", ValueType.TEXT, "aba", id=_TEXT_ID),
            FlowVariableDefinition(
                "boolean",
                ValueType.BOOLEAN,
                False,
                id=_BOOLEAN_ID,
            ),
        ],
        [],
    )


def _values() -> FlowInvocationValues:
    catalog = _catalog()
    return FlowInvocationValues(
        _FLOW_ID,
        catalog,
        resolve_flow_inputs(catalog.inputs),
    )


def _flow(update: VariableUpdate, *, nested: bool = False) -> AutomationFlow:
    update_command = FlowCommand("update_variable", params=update.to_params())
    commands = (
        [
            FlowCommand(
                "for_each",
                params={"values": ["item"]},
                children=[update_command],
            )
        ]
        if nested
        else [update_command]
    )
    catalog = _catalog()
    return AutomationFlow(
        "Variable flow",
        commands,
        flow_id=_FLOW_ID,
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=list(catalog.inputs),
        variables=list(catalog.variables),
    )


def _context(flow: AutomationFlow) -> FlowContext:
    return FlowContext(
        DeviceSession(),
        flow_values=FlowInvocationValues(
            flow.flow_id,
            flow.value_catalog,
            resolve_flow_inputs(flow.value_catalog.inputs),
        ),
    )


def _literal(value_type: ValueType, value: object) -> LiteralValueReference:
    return LiteralValueReference(value_type, value)  # type: ignore[arg-type]


def test_update_variable_contract_round_trips_operation_specific_operands() -> None:
    updates = [
        VariableUpdate(
            _INTEGER_ID,
            VariableUpdateOperation.ADD,
            source=_literal(ValueType.INTEGER, 2),
        ),
        VariableUpdate(
            _TEXT_ID,
            VariableUpdateOperation.REPLACE,
            find=_literal(ValueType.TEXT, "a"),
            replacement=_literal(ValueType.TEXT, "x"),
        ),
        VariableUpdate(_BOOLEAN_ID, VariableUpdateOperation.TOGGLE),
        VariableUpdate(_NUMBER_ID, VariableUpdateOperation.RESET),
    ]

    assert [VariableUpdate.from_params(item.to_params()) for item in updates] == updates


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"target_variable_id": _INTEGER_ID, "operation": "add"},
            "missing source",
        ),
        (
            {
                "target_variable_id": _BOOLEAN_ID,
                "operation": "toggle",
                "source": _literal(ValueType.BOOLEAN, True).to_dict(),
            },
            "unexpected source",
        ),
        (
            {
                "target_variable_id": _TEXT_ID,
                "operation": "replace",
                "find": _literal(ValueType.TEXT, "a").to_dict(),
            },
            "missing replacement",
        ),
        (
            {"target_variable_id": _INTEGER_ID, "operation": "unknown"},
            "Unsupported Update Variable operation",
        ),
    ],
)
def test_update_variable_contract_rejects_missing_extra_and_unknown_fields(
    params: object,
    message: str,
) -> None:
    with pytest.raises(VariableUpdateError, match=message):
        VariableUpdate.from_params(params)


@pytest.mark.parametrize(
    ("target_id", "operation", "source", "expected"),
    [
        (_INTEGER_ID, VariableUpdateOperation.ADD, (ValueType.INTEGER, 2), 8),
        (_INTEGER_ID, VariableUpdateOperation.DIVIDE, (ValueType.INTEGER, 2), 3),
        (_NUMBER_ID, VariableUpdateOperation.DIVIDE, (ValueType.INTEGER, 2), 0.75),
        (_DURATION_ID, VariableUpdateOperation.ADD, (ValueType.DURATION_MS, 500), 1500),
        (_DURATION_ID, VariableUpdateOperation.MULTIPLY, (ValueType.NUMBER, 1.5), 1500),
        (_TEXT_ID, VariableUpdateOperation.APPEND, (ValueType.TEXT, "!"), "aba!"),
        (_TEXT_ID, VariableUpdateOperation.PREPEND, (ValueType.TEXT, "!"), "!aba"),
        (_BOOLEAN_ID, VariableUpdateOperation.SET, (ValueType.BOOLEAN, True), True),
    ],
)
def test_typed_variable_operations_commit_expected_values(
    target_id: str,
    operation: VariableUpdateOperation,
    source: tuple[ValueType, object],
    expected: object,
) -> None:
    values = _values()

    result = values.apply_variable_update(
        VariableUpdate(
            target_id,
            operation,
            source=_literal(*source),
        )
    )

    assert result == expected
    assert values.variables.get(target_id) == expected


def test_text_replace_is_global_rejects_empty_find_and_commits_atomically() -> None:
    values = _values()
    update = VariableUpdate(
        _TEXT_ID,
        VariableUpdateOperation.REPLACE,
        find=_literal(ValueType.TEXT, "a"),
        replacement=_literal(ValueType.TEXT, "x"),
    )

    assert values.apply_variable_update(update) == "xbx"

    invalid = VariableUpdate(
        _TEXT_ID,
        VariableUpdateOperation.REPLACE,
        find=_literal(ValueType.TEXT, ""),
        replacement=_literal(ValueType.TEXT, "x"),
    )
    with pytest.raises(FlowRuntimeValueError, match="cannot be empty"):
        values.apply_variable_update(invalid)
    assert values.variables.get(_TEXT_ID) == "xbx"


def test_toggle_and_reset_need_no_source() -> None:
    values = _values()

    assert values.apply_variable_update(
        VariableUpdate(_BOOLEAN_ID, VariableUpdateOperation.TOGGLE)
    ) is True
    values.apply_variable_update(
        VariableUpdate(
            _INTEGER_ID,
            VariableUpdateOperation.SET,
            source=_literal(ValueType.INTEGER, 9),
        )
    )
    assert values.apply_variable_update(
        VariableUpdate(_INTEGER_ID, VariableUpdateOperation.RESET)
    ) == 6


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (
            VariableUpdate(
                _INTEGER_ID,
                VariableUpdateOperation.DIVIDE,
                source=_literal(ValueType.INTEGER, 4),
            ),
            "whole value",
        ),
        (
            VariableUpdate(
                _NUMBER_ID,
                VariableUpdateOperation.DIVIDE,
                source=_literal(ValueType.INTEGER, 0),
            ),
            "divide by zero",
        ),
        (
            VariableUpdate(
                _DURATION_ID,
                VariableUpdateOperation.SUBTRACT,
                source=_literal(ValueType.DURATION_MS, 2000),
            ),
            "between 0",
        ),
        (
            VariableUpdate(
                _INTEGER_ID,
                VariableUpdateOperation.ADD,
                source=_literal(ValueType.TEXT, "bad"),
            ),
            "cannot use a text source",
        ),
        (
            VariableUpdate(
                _NUMBER_ID,
                VariableUpdateOperation.MULTIPLY,
                source=_literal(ValueType.NUMBER, 1.7e308),
            ),
            "finite number",
        ),
    ],
)
def test_invalid_arithmetic_never_mutates_target(
    update: VariableUpdate,
    message: str,
) -> None:
    values = _values()
    before = values.variables.get(update.target_variable_id)

    with pytest.raises(FlowRuntimeValueError, match=message):
        values.apply_variable_update(update)

    assert values.variables.get(update.target_variable_id) == before


def test_sources_resolve_from_inputs_variables_and_scoped_builtins() -> None:
    values = _values()

    assert values.apply_variable_update(
        VariableUpdate(
            _NUMBER_ID,
            VariableUpdateOperation.MULTIPLY,
            source=InputValueReference(_INPUT_ID),
        )
    ) == 3.0
    assert values.apply_variable_update(
        VariableUpdate(
            _NUMBER_ID,
            VariableUpdateOperation.ADD,
            source=VariableValueReference(_INTEGER_ID),
        )
    ) == 9.0
    with values.builtin_scope(
        {"factor": RuntimeTypedValue(ValueType.INTEGER, 2)}
    ):
        assert values.apply_variable_update(
            VariableUpdate(
                _NUMBER_ID,
                VariableUpdateOperation.DIVIDE,
                source=BuiltinValueReference("factor"),
            )
        ) == 4.5


def test_stale_update_target_and_source_are_rejected_without_mutation() -> None:
    values = _values()
    stale_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"

    with pytest.raises(FlowRuntimeValueError, match="target Variable.*stale"):
        values.apply_variable_update(
            VariableUpdate(
                stale_id,
                VariableUpdateOperation.SET,
                source=_literal(ValueType.INTEGER, 1),
            )
        )
    before = values.variables.snapshot
    with pytest.raises(FlowRuntimeValueError, match="Variable reference.*stale"):
        values.apply_variable_update(
            VariableUpdate(
                _INTEGER_ID,
                VariableUpdateOperation.SET,
                source=VariableValueReference(stale_id),
            )
        )
    assert values.variables.snapshot == before


def test_update_variable_command_metadata_exposes_structured_operands() -> None:
    definition = load_command_types()["update_variable"]
    params = {item["name"]: item for item in definition["params"]}

    assert params["operation"]["options"] == [
        operation.value for operation in VariableUpdateOperation
    ]
    assert params["source"]["type"] == "value_reference"
    assert params["find"]["type"] == "value_reference"
    assert params["replacement"]["type"] == "value_reference"


def test_update_variable_preflight_accepts_model_and_raw_draft() -> None:
    flow = _flow(
        VariableUpdate(
            _INTEGER_ID,
            VariableUpdateOperation.ADD,
            source=_literal(ValueType.INTEGER, 2),
        )
    )

    assert not preflight_flow(flow).errors
    assert not preflight_flow(flow.to_dict()).errors


@pytest.mark.parametrize(
    ("update", "code"),
    [
        (
            VariableUpdate(
                "ffffffff-ffff-4fff-8fff-ffffffffffff",
                VariableUpdateOperation.SET,
                source=_literal(ValueType.INTEGER, 1),
            ),
            "STALE_VARIABLE_UPDATE_TARGET",
        ),
        (
            VariableUpdate(
                _INTEGER_ID,
                VariableUpdateOperation.APPEND,
                source=_literal(ValueType.TEXT, "bad"),
            ),
            "INVALID_VARIABLE_UPDATE_OPERATION",
        ),
        (
            VariableUpdate(
                _INTEGER_ID,
                VariableUpdateOperation.ADD,
                source=_literal(ValueType.TEXT, "bad"),
            ),
            "VARIABLE_UPDATE_TYPE_MISMATCH",
        ),
        (
            VariableUpdate(
                _NUMBER_ID,
                VariableUpdateOperation.DIVIDE,
                source=_literal(ValueType.INTEGER, 0),
            ),
            "INVALID_VARIABLE_UPDATE",
        ),
        (
            VariableUpdate(
                _TEXT_ID,
                VariableUpdateOperation.REPLACE,
                find=_literal(ValueType.TEXT, ""),
                replacement=_literal(ValueType.TEXT, "x"),
            ),
            "INVALID_VARIABLE_UPDATE",
        ),
    ],
)
def test_update_variable_preflight_blocks_invalid_contract_semantics(
    update: VariableUpdate,
    code: str,
) -> None:
    report = preflight_flow(_flow(update))

    assert code in {issue.code for issue in report.errors}


def test_update_variable_preflight_tracks_builtin_scope() -> None:
    update = VariableUpdate(
        _TEXT_ID,
        VariableUpdateOperation.APPEND,
        source=BuiltinValueReference("item"),
    )

    inside = preflight_flow(_flow(update, nested=True))
    outside = preflight_flow(_flow(update))

    assert not inside.errors
    assert "INVALID_VARIABLE_UPDATE_SOURCE" in {
        issue.code for issue in outside.errors
    }


def test_update_variable_preflight_rejects_legacy_flow_usage() -> None:
    update = VariableUpdate(
        _INTEGER_ID,
        VariableUpdateOperation.SET,
        source=_literal(ValueType.INTEGER, 1),
    )
    legacy = AutomationFlow(
        "Legacy",
        [FlowCommand("update_variable", params=update.to_params())],
    )

    report = preflight_flow(legacy)

    assert "INVALID_VARIABLE_UPDATE" in {issue.code for issue in report.errors}


def test_update_variable_handler_executes_only_against_v1_context() -> None:
    update = VariableUpdate(
        _NUMBER_ID,
        VariableUpdateOperation.ADD,
        source=InputValueReference(_INPUT_ID),
    )
    flow = _flow(update)
    context = _context(flow)
    executor = FlowExecutor()
    executor.register_handler("update_variable", handle_update_variable)

    executor.execute(flow, context)

    assert context.get_flow_variable(_NUMBER_ID) == 3.5

    legacy = AutomationFlow(
        "Legacy",
        [FlowCommand("update_variable", params=update.to_params())],
    )
    with pytest.raises(RuntimeError, match="only in version-1 flows"):
        executor.execute(legacy, FlowContext(DeviceSession()))


def test_update_variable_log_values_are_opt_in_and_use_the_variable_name() -> None:
    update = VariableUpdate(
        _NUMBER_ID,
        VariableUpdateOperation.ADD,
        source=InputValueReference(_INPUT_ID),
    )
    flow = _flow(update)
    command = flow.commands[0]
    context = _context(flow)
    logs: list[str] = []
    context.listener = SimpleNamespace(on_log=logs.append)

    handle_update_variable(command, context)

    assert logs == ["Updated Variable 'number' (add); value hidden"]
    assert _NUMBER_ID not in logs[0]
    assert "3.5" not in logs[0]

    command.params["log_values"] = True
    logs.clear()
    handle_update_variable(command, context)

    assert logs == ["Updated Variable 'number' (add) = 5.5"]


def test_update_variable_handler_preserves_state_when_runtime_operand_fails() -> None:
    update = VariableUpdate(
        _INTEGER_ID,
        VariableUpdateOperation.DIVIDE,
        source=_literal(ValueType.INTEGER, 4),
    )
    flow = _flow(update)
    context = _context(flow)
    executor = FlowExecutor()
    executor.register_handler("update_variable", handle_update_variable)

    with pytest.raises(RuntimeError, match="whole value"):
        executor.execute(flow, context)

    assert context.get_flow_variable(_INTEGER_ID) == 6
