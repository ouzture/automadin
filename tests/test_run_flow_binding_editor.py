from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QPushButton

from automadin.execution.handlers.registry import load_command_types
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow, FlowCommand
from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowOutputDefinition,
    FlowVariableDefinition,
)
from automadin.models.run_flow_binding import RunFlowBinding
from automadin.models.typed_value import ValueType
from automadin.models.value_reference import InputValueReference
from automadin.services.database import DatabaseService
from automadin.services.flow_preflight import preflight_flow
from automadin.ui.flow_editor.command_card import CommandCard

_PARENT_INPUT_ID = "10000000-0000-4000-8000-000000000001"
_PARENT_INTEGER_ID = "20000000-0000-4000-8000-000000000001"
_PARENT_NUMBER_ID = "20000000-0000-4000-8000-000000000002"
_PARENT_TEXT_ID = "20000000-0000-4000-8000-000000000003"
_CHILD_INPUT_ID = "30000000-0000-4000-8000-000000000001"
_CHILD_VARIABLE_ID = "40000000-0000-4000-8000-000000000001"
_CHILD_OUTPUT_ID = "50000000-0000-4000-8000-000000000001"
_STALE_INPUT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Editor:
    def __init__(self, flow: AutomationFlow) -> None:
        self._flow = flow
        self.changed: list[FlowCommand] = []

    def _on_command_metadata_changed(self, command: FlowCommand) -> None:
        self.changed.append(command)


def _parent(command: FlowCommand) -> AutomationFlow:
    return AutomationFlow(
        "Parent",
        [command],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[
            FlowInputDefinition(
                "attempts",
                ValueType.INTEGER,
                2,
                id=_PARENT_INPUT_ID,
            )
        ],
        variables=[
            FlowVariableDefinition(
                "integer_result",
                ValueType.INTEGER,
                0,
                id=_PARENT_INTEGER_ID,
            ),
            FlowVariableDefinition(
                "number_result",
                ValueType.NUMBER,
                0,
                id=_PARENT_NUMBER_ID,
            ),
            FlowVariableDefinition(
                "text_result",
                ValueType.TEXT,
                "",
                id=_PARENT_TEXT_ID,
            ),
        ],
    )


def _child(name: str = "Child") -> AutomationFlow:
    return AutomationFlow(
        name,
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[
            FlowInputDefinition(
                "amount",
                ValueType.NUMBER,
                id=_CHILD_INPUT_ID,
            )
        ],
        variables=[
            FlowVariableDefinition(
                "result",
                ValueType.INTEGER,
                0,
                id=_CHILD_VARIABLE_ID,
            )
        ],
        outputs=[
            FlowOutputDefinition(
                "result",
                ValueType.INTEGER,
                _CHILD_VARIABLE_ID,
                id=_CHILD_OUTPUT_ID,
            )
        ],
    )


def _database(tmp_path: Path, *flows: AutomationFlow) -> DatabaseService:
    database = DatabaseService(str(tmp_path / "run-flow-editor.db"))
    for flow in flows:
        database.save_flow(flow.name, flow)
    return database


def _labels(combo: QComboBox) -> list[str]:
    return [combo.itemText(index) for index in range(combo.count())]


def _tuple_data_index(combo: QComboBox, expected: tuple[str, str]) -> int:
    return next(
        (
            index
            for index in range(combo.count())
            if tuple(combo.itemData(index) or ()) == expected
        ),
        -1,
    )


