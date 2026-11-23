from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from automadin.execution.handlers.registry import load_command_types
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow, FlowCommand
from automadin.models.flow_value import FlowValueCatalog, FlowVariableDefinition
from automadin.models.typed_value import ValueType
from automadin.ui.flow_editor.command_card import CommandCard
from automadin.ui.flow_editor.variable_update_fields import (
    configure_variable_update_param_defs,
    normalize_variable_update_editor_change,
)

_INTEGER_ID = "20000000-0000-4000-8000-000000000001"
_TEXT_ID = "20000000-0000-4000-8000-000000000002"
_DURATION_ID = "20000000-0000-4000-8000-000000000003"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _catalog() -> FlowValueCatalog:
    return FlowValueCatalog.build(
        [],
        [
            FlowVariableDefinition("count", ValueType.INTEGER, 1, id=_INTEGER_ID),
            FlowVariableDefinition("message", ValueType.TEXT, "", id=_TEXT_ID),
            FlowVariableDefinition(
                "delay", ValueType.DURATION_MS, 100, id=_DURATION_ID
            ),
        ],
        [],
    )


def _update_defs() -> list[dict[str, object]]:
    return load_command_types()["update_variable"]["params"]


def test_target_change_clears_operands_and_repairs_invalid_operation() -> None:
    params = {
        "target_variable_id": _TEXT_ID,
        "operation": "append",
        "source": {"kind": "literal", "type": "text", "value": "!"},
        "find": {"kind": "literal", "type": "text", "value": "x"},
    }

    normalized = normalize_variable_update_editor_change(
        params,
        "target_variable_id",
        _INTEGER_ID,
        _catalog(),
    )

    assert normalized == {
        "target_variable_id": _INTEGER_ID,
        "operation": "set",
    }


def test_operation_change_keeps_only_relevant_operands() -> None:
    params = {
        "target_variable_id": _TEXT_ID,
        "operation": "replace",
        "source": {"kind": "literal", "type": "text", "value": "old"},
        "find": {"kind": "literal", "type": "text", "value": "a"},
        "replacement": {"kind": "literal", "type": "text", "value": "b"},
    }

    toggled = normalize_variable_update_editor_change(
        params, "operation", "toggle", _catalog()
    )
    replaced = normalize_variable_update_editor_change(
        params, "operation", "replace", _catalog()
    )

    assert not {"source", "find", "replacement"}.intersection(toggled)
    assert "source" not in replaced
    assert replaced["find"] == params["find"]
    assert replaced["replacement"] == params["replacement"]


def test_configured_fields_filter_operations_operands_and_source_types() -> None:
    integer_defs = configure_variable_update_param_defs(
        _update_defs(),
        {"target_variable_id": _INTEGER_ID, "operation": "add"},
        _catalog(),
    )
    integer_by_name = {definition["name"]: definition for definition in integer_defs}

    assert "source" in integer_by_name
    assert "find" not in integer_by_name
    assert "append" not in integer_by_name["operation"]["options"]
    assert integer_by_name["source"]["allowed_value_types"] == ["integer"]

    duration_defs = configure_variable_update_param_defs(
        _update_defs(),
        {"target_variable_id": _DURATION_ID, "operation": "multiply"},
        _catalog(),
    )
    duration_by_name = {definition["name"]: definition for definition in duration_defs}
    assert duration_by_name["source"]["allowed_value_types"] == [
        "integer",
        "number",
    ]

    replace_defs = configure_variable_update_param_defs(
        _update_defs(),
        {"target_variable_id": _TEXT_ID, "operation": "replace"},
        _catalog(),
    )
    replace_by_name = {definition["name"]: definition for definition in replace_defs}
    assert "source" not in replace_by_name
    assert replace_by_name["find"]["allowed_value_types"] == ["text"]
    assert replace_by_name["replacement"]["allowed_value_types"] == ["text"]


class _Editor:
    def __init__(self, flow: AutomationFlow) -> None:
        self._flow = flow


def test_command_card_deferred_rebuild_removes_obsolete_operand_editor() -> None:
    _app()
    command = FlowCommand(
        "update_variable",
        params={
            "target_variable_id": _INTEGER_ID,
            "operation": "add",
            "source": {"kind": "literal", "type": "integer", "value": 1},
        },
    )
    flow = AutomationFlow(
        "Editor flow",
        [command],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=list(_catalog().variables),
    )
    card = CommandCard(command, load_command_types(), _Editor(flow))

    assert len(card.findChildren(QComboBox, "valueReferencePicker")) == 1
    card._on_param_changed("operation", "reset")
    _app().processEvents()

    assert "source" not in command.params
    assert card.findChildren(QComboBox, "valueReferencePicker") == []
    card.deleteLater()


def test_nested_command_card_offers_only_type_compatible_builtins() -> None:
    _app()
    command = FlowCommand(
        "update_variable",
        params={"target_variable_id": _INTEGER_ID, "operation": "add"},
    )
    loop = FlowCommand("for_each", children=[command])
    flow = AutomationFlow(
        "Scoped editor flow",
        [loop],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=list(_catalog().variables),
    )
    card = CommandCard(command, load_command_types(), _Editor(flow), depth=1)
    picker = card.findChild(QComboBox, "valueReferencePicker")

    assert picker is not None
    labels = [picker.itemText(index) for index in range(picker.count())]
    assert any("Built-in — index" in label for label in labels)
    assert not any("Built-in — item" in label for label in labels)
    card.deleteLater()
