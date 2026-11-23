from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowValueCatalog,
    FlowVariableDefinition,
)
from automadin.models.typed_value import ValueType
from automadin.ui.flow_editor.param_editor import ParamEditorWidget
from automadin.ui.flow_editor.flow_values_dialog import FlowValueEditDialog

_INPUT_ID = "10000000-0000-4000-8000-000000000001"
_INTEGER_ID = "20000000-0000-4000-8000-000000000001"
_TEXT_ID = "20000000-0000-4000-8000-000000000002"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _catalog(*, input_name: str = "factor") -> FlowValueCatalog:
    return FlowValueCatalog.build(
        [FlowInputDefinition(input_name, ValueType.NUMBER, 2.0, id=_INPUT_ID)],
        [
            FlowVariableDefinition("attempts", ValueType.INTEGER, 3, id=_INTEGER_ID),
            FlowVariableDefinition("message", ValueType.TEXT, "", id=_TEXT_ID),
        ],
        [],
    )


def _index_for_data(combo: QComboBox, value: object) -> int:
    return next(
        index for index in range(combo.count()) if combo.itemData(index) == value
    )


def test_variable_target_picker_emits_stable_variable_id() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {"name": "target_variable_id", "type": "variable_target"},
        "",
        lambda name, value: changes.append((name, value)),
        value_catalog=_catalog(),
    )
    picker = widget.findChild(QComboBox, "variableTargetPicker")

    assert picker is not None
    picker.setCurrentIndex(picker.findData(_INTEGER_ID))

    assert changes == [("target_variable_id", _INTEGER_ID)]


def test_variable_target_picker_filters_allowed_types_and_keeps_bad_saved_id() -> None:
    _app()
    widget = ParamEditorWidget(
        {
            "name": "variable",
            "type": "variable_target",
            "allowed_value_types": ["text"],
        },
        _INTEGER_ID,
        lambda _name, _value: None,
        value_catalog=_catalog(),
    )
    picker = widget.findChild(QComboBox, "variableTargetPicker")

    assert picker is not None
    assert picker.currentData() == _INTEGER_ID
    assert picker.currentText().startswith("Incompatible Variable")
    assert picker.findData(_TEXT_ID) >= 0


def test_optional_variable_target_can_clear_a_saved_output() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {"name": "variable", "type": "variable_target", "optional": True},
        _TEXT_ID,
        lambda name, value: changes.append((name, value)),
        value_catalog=_catalog(),
    )
    picker = widget.findChild(QComboBox, "variableTargetPicker")

    assert picker is not None
    picker.setCurrentIndex(picker.findData(""))
    assert changes == [("variable", "")]


def test_output_target_can_create_a_compatible_variable_inline() -> None:
    _app()
    created_id = "30000000-0000-4000-8000-000000000001"
    changes: list[tuple[str, object]] = []
    allowed: list[set[ValueType]] = []
    widget = ParamEditorWidget(
        {
            "name": "variable",
            "type": "variable_target",
            "allowed_value_types": ["text"],
        },
        "",
        lambda name, value: changes.append((name, value)),
        value_catalog=_catalog(),
        create_variable=lambda types: (allowed.append(types), created_id)[1],
    )
    picker = widget.findChild(QComboBox, "variableTargetPicker")

    assert picker is not None
    create_index = next(
        index
        for index in range(picker.count())
        if picker.itemText(index).startswith("Create new Variable")
    )
    picker.setCurrentIndex(create_index)

    assert allowed == [{ValueType.TEXT}]
    assert changes == [("variable", created_id)]


def test_inline_variable_dialog_locks_output_type() -> None:
    _app()
    dialog = FlowValueEditDialog("variable", allowed_types=(ValueType.TEXT,))

    assert dialog._type_combo.count() == 1
    assert not dialog._type_combo.isEnabled()
    dialog.deleteLater()


def test_variable_target_without_catalog_preserves_saved_id_visibly() -> None:
    _app()
    widget = ParamEditorWidget(
        {"name": "target_variable_id", "type": "variable_target"},
        _INTEGER_ID,
        lambda _name, _value: None,
    )
    picker = widget.findChild(QComboBox, "variableTargetPicker")

    assert picker is not None
    assert not picker.isEnabled()
    assert picker.currentData() == _INTEGER_ID
    assert _INTEGER_ID in picker.currentText()


def test_value_reference_picker_round_trips_ids_without_emitting_on_load() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {"name": "source", "type": "value_reference"},
        {"kind": "input", "id": _INPUT_ID},
        lambda name, value: changes.append((name, value)),
        value_catalog=_catalog(input_name="renamed_factor"),
    )
    picker = widget.findChild(QComboBox, "valueReferencePicker")

    assert picker is not None
    assert picker.currentData() == ("input", _INPUT_ID)
    assert "renamed_factor" in picker.currentText()
    assert changes == []

    picker.setCurrentIndex(_index_for_data(picker, ("variable", _TEXT_ID)))
    assert changes == [("source", {"kind": "variable", "id": _TEXT_ID})]


