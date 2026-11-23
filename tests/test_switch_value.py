from __future__ import annotations

import pytest

from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.flow_values import (
    FlowInvocationValues,
    FlowRuntimeValueError,
    resolve_flow_inputs,
)
from automadin.execution.handlers.control import handle_switch_value
from automadin.models.device import DeviceSession
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow, FlowCommand
from automadin.models.flow_value import FlowValueCatalog, FlowVariableDefinition
from automadin.models.typed_value import ValueType
from automadin.services.flow_preflight import preflight_flow


_FLOW_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_NUMBER_ID = "10000000-0000-4000-8000-000000000001"
_TEXT_ID = "10000000-0000-4000-8000-000000000002"
_BOOLEAN_ID = "10000000-0000-4000-8000-000000000003"
_DURATION_ID = "10000000-0000-4000-8000-000000000004"


def _reference(identity: str) -> dict[str, str]:
    return {"kind": "variable", "id": identity}


def _literal(value_type: str, value: object) -> dict[str, object]:
    return {"kind": "literal", "type": value_type, "value": value}


def _case(
    operator: str,
    right: dict[str, object] | None,
    child_id: str,
) -> FlowCommand:
    params: dict[str, object] = {
        "operator": operator,
        "ignore_case": False,
    }
    if right is not None:
        params["right"] = right
    return FlowCommand(
        "switch_case",
        params=params,
        children=[FlowCommand(child_id)],
    )


def _default(child_id: str = "default") -> FlowCommand:
    return FlowCommand("switch_default", children=[FlowCommand(child_id)])


def _flow(command: FlowCommand) -> AutomationFlow:
    catalog = FlowValueCatalog.build(
        [],
        [
            FlowVariableDefinition("count", ValueType.NUMBER, 4, id=_NUMBER_ID),
            FlowVariableDefinition("message", ValueType.TEXT, "ready", id=_TEXT_ID),
            FlowVariableDefinition("enabled", ValueType.BOOLEAN, True, id=_BOOLEAN_ID),
            FlowVariableDefinition(
                "timeout", ValueType.DURATION_MS, 1000, id=_DURATION_ID
            ),
        ],
        [],
    )
    return AutomationFlow(
        "Switch flow",
        [command],
        flow_id=_FLOW_ID,
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
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


def _run(flow: AutomationFlow) -> list[str]:
    events: list[str] = []
    executor = FlowExecutor()
    executor.register_handler("switch_value", handle_switch_value)
    for command_id in ("three", "greater", "default"):
        executor.register_handler(
            command_id,
            lambda _command, _ctx, value=command_id: events.append(value),
        )
    executor.execute(flow, _context(flow))
    return events


def test_switch_runs_only_the_first_matching_case() -> None:
    switch = FlowCommand(
        "switch_value",
        params={"source": _reference(_NUMBER_ID)},
        children=[
            _case("equals", _literal("number", 3), "three"),
            _case("greater_than", _literal("number", 2), "greater"),
            _case("greater_than", _literal("number", 1), "three"),
            _default(),
        ],
    )

    assert _run(_flow(switch)) == ["greater"]


def test_switch_runs_default_when_no_case_matches() -> None:
    switch = FlowCommand(
        "switch_value",
        params={"source": _reference(_NUMBER_ID)},
        children=[
            _case("equals", _literal("number", 3), "three"),
            _default(),
        ],
    )

    assert _run(_flow(switch)) == ["default"]


@pytest.mark.parametrize(
    ("source_id", "operator", "right", "ignore_case"),
    [
        (_NUMBER_ID, "equals", _literal("integer", 4), False),
        (_TEXT_ID, "contains", _literal("text", "EAD"), True),
        (_BOOLEAN_ID, "equals", _literal("boolean", True), False),
        (_DURATION_ID, "greater_than", _literal("duration_ms", 500), False),
    ],
)
def test_switch_supports_existing_typed_comparison_kinds(
    source_id: str,
    operator: str,
    right: dict[str, object],
    ignore_case: bool,
) -> None:
    case = _case(operator, right, "greater")
    case.params["ignore_case"] = ignore_case
    switch = FlowCommand(
        "switch_value",
        params={"source": _reference(source_id)},
        children=[case, _default()],
    )

    assert _run(_flow(switch)) == ["greater"]


def test_switch_preflight_rejects_mixed_types_duplicate_cases_and_bad_structure() -> None:
    duplicate = _case("equals", _literal("number", 3), "three")
    switch = FlowCommand(
        "switch_value",
        params={"source": _reference(_NUMBER_ID)},
        children=[
            duplicate,
            duplicate.deep_copy(),
            _case("equals", _literal("text", "mytext"), "three"),
            _default(),
        ],
    )
    flow = _flow(switch)
    flow.commands.append(_case("equals", _literal("number", 4), "three"))

    report = preflight_flow(flow)
    codes = {issue.code for issue in report.errors}

    assert "DUPLICATE_SWITCH_CASE" in codes
    assert "VALUE_CONDITION_TYPE_MISMATCH" in codes
    assert "SWITCH_BRANCH_OUTSIDE_SWITCH" in codes


def test_switch_preflight_requires_one_default_last_and_at_least_one_case() -> None:
    switch = FlowCommand(
        "switch_value",
        params={"source": _reference(_NUMBER_ID)},
        children=[_default(), _default(), FlowCommand("group")],
    )

    codes = {issue.code for issue in preflight_flow(_flow(switch)).errors}

    assert "SWITCH_CASE_REQUIRED" in codes
    assert "SWITCH_DEFAULT_COUNT" in codes
    assert "SWITCH_DEFAULT_LAST" in codes


def test_switch_case_cannot_override_the_parent_source() -> None:
    switch = FlowCommand(
        "switch_value",
        params={"source": _reference(_NUMBER_ID)},
        children=[
            FlowCommand(
                "switch_case",
                params={
                    "left": _reference(_TEXT_ID),
                    "operator": "equals",
                    "right": _literal("number", 4),
                    "ignore_case": False,
                },
                children=[FlowCommand("greater")],
            ),
            _default(),
        ],
    )
    flow = _flow(switch)

    codes = {issue.code for issue in preflight_flow(flow).errors}
    assert "INVALID_SWITCH_CASE_SOURCE" in codes

    with pytest.raises(FlowRuntimeValueError, match="cannot define its own source"):
        _run(flow)