def test_v1_run_flow_editor_builds_uuid_mappings_and_passes_preflight(
    tmp_path: Path,
) -> None:
    _app()
    command = FlowCommand("run_flow", params={"pause_ms": 25})
    parent = _parent(command)
    child = _child()
    database = _database(tmp_path, parent, child)
    editor = _Editor(parent)
    card = CommandCard(command, load_command_types(), editor, db=database)

    target = card.findChild(QComboBox, "runFlowTargetPicker")
    assert target is not None
    target.setCurrentIndex(target.findData(child.flow_id))
    assert any(
        issue.code == "MISSING_CHILD_INPUT_MAPPING"
        for issue in preflight_flow(parent, db=database).errors
    )
    source = card.findChild(QComboBox, "valueReferencePicker")
    assert source is not None
    source_index = _tuple_data_index(source, ("input", _PARENT_INPUT_ID))
    assert source_index >= 0, _labels(source)
    source.setCurrentIndex(source_index)
    output = card.findChild(QComboBox, "runFlowOutputTargetPicker")
    assert output is not None
    output.setCurrentIndex(output.findData(_PARENT_NUMBER_ID))

    binding = RunFlowBinding.from_params(
        {name: value for name, value in command.params.items() if name != "pause_ms"}
    )
    assert command.params["pause_ms"] == 25
    assert binding.flow_id == child.flow_id
    assert binding.input_mappings == {
        _CHILD_INPUT_ID: InputValueReference(_PARENT_INPUT_ID)
    }
    assert binding.output_mappings == {_CHILD_OUTPUT_ID: _PARENT_NUMBER_ID}
    assert not preflight_flow(parent, db=database).errors
    assert editor.changed
    card.deleteLater()
    database.close()


def test_v1_run_flow_editor_filters_targets_and_mapping_types(tmp_path: Path) -> None:
    _app()
    command = FlowCommand("run_flow")
    parent = _parent(command)
    child = _child("Typed child")
    legacy = AutomationFlow("Legacy child")
    database = _database(tmp_path, parent, child, legacy)
    card = CommandCard(command, load_command_types(), _Editor(parent), db=database)

    target = card.findChild(QComboBox, "runFlowTargetPicker")
    assert target is not None
    assert target.findData(child.flow_id) >= 0
    assert target.findData(parent.flow_id) < 0
    assert target.findData(legacy.flow_id) < 0
    target.setCurrentIndex(target.findData(child.flow_id))

    source = card.findChild(QComboBox, "valueReferencePicker")
    assert source is not None
    source_labels = _labels(source)
    assert any("Input — attempts" in label for label in source_labels)
    assert not any("text_result" in label for label in source_labels)
    output = card.findChild(QComboBox, "runFlowOutputTargetPicker")
    assert output is not None
    output_labels = _labels(output)
    assert any("integer_result" in label for label in output_labels)
    assert any("number_result" in label for label in output_labels)
    assert not any("text_result" in label for label in output_labels)
    card.deleteLater()
    database.close()


def test_stale_child_mappings_survive_edits_until_confirmed_cleanup(
    tmp_path: Path,
) -> None:
    _app()
    child = _child()
    binding = RunFlowBinding(
        child.flow_id,
        {_STALE_INPUT_ID: InputValueReference(_PARENT_INPUT_ID)},
        {},
    )
    command = FlowCommand("run_flow", params=binding.to_params())
    parent = _parent(command)
    database = _database(tmp_path, parent, child)
    card = CommandCard(command, load_command_types(), _Editor(parent), db=database)

    cleanup = next(
        button
        for button in card.findChildren(QPushButton)
        if button.text() == "Remove stale mappings"
    )
    source = card.findChild(QComboBox, "valueReferencePicker")
    assert source is not None
    source.setCurrentIndex(_tuple_data_index(source, ("input", _PARENT_INPUT_ID)))
    before_cleanup = RunFlowBinding.from_params(command.params).input_mappings
    assert _STALE_INPUT_ID in before_cleanup
    assert _CHILD_INPUT_ID in before_cleanup
    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        cleanup.click()

    after_cleanup = RunFlowBinding.from_params(command.params).input_mappings
    assert _STALE_INPUT_ID not in after_cleanup
    assert _CHILD_INPUT_ID in after_cleanup
    card.deleteLater()
    database.close()


def test_legacy_run_flow_keeps_name_based_editor(tmp_path: Path) -> None:
    _app()
    child = AutomationFlow("Legacy child")
    command = FlowCommand("run_flow", params={"flow_name": child.name})
    parent = AutomationFlow("Legacy parent", [command])
    database = _database(tmp_path, parent, child)
    card = CommandCard(command, load_command_types(), _Editor(parent), db=database)

    assert card.findChild(QComboBox, "runFlowTargetPicker") is None
    assert command.params == {"flow_name": child.name}
    card.deleteLater()
    database.close()
