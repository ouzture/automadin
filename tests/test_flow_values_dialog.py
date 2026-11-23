from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from automadin.engine.session import DeviceSessionService
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    FlowCommand,
)
from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowOutputDefinition,
    FlowVariableDefinition,
)
from automadin.models.typed_value import ValueType
from automadin.services.database import DatabaseService
from automadin.ui.flow_editor.editor_view import FlowEditorView
from automadin.ui.flow_editor.flow_values_dialog import (
    FlowOutputEditDialog,
    FlowValueEditDialog,
    FlowValuesDialog,
    count_flow_value_references,
)

_INPUT_ID = "10000000-0000-4000-8000-000000000001"
_VARIABLE_ID = "20000000-0000-4000-8000-000000000001"
_OUTPUT_ID = "30000000-0000-4000-8000-000000000001"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_input_editor_preserves_id_and_required_default_omission() -> None:
    _app()
    existing = FlowInputDefinition(
        "account",
        ValueType.TEXT,
        "personal",
        id=_INPUT_ID,
    )
    dialog = FlowValueEditDialog("input", existing)
    assert not dialog._type_combo.isEnabled()
    dialog._name_edit.setText("renamed_account")
    assert dialog._required_check is not None
    dialog._required_check.setChecked(True)

    dialog._validate_and_accept()
    updated = dialog.definition()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert isinstance(updated, FlowInputDefinition)
    assert updated.id == _INPUT_ID
    assert updated.required
    assert "default" not in updated.to_dict()
    dialog.deleteLater()


def test_optional_false_and_duration_values_are_typed() -> None:
    _app()
    boolean_dialog = FlowValueEditDialog("input")
    boolean_dialog._name_edit.setText("dry_run")
    boolean_dialog._type_combo.setCurrentIndex(
        boolean_dialog._type_combo.findData(ValueType.BOOLEAN)
    )
    boolean_dialog._value_edit.setText("false")
    boolean_dialog._validate_and_accept()
    boolean = boolean_dialog.definition()

    duration_dialog = FlowValueEditDialog("variable")
    duration_dialog._name_edit.setText("delay")
    duration_dialog._type_combo.setCurrentIndex(
        duration_dialog._type_combo.findData(ValueType.DURATION_MS)
    )
    duration_dialog._value_edit.setText("1.5 s")
    duration_dialog._validate_and_accept()
    duration = duration_dialog.definition()

    assert isinstance(boolean, FlowInputDefinition)
    assert boolean.default is False
    assert isinstance(duration, FlowVariableDefinition)
    assert duration.initial == 1500
    boolean_dialog.deleteLater()
    duration_dialog.deleteLater()


def test_invalid_variable_initial_value_does_not_accept_dialog() -> None:
    _app()
    dialog = FlowValueEditDialog("variable")
    dialog._name_edit.setText("attempts")
    dialog._type_combo.setCurrentIndex(
        dialog._type_combo.findData(ValueType.INTEGER)
    )
    dialog._value_edit.clear()

    with patch.object(QMessageBox, "warning") as warning:
        dialog._validate_and_accept()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert "requires an integer" in warning.call_args.args[2]
    dialog.deleteLater()


def test_manager_rejects_casefold_duplicate_shared_names() -> None:
    _app()
    dialog = FlowValuesDialog(
        [FlowInputDefinition("Account", ValueType.TEXT, "", id=_INPUT_ID)],
        [],
        [],
        [],
    )
    duplicate = FlowVariableDefinition(
        "account",
        ValueType.TEXT,
        "",
        id=_VARIABLE_ID,
    )

    with patch.object(QMessageBox, "warning") as warning:
        accepted = dialog._commit_candidate("variable", [duplicate])

    assert not accepted
    assert dialog.variables() == []
    assert "declared more than once" in warning.call_args.args[2]
    dialog.deleteLater()


def test_delete_confirmation_leaves_command_reference_stale_and_auditable() -> None:
    _app()
    command = FlowCommand(
        "update_variable",
        params={"target_variable_id": _VARIABLE_ID, "operation": "reset"},
    )
    variable = FlowVariableDefinition(
        "count", ValueType.INTEGER, 0, id=_VARIABLE_ID
    )
    dialog = FlowValuesDialog([], [variable], [], [command])
    dialog._variable_list.setCurrentRow(0)

    assert count_flow_value_references([command], _VARIABLE_ID) == 1
    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.Yes,
    ) as question:
        dialog._remove("variable")

    assert dialog.variables() == []
    assert command.params["target_variable_id"] == _VARIABLE_ID
    assert "will become stale" in question.call_args.args[2]
    dialog.deleteLater()


def test_cancelled_delete_leaves_declaration_unchanged() -> None:
    _app()
    value = FlowInputDefinition("account", ValueType.TEXT, "", id=_INPUT_ID)
    dialog = FlowValuesDialog([value], [], [], [])
    dialog._input_list.setCurrentRow(0)

    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.No,
    ):
        dialog._remove("input")

    assert dialog.inputs() == [value]
    dialog.deleteLater()


def test_output_source_blocks_variable_delete_and_outputs_round_trip() -> None:
    _app()
    variable = FlowVariableDefinition(
        "result", ValueType.TEXT, "", id=_VARIABLE_ID
    )
    output = FlowOutputDefinition(
        "result",
        ValueType.TEXT,
        _VARIABLE_ID,
        id=_OUTPUT_ID,
    )
    dialog = FlowValuesDialog([], [variable], [output], [])
    dialog._variable_list.setCurrentRow(0)

    with patch.object(QMessageBox, "warning") as warning:
        dialog._remove("variable")

    assert dialog.variables() == [variable]
    assert dialog.outputs() == [output]
    assert warning.call_args.args[1] == "Variable Is an Output Source"
    dialog.deleteLater()


