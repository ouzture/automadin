from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from automadin.execution.context import FlowContext
from automadin.execution.flow_values import FlowInvocationValues, resolve_flow_inputs
from automadin.execution.handlers.control import handle_section
from automadin.execution.handlers.registry import load_command_types
from automadin.models.device import DeviceSession
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow, FlowCommand
from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowValueCatalog,
    FlowVariableDefinition,
)
from automadin.models.typed_value import ValueType
from automadin.models.value_reference import (
    BuiltinValueReference,
    FlowValueTemplate,
    InputValueReference,
    TemplateTextSegment,
)
from automadin.services.flow_preflight import preflight_flow
from automadin.ui.flow_editor.command_card import CommandCard
from automadin.ui.flow_editor.value_template_editor import (
    parse_value_template_editor_text,
)

_INPUT_ID = "10000000-0000-4000-8000-000000000001"
_VARIABLE_ID = "20000000-0000-4000-8000-000000000001"
_STALE_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _catalog() -> FlowValueCatalog:
    return FlowValueCatalog.build(
        [FlowInputDefinition("account", ValueType.TEXT, "personal", id=_INPUT_ID)],
        [
            FlowVariableDefinition(
                "attempts",
                ValueType.INTEGER,
                1,
                id=_VARIABLE_ID,
            )
        ],
        [],
    )


def _flow(command: FlowCommand) -> AutomationFlow:
    catalog = _catalog()
    return AutomationFlow(
        "Template flow",
        [command],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=list(catalog.inputs),
        variables=list(catalog.variables),
    )


def test_editor_text_parser_persists_stable_ids_and_scoped_builtins() -> None:
    template = parse_value_template_editor_text(
        "Account {{account}}, attempt {{attempts}}, item {{item}}",
        _catalog(),
        {"item": ValueType.TEXT},
    )

    assert template == FlowValueTemplate(
        (
            TemplateTextSegment("Account "),
            InputValueReference(_INPUT_ID),
            TemplateTextSegment(", attempt "),
            # The type is resolved by the declaration, while persistence uses its ID.
            {"kind": "variable", "id": _VARIABLE_ID},
            TemplateTextSegment(", item "),
            BuiltinValueReference("item"),
        )
    )


def test_v1_command_card_inserts_structured_template_references() -> None:
    _app()
    command = FlowCommand("section", params={"text": "Hello "})
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    picker = card.findChild(QComboBox, "insertValuePicker")

    assert picker is not None
    picker.setCurrentIndex(
        next(
            index
            for index in range(picker.count())
            if tuple(picker.itemData(index) or ()) == ("input", "account")
        )
    )
    template = FlowValueTemplate.from_dict(command.params["text"])
    assert template.segments == (
        TemplateTextSegment("Hello "),
        InputValueReference(_INPUT_ID),
    )
    assert any("Hello {{account}}" in part for part in card._summary_parts())
    card.deleteLater()


def test_qualified_editor_tokens_disambiguate_input_and_builtin_names() -> None:
    input_definition = FlowInputDefinition("item", ValueType.TEXT, "input")
    catalog = FlowValueCatalog.build([input_definition], [], [])

    template = parse_value_template_editor_text(
        "{{input:item}}/{{builtin:item}}",
        catalog,
        {"item": ValueType.TEXT},
    )

    assert template.segments == (
        InputValueReference(input_definition.id),
        TemplateTextSegment("/"),
        BuiltinValueReference("item"),
    )


def test_preflight_validates_template_references_and_builtin_scope() -> None:
    valid = _flow(
        FlowCommand(
            "section",
            params={
                "text": FlowValueTemplate(
                    (InputValueReference(_INPUT_ID),)
                ).to_dict()
            },
        )
    )
    stale = _flow(
        FlowCommand(
            "section",
            params={
                "text": FlowValueTemplate(
                    (InputValueReference(_STALE_ID),)
                ).to_dict()
            },
        )
    )
    scoped_command = FlowCommand(
        "section",
        params={
            "text": FlowValueTemplate(
                (BuiltinValueReference("item"),)
            ).to_dict()
        },
    )
    inside = _flow(
        FlowCommand(
            "for_each",
            params={"values": ["one"]},
            children=[scoped_command],
        )
    )
    outside = _flow(scoped_command)

    assert not preflight_flow(valid).errors
    assert any(
        issue.code == "INVALID_VALUE_TEMPLATE"
        for issue in preflight_flow(stale).errors
    )
    assert not preflight_flow(inside).errors
    assert any(
        issue.code == "INVALID_VALUE_TEMPLATE"
        for issue in preflight_flow(outside).errors
    )


def test_preflight_accepts_dynamic_url_but_rejects_non_dynamic_output_path() -> None:
    template = FlowValueTemplate((InputValueReference(_INPUT_ID),)).to_dict()
    navigation = _flow(FlowCommand("navigate_to", params={"url": template}))
    screenshot = _flow(
        FlowCommand(
            "screenshot",
            params={"save_to_disk": True, "save_path": template},
        )
    )

    assert not preflight_flow(navigation).errors
    assert any(
        issue.code == "INVALID_PARAMETER_TYPE"
        for issue in preflight_flow(screenshot).errors
    )


def test_section_handler_renders_v1_template_at_runtime() -> None:
    command = FlowCommand(
        "section",
        params={
            "text": FlowValueTemplate(
                (
                    TemplateTextSegment("Account "),
                    InputValueReference(_INPUT_ID),
                )
            ).to_dict()
        },
    )
    flow = _flow(command)
    messages: list[str] = []
    context = FlowContext(
        DeviceSession(),
        SimpleNamespace(on_log=messages.append),
        flow_values=FlowInvocationValues(
            flow.flow_id,
            flow.value_catalog,
            resolve_flow_inputs(flow.inputs),
        ),
    )

    handle_section(command, context)

    assert messages == ["Section — Account personal"]


def test_legacy_command_card_keeps_legacy_insert_control() -> None:
    _app()
    command = FlowCommand("section", params={"text": "Legacy"})
    flow = AutomationFlow("Legacy", [command])
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))

    assert card.findChild(QComboBox, "insertValuePicker") is None
    card.deleteLater()
