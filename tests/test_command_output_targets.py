from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

from automadin.execution.context import FlowContext
from automadin.execution.flow_values import FlowInvocationValues
from automadin.execution.handlers.browser_elements import handle_extract_text
from automadin.execution.handlers.browser_ext import handle_evaluate_js
from automadin.execution.handlers.registry import load_command_types
from automadin.models.device import DeviceSession
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.flow_value import FlowVariableDefinition
from automadin.models.typed_value import ValueType
from automadin.services.flow_preflight import preflight_flow
from automadin.ui.flow_editor.command_card import CommandCard

_TEXT_ID = "20000000-0000-4000-8000-000000000002"
_INTEGER_ID = "20000000-0000-4000-8000-000000000003"
_STALE_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _flow(command: FlowCommand) -> AutomationFlow:
    return AutomationFlow(
        "Output targets",
        [command],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=[
            FlowVariableDefinition("message", ValueType.TEXT, "", id=_TEXT_ID),
            FlowVariableDefinition("count", ValueType.INTEGER, 0, id=_INTEGER_ID),
        ],
    )


def _codes(flow: AutomationFlow) -> set[str]:
    return {issue.code for issue in preflight_flow(flow).issues}


def test_v1_output_target_editor_selects_only_text_variables() -> None:
    _app()
    command = FlowCommand(
        "extract_text",
        params={"locator": "#status", "variable": _TEXT_ID},
    )
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    picker = card.findChild(QComboBox, "variableTargetPicker")

    assert picker is not None
    assert picker.currentData() == _TEXT_ID
    assert picker.findData(_INTEGER_ID) == -1
    card.deleteLater()


def test_command_card_wires_inline_variable_creation_to_output_target() -> None:
    _app()
    created_id = "30000000-0000-4000-8000-000000000001"
    command = FlowCommand(
        "extract_text",
        params={"locator": "#status", "variable": ""},
    )
    flow = _flow(command)
    editor = SimpleNamespace(
        _flow=flow,
        _create_flow_variable_for_types=lambda _types: created_id,
    )
    card = CommandCard(command, load_command_types(), editor)
    picker = card.findChild(QComboBox, "variableTargetPicker")

    assert picker is not None
    create_index = next(
        index
        for index in range(picker.count())
        if picker.itemText(index).startswith("Create new Variable")
    )
    picker.setCurrentIndex(create_index)

    assert command.params["variable"] == created_id
    card.deleteLater()


def test_extract_text_target_switches_between_page_and_current_element_fields() -> None:
    _app()
    command = FlowCommand(
        "extract_text",
        params={
            "target": "current_element",
            "locator": {"legacy": "stale"},
            "child_selector": "p.description",
            "variable": _TEXT_ID,
        },
    )
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))

    assert card.findChild(QLineEdit, "locatorValue") is None
    assert card.findChild(QLineEdit, "valueTemplateTextEditor") is not None
    assert all("Element Locator:" not in part for part in card._summary_parts())

    card._on_param_changed("target", "locator")
    card._rebuild_params()
    assert "child_selector" not in command.params
    assert card.findChild(QLineEdit, "locatorValue") is not None

    card._on_param_changed("target", "current_element")
    card._rebuild_params()
    assert "locator" not in command.params
    assert card.findChild(QLineEdit, "locatorValue") is None
    card.deleteLater()


def test_v1_output_target_preflight_rejects_stale_and_wrong_type_ids() -> None:
    stale = _flow(
        FlowCommand(
            "extract_text",
            params={"locator": "#status", "variable": _STALE_ID},
        )
    )
    wrong_type = _flow(
        FlowCommand(
            "evaluate_js",
            params={"script": "document.title", "variable": _INTEGER_ID},
        )
    )
    valid = _flow(
        FlowCommand(
            "evaluate_js",
            params={"script": "document.title", "variable": _TEXT_ID},
        )
    )

    assert "STALE_OUTPUT_VARIABLE" in _codes(stale)
    assert "OUTPUT_VARIABLE_TYPE_MISMATCH" in _codes(wrong_type)
    assert "STALE_OUTPUT_VARIABLE" not in _codes(valid)
    assert "OUTPUT_VARIABLE_TYPE_MISMATCH" not in _codes(valid)


def test_value_producing_handlers_write_stable_v1_targets_and_keep_v0_names() -> None:
    writes: list[tuple[str, str]] = []
    v1 = SimpleNamespace(
        flow_values=object(),
        session=SimpleNamespace(
            data={
                "browser_data": SimpleNamespace(
                    evaluate_js=lambda _script: "Automadin"
                )
            }
        ),
        listener=None,
        interpolate=lambda value: value,
        set_flow_variable=lambda target, value: writes.append((target, value)),
        set_output_variable=lambda target, value: writes.append((target, value)),
        set_variable=lambda _name, _value: None,
    )
    handle_evaluate_js(
        FlowCommand(
            "evaluate_js",
            params={"script": "document.title", "variable": _TEXT_ID},
        ),
        v1,
    )
    assert writes == [(_TEXT_ID, "Automadin")]

    legacy: dict[str, str] = {}
    v0 = SimpleNamespace(
        flow_values=None,
        session=SimpleNamespace(
            data={
                "browser_data": SimpleNamespace(
                    extract_locator_text=lambda _locator: "Saved"
                )
            }
        ),
        listener=None,
        interpolate=lambda value: value,
        set_flow_variable=lambda _target, _value: None,
        set_output_variable=lambda target, value: legacy.__setitem__(target, value),
        set_variable=lambda name, value: legacy.__setitem__(name, value),
    )
    handle_extract_text(
        FlowCommand(
            "extract_text",
            params={"locator": "#status", "variable": "status"},
        ),
        v0,
    )
    assert legacy == {"status": "Saved"}


def test_output_logs_resolve_variable_names_and_hide_values_by_default() -> None:
    command = FlowCommand(
        "extract_text",
        params={"locator": "#status", "variable": _TEXT_ID},
    )
    flow = _flow(command)
    logs: list[str] = []
    context = FlowContext(
        DeviceSession(
            data={
                "browser_data": SimpleNamespace(
                    extract_locator_text=lambda _locator: "Secret status"
                )
            }
        ),
        flow_values=FlowInvocationValues(flow.flow_id, flow.value_catalog, {}),
        listener=SimpleNamespace(on_log=logs.append),
    )

    handle_extract_text(command, context)

    assert logs == [
        "Extracted browser text into Variable 'message'; value hidden"
    ]
    assert _TEXT_ID not in logs[0]
    assert "Secret status" not in logs[0]

    command.params["log_values"] = True
    logs.clear()
    handle_extract_text(command, context)

    assert logs == [
        'Extracted browser text into Variable \'message\' = "Secret status"'
    ]