def test_output_edit_preserves_id_and_contract_type_on_same_type_retarget() -> None:
    _app()
    first = FlowVariableDefinition("first", ValueType.TEXT, "", id=_VARIABLE_ID)
    second = FlowVariableDefinition("second", ValueType.TEXT, "")
    integer = FlowVariableDefinition("count", ValueType.INTEGER, 0)
    existing = FlowOutputDefinition(
        "result", ValueType.TEXT, first.id, id=_OUTPUT_ID
    )
    dialog = FlowOutputEditDialog([first, second, integer], existing)
    dialog._name_edit.setText("renamed_result")
    dialog._source_combo.setCurrentIndex(
        dialog._source_combo.findData(second.id)
    )
    dialog._validate_and_accept()
    updated = dialog.definition()

    assert updated.id == _OUTPUT_ID
    assert updated.name == "renamed_result"
    assert updated.type is ValueType.TEXT
    assert updated.source_variable_id == second.id
    assert dialog._source_combo.findData(integer.id) == -1
    dialog.deleteLater()


def test_output_names_are_casefold_unique_in_separate_namespace() -> None:
    _app()
    variable = FlowVariableDefinition(
        "result", ValueType.TEXT, "", id=_VARIABLE_ID
    )
    first = FlowOutputDefinition("Result", ValueType.TEXT, variable.id)
    duplicate = FlowOutputDefinition("result", ValueType.TEXT, variable.id)
    dialog = FlowValuesDialog([], [variable], [first], [])

    with patch.object(QMessageBox, "warning") as warning:
        accepted = dialog._commit_outputs([first, duplicate])

    assert not accepted
    assert dialog.outputs() == [first]
    assert "declared more than once" in warning.call_args.args[2]
    dialog.deleteLater()


def test_output_retarget_requires_confirmation_and_keeps_stable_id() -> None:
    _app()
    first = FlowVariableDefinition("first", ValueType.TEXT, "", id=_VARIABLE_ID)
    second = FlowVariableDefinition("second", ValueType.TEXT, "")
    output = FlowOutputDefinition("result", ValueType.TEXT, first.id, id=_OUTPUT_ID)
    replacement = FlowOutputDefinition(
        "result", ValueType.TEXT, second.id, id=_OUTPUT_ID
    )
    dialog = FlowValuesDialog([], [first, second], [output], [])
    dialog._output_list.setCurrentRow(0)

    class _AcceptedDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def exec() -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        @staticmethod
        def definition() -> FlowOutputDefinition:
            return replacement

    with (
        patch(
            "automadin.ui.flow_editor.flow_values_dialog.FlowOutputEditDialog",
            _AcceptedDialog,
        ),
        patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ),
    ):
        dialog._edit_output()

    assert dialog.outputs()[0].id == _OUTPUT_ID
    assert dialog.outputs()[0].source_variable_id == second.id
    dialog.deleteLater()


def test_output_delete_warns_and_preserves_stale_mapping_for_repair() -> None:
    _app()
    variable = FlowVariableDefinition("result", ValueType.TEXT, "", id=_VARIABLE_ID)
    output = FlowOutputDefinition(
        "result", ValueType.TEXT, variable.id, id=_OUTPUT_ID
    )
    command = FlowCommand(
        "run_flow",
        params={"output_mappings": {_OUTPUT_ID: _VARIABLE_ID}},
    )
    dialog = FlowValuesDialog([], [variable], [output], [command])
    dialog._output_list.setCurrentRow(0)

    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.Yes,
    ) as question:
        dialog._remove_output()

    assert dialog.outputs() == []
    assert _OUTPUT_ID in command.params["output_mappings"]
    assert "1 command reference" in question.call_args.args[2]
    assert "Run Flow callers" in question.call_args.args[2]
    dialog.deleteLater()


def test_cancelled_output_delete_keeps_contract() -> None:
    _app()
    variable = FlowVariableDefinition("result", ValueType.TEXT, "", id=_VARIABLE_ID)
    output = FlowOutputDefinition(
        "result", ValueType.TEXT, variable.id, id=_OUTPUT_ID
    )
    dialog = FlowValuesDialog([], [variable], [output], [])
    dialog._output_list.setCurrentRow(0)

    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.No,
    ):
        dialog._remove_output()

    assert dialog.outputs() == [output]
    dialog.deleteLater()


def test_new_editor_flow_routes_to_inputs_and_variables_dialog(tmp_path: Path) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "flow-values-editor.db"))
    editor = FlowEditorView(database, DeviceSessionService())
    declared_input = FlowInputDefinition(
        "account", ValueType.TEXT, "personal", id=_INPUT_ID
    )

    class _AcceptedDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def exec() -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        @staticmethod
        def inputs() -> list[FlowInputDefinition]:
            return [declared_input]

        @staticmethod
        def variables() -> list[FlowVariableDefinition]:
            return []

        @staticmethod
        def outputs() -> list[FlowOutputDefinition]:
            return []

    assert editor._flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
    assert editor._flow_parameters_btn.text() == "Inputs & Variables"
    with patch(
        "automadin.ui.flow_editor.editor_view.FlowValuesDialog",
        _AcceptedDialog,
    ):
        editor._edit_flow_parameters()

    assert editor._flow.inputs == [declared_input]
    assert editor.has_unsaved_changes
    editor.deleteLater()
    database.close()
