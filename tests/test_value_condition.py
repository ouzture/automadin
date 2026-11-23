from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.flow_values import FlowInvocationValues, resolve_flow_inputs
from automadin.execution.handlers.control import handle_if_value, handle_while_value
from automadin.models.device import DeviceSession
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow, FlowCommand
from automadin.models.flow_value import FlowValueCatalog, FlowVariableDefinition
from automadin.models.typed_value import ValueType
from automadin.models.value_condition import ValueCondition, ValueConditionError
from automadin.services.flow_preflight import preflight_flow
from automadin.ui.flow_editor.value_condition_fields import (
    configure_value_condition_param_defs,
    normalize_value_condition_editor_change,
)
from automadin.ui.flow_editor.param_editor import ParamEditorWidget


_FLOW_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_TEXT_ID = "10000000-0000-4000-8000-000000000001"
_NUMBER_ID = "10000000-0000-4000-8000-000000000002"
_DURATION_ID = "10000000-0000-4000-8000-000000000003"


class _LogListener:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def on_log(self, message: str) -> None:
        self.messages.append(message)

    def on_step_start(self, _command_id: str, _step_id: str) -> None:
        pass

    def on_step_end(self, _command_id: str, _step_id: str) -> None:
        pass


def _catalog() -> FlowValueCatalog:
    return FlowValueCatalog.build(
        [],
        [
            FlowVariableDefinition("message", ValueType.TEXT, "Hello", id=_TEXT_ID),
            FlowVariableDefinition("count", ValueType.NUMBER, 4, id=_NUMBER_ID),
            FlowVariableDefinition(
                "loop_limit", ValueType.DURATION_MS, 1000, id=_DURATION_ID
            ),
        ],
        [],
    )


def _flow(command: FlowCommand) -> AutomationFlow:
    catalog = _catalog()
    return AutomationFlow(
        "Value condition",
        [command],
        flow_id=_FLOW_ID,
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=list(catalog.variables),
    )


def _reference(kind: str, identity: str) -> dict[str, str]:
    return {"kind": kind, "id": identity}


def test_if_value_branches_and_can_ignore_text_case() -> None:
    command = FlowCommand(
        "if_value",
        params={
            "left": _reference("variable", _TEXT_ID),
            "operator": "equals",
            "right": {"kind": "literal", "type": "text", "value": "hello"},
            "ignore_case": True,
            "log_values": True,
        },
        children=[FlowCommand("then")],
        else_children=[FlowCommand("else")],
    )
    flow = _flow(command)
    context = FlowContext(
        DeviceSession(),
        flow_values=FlowInvocationValues(
            flow.flow_id,
            flow.value_catalog,
            resolve_flow_inputs(flow.value_catalog.inputs),
        ),
    )
    events: list[str] = []
    listener = _LogListener()
    context.listener = listener
    executor = FlowExecutor()
    executor.register_handler("if_value", handle_if_value)
    executor.register_handler("then", lambda _command, _ctx: events.append("then"))
    executor.register_handler("else", lambda _command, _ctx: events.append("else"))

    executor.execute(flow, context)

    assert events == ["then"]
    assert (
        "If Value: Variable 'message' (text) = \"Hello\"; equals; "
        "Literal (text) = \"hello\" (ignoring case) → MATCHED"
    ) in listener.messages

    command.params["log_values"] = False
    listener.messages.clear()
    executor.execute(flow, context)

    assert "If Value: comparison matched; values hidden" in listener.messages
    assert not any("Hello" in message for message in listener.messages)


def test_value_condition_rejects_incompatible_types_before_execution() -> None:
    command = FlowCommand(
        "if_value",
        params={
            "left": _reference("variable", _NUMBER_ID),
            "operator": "contains",
            "right": {"kind": "literal", "type": "text", "value": "4"},
            "ignore_case": False,
        },
    )

    report = preflight_flow(_flow(command))

    assert any(issue.code == "VALUE_CONDITION_TYPE_MISMATCH" for issue in report.issues)


def test_while_value_rechecks_the_condition_after_each_child_execution() -> None:
    command = FlowCommand(
        "while_value",
        params={
            "left": _reference("variable", _NUMBER_ID),
            "operator": "less_than",
            "right": {"kind": "literal", "type": "number", "value": 3},
            "ignore_case": False,
            "timeout": _reference("variable", _DURATION_ID),
        },
        children=[FlowCommand("increment")],
    )
    flow = _flow(command)
    context = FlowContext(
        DeviceSession(),
        flow_values=FlowInvocationValues(
            flow.flow_id,
            flow.value_catalog,
            resolve_flow_inputs(flow.value_catalog.inputs),
        ),
    )
    context.flow_values.variables.set(_NUMBER_ID, 0)
    executor = FlowExecutor()
    executor.register_handler("while_value", handle_while_value)

    assert not any(
        issue.location.command_id == "while_value"
        for issue in preflight_flow(flow).issues
    )

    def increment(_command: FlowCommand, current_context: FlowContext) -> None:
        value = current_context.flow_values.variables.get(_NUMBER_ID)
        current_context.flow_values.variables.set(_NUMBER_ID, value + 1)

    executor.register_handler("increment", increment)
    executor.execute(flow, context)

    assert context.flow_values.variables.get(_NUMBER_ID) == 3


def test_empty_operator_needs_no_right_value_and_editor_clears_it() -> None:
    params = {
        "left": _reference("variable", _TEXT_ID),
        "operator": "equals",
        "right": {"kind": "literal", "type": "text", "value": ""},
        "ignore_case": False,
    }
    normalized = normalize_value_condition_editor_change(params, "operator", "is_empty")

    assert "right" not in normalized
    assert ValueCondition.from_params(normalized).right is None
    assert [
        definition["name"]
        for definition in configure_value_condition_param_defs(
            [{"name": "left"}, {"name": "right"}], normalized
        )
    ] == ["left"]


def test_value_condition_requires_exact_operator_specific_fields() -> None:
    with pytest.raises(ValueConditionError, match="unexpected right"):
        ValueCondition.from_params(
            {
                "left": _reference("variable", _TEXT_ID),
                "operator": "is_not_empty",
                "right": {"kind": "literal", "type": "text", "value": ""},
                "ignore_case": False,
            }
        )


def test_value_condition_supports_negated_text_comparisons() -> None:
    condition = ValueCondition.from_params(
        {
            "left": _reference("variable", _TEXT_ID),
            "operator": "not_contains",
            "right": {"kind": "literal", "type": "text", "value": "WORLD"},
            "ignore_case": True,
        }
    )

    condition.validate_types(ValueType.TEXT, ValueType.TEXT)

    assert condition.evaluate("Hello", "WORLD")
    assert not condition.evaluate("Hello world", "WORLD")


def test_comparison_enum_uses_friendly_labels_but_emits_stable_values() -> None:
    app = QApplication.instance() or QApplication([])
    changes: list[object] = []
    widget = ParamEditorWidget(
        {
            "name": "operator",
            "type": "enum",
            "options": ["equals", "greater_or_equal"],
            "option_labels": {
                "equals": "Equals",
                "greater_or_equal": "Is greater than or equal to",
            },
        },
        "greater_or_equal",
        lambda _name, value: changes.append(value),
    )
    combo = widget.findChild(QComboBox)

    assert app is not None
    assert combo is not None
    assert combo.currentText() == "Is greater than or equal to"
    combo.setCurrentIndex(combo.findData("equals"))

    assert changes == ["equals"]