def test_literal_reference_editor_emits_typed_dictionary() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {"name": "source", "type": "value_reference"},
        {"kind": "literal", "type": "duration_ms", "value": 1500},
        lambda name, value: changes.append((name, value)),
        value_catalog=_catalog(),
    )
    picker = widget.findChild(QComboBox, "valueReferencePicker")
    edit = widget.findChild(QLineEdit, "literalValueEditor")

    assert picker is not None
    assert picker.currentData() == ("literal", "duration_ms")
    assert edit is not None
    assert edit.text() == "1500"
    assert changes == []

    edit.setText("2.5 s")
    edit.editingFinished.emit()
    assert changes == [
        (
            "source",
            {"kind": "literal", "type": "duration_ms", "value": 2500},
        )
    ]


def test_boolean_literal_reference_uses_true_false_picker() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {
            "name": "color_match",
            "type": "value_reference",
            "allowed_value_types": ["boolean"],
        },
        {"kind": "literal", "type": "boolean", "value": True},
        lambda name, value: changes.append((name, value)),
        value_catalog=_catalog(),
    )
    boolean_editor = widget.findChild(QComboBox, "literalBooleanEditor")

    assert boolean_editor is not None
    assert boolean_editor.currentData() is True
    assert widget.findChild(QLineEdit, "literalValueEditor") is None
    assert changes == []

    boolean_editor.setCurrentIndex(boolean_editor.findData(False))
    assert changes == [
        (
            "color_match",
            {"kind": "literal", "type": "boolean", "value": False},
        )
    ]


def test_selecting_boolean_literal_initializes_false_without_typing() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {
            "name": "color_match",
            "type": "value_reference",
            "allowed_value_types": ["boolean"],
        },
        None,
        lambda name, value: changes.append((name, value)),
        value_catalog=_catalog(),
    )
    picker = widget.findChild(QComboBox, "valueReferencePicker")
    assert picker is not None

    picker.setCurrentIndex(_index_for_data(picker, ("literal", "boolean")))

    boolean_editor = widget.findChild(QComboBox, "literalBooleanEditor")
    assert boolean_editor is not None
    assert boolean_editor.currentData() is False
    assert changes == [
        (
            "color_match",
            {"kind": "literal", "type": "boolean", "value": False},
        )
    ]


def test_builtin_and_malformed_references_remain_visible_for_repair() -> None:
    _app()
    for saved, expected_label in (
        ({"kind": "builtin", "name": "item"}, "Out-of-scope Built-in"),
        ({"kind": "literal", "type": "integer", "value": "bad"}, "Invalid"),
    ):
        changes: list[tuple[str, object]] = []
        widget = ParamEditorWidget(
            {"name": "source", "type": "value_reference"},
            saved,
            lambda name, value: changes.append((name, value)),
            value_catalog=_catalog(),
        )
        picker = widget.findChild(QComboBox, "valueReferencePicker")

        assert picker is not None
        assert expected_label in picker.currentText()
        assert changes == []


def test_type_filtered_picker_marks_existing_incompatible_reference() -> None:
    _app()
    widget = ParamEditorWidget(
        {
            "name": "source",
            "type": "value_reference",
            "allowed_value_types": ["text"],
        },
        {"kind": "input", "id": _INPUT_ID},
        lambda _name, _value: None,
        value_catalog=_catalog(),
    )
    picker = widget.findChild(QComboBox, "valueReferencePicker")

    assert picker is not None
    assert picker.currentData() == ("input", _INPUT_ID)
    assert picker.currentText().startswith("Incompatible Input")
    assert all(
        picker.itemData(index) != ("literal", "integer")
        for index in range(picker.count())
    )


def test_in_scope_builtin_picker_emits_typed_reference() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {
            "name": "source",
            "type": "value_reference",
            "allowed_value_types": ["integer"],
        },
        None,
        lambda name, value: changes.append((name, value)),
        value_catalog=_catalog(),
        builtin_types={"item": ValueType.TEXT, "index": ValueType.INTEGER},
    )
    picker = widget.findChild(QComboBox, "valueReferencePicker")

    assert picker is not None
    labels = [picker.itemText(index) for index in range(picker.count())]
    assert any("Built-in — index" in label for label in labels)
    assert not any("Built-in — item" in label for label in labels)

    picker.setCurrentIndex(_index_for_data(picker, ("builtin", "index")))
    assert changes == [("source", {"kind": "builtin", "name": "index"})]
