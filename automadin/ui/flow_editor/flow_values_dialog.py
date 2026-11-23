"""Version-1 Input, Variable, and Output declaration management dialogs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from automadin.models.flow import FlowCommand
from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowOutputDefinition,
    FlowValueCatalog,
    FlowValueDeclarationError,
    FlowVariableDefinition,
)
from automadin.models.typed_value import ValueType, canonical_typed_value_text

DeclarationKind = Literal["input", "variable"]
Declaration = FlowInputDefinition | FlowVariableDefinition

_TYPE_LABELS = {
    ValueType.TEXT: "Text",
    ValueType.INTEGER: "Integer",
    ValueType.NUMBER: "Number",
    ValueType.BOOLEAN: "Boolean",
    ValueType.DURATION_MS: "Duration",
}
_TYPE_DEFAULTS: dict[ValueType, object] = {
    ValueType.TEXT: "",
    ValueType.INTEGER: 0,
    ValueType.NUMBER: 0.0,
    ValueType.BOOLEAN: False,
    ValueType.DURATION_MS: 0,
}


def count_flow_value_references(
    commands: Sequence[FlowCommand],
    declaration_id: str,
) -> int:
    """Count exact stable-ID occurrences in command parameters."""
    def _count(value: object) -> int:
        if value == declaration_id:
            return 1
        if isinstance(value, Mapping):
            return sum(_count(key) + _count(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return sum(_count(item) for item in value)
        return 0

    return sum(
        _count(command.params)
        + count_flow_value_references(command.children, declaration_id)
        + count_flow_value_references(command.else_children, declaration_id)
        for command in commands
    )


class FlowValueEditDialog(QDialog):
    def __init__(
        self,
        kind: DeclarationKind,
        existing: Declaration | None = None,
        *,
        allowed_types: Sequence[ValueType] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._existing = existing
        self._definition: Declaration | None = None
        self._allowed_types = tuple(allowed_types) if allowed_types is not None else tuple(ValueType)
        if not self._allowed_types:
            raise ValueError("At least one Variable type must be allowed.")
        noun = "Input" if kind == "input" else "Variable"
        self.setWindowTitle(f"{'Edit' if existing else 'Add'} {noun}")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. retry_count")
        form.addRow("Name", self._name_edit)

        self._type_combo = QComboBox()
        for value_type in self._allowed_types:
            self._type_combo.addItem(_TYPE_LABELS[value_type], value_type)
        form.addRow("Type", self._type_combo)

        self._required_check: QCheckBox | None = None
        if kind == "input":
            self._required_check = QCheckBox("Required — value must be supplied")
            form.addRow("", self._required_check)

        self._value_edit = QLineEdit()
        self._value_edit.setPlaceholderText(
            "300 ms, 1.5 s, and 2 min are accepted for durations"
        )
        form.addRow("Default" if kind == "input" else "Initial value", self._value_edit)

        self._description_edit = QLineEdit()
        self._description_edit.setPlaceholderText("Optional guidance")
        form.addRow("Description", self._description_edit)
        layout.addLayout(form)

        hint = QLabel(
            "Names start with a letter and use only letters, digits, and underscores. "
            "Input and Variable names share one case-insensitive namespace."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if existing is not None:
            self._name_edit.setText(existing.name)
            self._type_combo.setCurrentIndex(
                self._type_combo.findData(existing.type)
            )
            value = (
                existing.default
                if isinstance(existing, FlowInputDefinition) and not existing.required
                else existing.initial
                if isinstance(existing, FlowVariableDefinition)
                else _TYPE_DEFAULTS[existing.type]
            )
            self._value_edit.setText(canonical_typed_value_text(value))
            self._description_edit.setText(existing.description)
            if self._required_check is not None:
                self._required_check.setChecked(
                    isinstance(existing, FlowInputDefinition) and existing.required
                )
            self._type_combo.setEnabled(False)
            self._type_combo.setToolTip(
                "Types are part of the stable flow contract. Remove and recreate "
                "the declaration to change its type."
            )
        else:
            self._value_edit.setText("")
            if len(self._allowed_types) == 1:
                self._type_combo.setEnabled(False)
                self._type_combo.setToolTip(
                    "This output only supports the compatible Variable type."
                )

        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        if self._required_check is not None:
            self._required_check.toggled.connect(self._on_required_toggled)
            self._on_required_toggled(self._required_check.isChecked())

    def _on_type_changed(self, _index: int) -> None:
        value_type = self._type_combo.currentData()
        self._value_edit.setText(
            canonical_typed_value_text(_TYPE_DEFAULTS[value_type])
        )

    def _on_required_toggled(self, required: bool) -> None:
        self._value_edit.setEnabled(not required)

    def _validate_and_accept(self) -> None:
        name = self._name_edit.text().strip()
        value_type = self._type_combo.currentData()
        description = self._description_edit.text().strip()
        stable_id = self._existing.id if self._existing is not None else None
        try:
            if self._kind == "input":
                kwargs = {
                    "name": name,
                    "type": value_type,
                    "description": description,
                }
                if stable_id is not None:
                    kwargs["id"] = stable_id
                if self._required_check is None or not self._required_check.isChecked():
                    kwargs["default"] = self._value_edit.text()
                self._definition = FlowInputDefinition(**kwargs)  # type: ignore[arg-type]
            else:
                self._definition = FlowVariableDefinition(
                    name,
                    value_type,
                    self._value_edit.text(),
                    description,
                    **({"id": stable_id} if stable_id is not None else {}),
                )
        except FlowValueDeclarationError as exc:
            QMessageBox.warning(self, f"Invalid {self._kind.title()}", str(exc))
            return
        self.accept()

    def definition(self) -> Declaration:
        if self._definition is None:
            raise RuntimeError("The flow value dialog was not accepted.")
        return self._definition


class FlowOutputEditDialog(QDialog):
    def __init__(
        self,
        variables: Sequence[FlowVariableDefinition],
        existing: FlowOutputDefinition | None = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._variables = list(variables)
        self._existing = existing
        self._definition: FlowOutputDefinition | None = None
        self.setWindowTitle(f"{'Edit' if existing else 'Add'} Output")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. result")
        form.addRow("Name", self._name_edit)

        self._source_combo = QComboBox()
        self._source_combo.addItem("Select Variable", "")
        eligible = (
            [variable for variable in self._variables if variable.type is existing.type]
            if existing is not None
            else self._variables
        )
        for variable in eligible:
            self._source_combo.addItem(
                f"{variable.name} ({_TYPE_LABELS[variable.type]})",
                variable.id,
            )
        form.addRow("Source Variable", self._source_combo)

        self._type_label = QLabel("Select a source Variable")
        form.addRow("Output type", self._type_label)

        self._description_edit = QLineEdit()
        self._description_edit.setPlaceholderText("Optional contract guidance")
        form.addRow("Description", self._description_edit)
        layout.addLayout(form)

        hint = QLabel(
            "The Output type is derived from its source Variable. Existing Outputs "
            "can only be retargeted to a Variable of the same contract type."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if existing is not None:
            self._name_edit.setText(existing.name)
            self._source_combo.setCurrentIndex(
                self._source_combo.findData(existing.source_variable_id)
            )
            self._description_edit.setText(existing.description)
        self._source_combo.currentIndexChanged.connect(self._refresh_type_label)
        self._refresh_type_label()

    def _selected_variable(self) -> FlowVariableDefinition | None:
        selected_id = self._source_combo.currentData()
        return next(
            (variable for variable in self._variables if variable.id == selected_id),
            None,
        )

    def _refresh_type_label(self, _index: int = -1) -> None:
        variable = self._selected_variable()
        self._type_label.setText(
            _TYPE_LABELS[variable.type]
            if variable is not None
            else "Select a source Variable"
        )

    def _validate_and_accept(self) -> None:
        source = self._selected_variable()
        if source is None:
            QMessageBox.warning(
                self,
                "Invalid Output",
                "Select an existing source Variable.",
            )
            return
        stable_id = self._existing.id if self._existing is not None else None
        try:
            self._definition = FlowOutputDefinition(
                self._name_edit.text().strip(),
                source.type,
                source.id,
                self._description_edit.text().strip(),
                **({"id": stable_id} if stable_id is not None else {}),
            )
        except FlowValueDeclarationError as exc:
            QMessageBox.warning(self, "Invalid Output", str(exc))
            return
        self.accept()

    def definition(self) -> FlowOutputDefinition:
        if self._definition is None:
            raise RuntimeError("The Output dialog was not accepted.")
        return self._definition


class FlowValuesDialog(QDialog):
    def __init__(
        self,
        inputs: Sequence[FlowInputDefinition],
        variables: Sequence[FlowVariableDefinition],
        outputs: Sequence[FlowOutputDefinition],
        commands: Sequence[FlowCommand],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._inputs = list(inputs)
        self._variables = list(variables)
        self._outputs = list(outputs)
        self._commands = list(commands)
        self.setWindowTitle("Inputs, Variables & Outputs")
        self.setMinimumSize(720, 470)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        intro = QLabel(
            "Inputs are supplied when a flow starts. Variables are private mutable "
            "values reset for each new run. Saved references use stable IDs."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._tabs = QTabWidget()
        self._input_list, self._input_actions = self._build_declaration_tab("input")
        self._variable_list, self._variable_actions = self._build_declaration_tab(
            "variable"
        )
        self._output_list, self._output_actions = self._build_output_tab()
        layout.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh("input")
        self._refresh("variable")
        self._refresh_outputs()

    def _build_declaration_tab(
        self,
        kind: DeclarationKind,
    ) -> tuple[QListWidget, dict[str, QPushButton]]:
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 10, 10, 10)
        values_list = QListWidget()
        values_list.setObjectName(f"{kind}Declarations")
        values_list.itemDoubleClicked.connect(lambda *_args: self._edit(kind))
        panel_layout.addWidget(values_list, 1)

        actions_layout = QHBoxLayout()
        actions: dict[str, QPushButton] = {}
        for action_name, label, callback in (
            ("add", "Add", lambda: self._add(kind)),
            ("edit", "Edit", lambda: self._edit(kind)),
            ("remove", "Remove", lambda: self._remove(kind)),
            ("up", "Move up", lambda: self._move(kind, -1)),
            ("down", "Move down", lambda: self._move(kind, 1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions[action_name] = button
            actions_layout.addWidget(button)
            if action_name == "remove":
                actions_layout.addStretch()
        panel_layout.addLayout(actions_layout)
        values_list.currentRowChanged.connect(lambda _row: self._update_actions(kind))
        self._tabs.addTab(panel, "Inputs" if kind == "input" else "Variables")
        return values_list, actions

    def _build_output_tab(self) -> tuple[QListWidget, dict[str, QPushButton]]:
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 10, 10, 10)
        values_list = QListWidget()
        values_list.setObjectName("outputDeclarations")
        values_list.itemDoubleClicked.connect(lambda *_args: self._edit_output())
        panel_layout.addWidget(values_list, 1)

        actions_layout = QHBoxLayout()
        actions: dict[str, QPushButton] = {}
        for action_name, label, callback in (
            ("add", "Add", self._add_output),
            ("edit", "Edit", self._edit_output),
            ("remove", "Remove", self._remove_output),
            ("up", "Move up", lambda: self._move_output(-1)),
            ("down", "Move down", lambda: self._move_output(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions[action_name] = button
            actions_layout.addWidget(button)
            if action_name == "remove":
                actions_layout.addStretch()
        panel_layout.addLayout(actions_layout)
        values_list.currentRowChanged.connect(
            lambda _row: self._update_output_actions()
        )
        self._tabs.addTab(panel, "Outputs")
        return values_list, actions

    def _declarations(self, kind: DeclarationKind) -> list[Declaration]:
        return self._inputs if kind == "input" else self._variables

    def _list(self, kind: DeclarationKind) -> QListWidget:
        return self._input_list if kind == "input" else self._variable_list

    def _actions(self, kind: DeclarationKind) -> dict[str, QPushButton]:
        return self._input_actions if kind == "input" else self._variable_actions

    def _refresh(self, kind: DeclarationKind, selected_row: int | None = None) -> None:
        values_list = self._list(kind)
        declarations = self._declarations(kind)
        values_list.clear()
        for definition in declarations:
            if isinstance(definition, FlowInputDefinition):
                value_text = (
                    "required"
                    if definition.required
                    else f"default {canonical_typed_value_text(definition.default)}"
                )
            else:
                value_text = f"initial {canonical_typed_value_text(definition.initial)}"
            text = f"{definition.name}  ·  {_TYPE_LABELS[definition.type]}  ·  {value_text}"
            if definition.description:
                text += f"  —  {definition.description}"
            values_list.addItem(text)
        if declarations:
            row = 0 if selected_row is None else min(max(0, selected_row), len(declarations) - 1)
            values_list.setCurrentRow(row)
        self._update_actions(kind)

    def _update_actions(self, kind: DeclarationKind) -> None:
        row = self._list(kind).currentRow()
        count = len(self._declarations(kind))
        actions = self._actions(kind)
        actions["edit"].setEnabled(row >= 0)
        actions["remove"].setEnabled(row >= 0)
        actions["up"].setEnabled(row > 0)
        actions["down"].setEnabled(0 <= row < count - 1)

    def _add(self, kind: DeclarationKind) -> None:
        dialog = FlowValueEditDialog(kind, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        declarations = self._declarations(kind)
        if self._commit_candidate(kind, [*declarations, dialog.definition()]):
            self._refresh(kind, len(declarations))

    def _edit(self, kind: DeclarationKind) -> None:
        row = self._list(kind).currentRow()
        declarations = self._declarations(kind)
        if row < 0:
            return
        dialog = FlowValueEditDialog(kind, declarations[row], parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = dialog.definition()
        candidate = list(declarations)
        candidate[row] = replacement
        if self._commit_candidate(kind, candidate):
            self._refresh(kind, row)

    def _remove(self, kind: DeclarationKind) -> None:
        row = self._list(kind).currentRow()
        declarations = self._declarations(kind)
        if row < 0:
            return
        existing = declarations[row]
        if isinstance(existing, FlowVariableDefinition) and any(
            output.source_variable_id == existing.id for output in self._outputs
        ):
            QMessageBox.warning(
                self,
                "Variable Is an Output Source",
                "Remove or retarget the Output before deleting this Variable.",
            )
            return
        reference_count = count_flow_value_references(self._commands, existing.id)
        impact = (
            f"\n\n{reference_count} saved command reference(s) will become stale "
            "and must be repaired."
            if reference_count
            else ""
        )
        if isinstance(existing, FlowInputDefinition):
            impact += (
                "\n\nSaved run setups and Run Flow callers may also require repair."
            )
        reply = QMessageBox.question(
            self,
            f"Remove {kind.title()}",
            f"Remove '{existing.name}'?{impact}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        candidate = list(declarations)
        del candidate[row]
        if self._commit_candidate(kind, candidate):
            self._refresh(kind, row)

    def _move(self, kind: DeclarationKind, offset: int) -> None:
        row = self._list(kind).currentRow()
        declarations = self._declarations(kind)
        target = row + offset
        if row < 0 or target < 0 or target >= len(declarations):
            return
        declarations[row], declarations[target] = declarations[target], declarations[row]
        self._refresh(kind, target)

    def _commit_candidate(
        self,
        kind: DeclarationKind,
        candidate: Sequence[Declaration],
    ) -> bool:
        inputs = candidate if kind == "input" else self._inputs
        variables = candidate if kind == "variable" else self._variables
        try:
            catalog = FlowValueCatalog.build(inputs, variables, self._outputs)
        except FlowValueDeclarationError as exc:
            QMessageBox.warning(self, "Invalid Flow Values", str(exc))
            return False
        self._inputs = list(catalog.inputs)
        self._variables = list(catalog.variables)
        if kind == "variable":
            self._refresh_outputs(self._output_list.currentRow())
        return True

    def _refresh_outputs(self, selected_row: int | None = None) -> None:
        self._output_list.clear()
        variables_by_id = {variable.id: variable for variable in self._variables}
        for output in self._outputs:
            source = variables_by_id.get(output.source_variable_id)
            source_name = source.name if source is not None else "Missing Variable"
            text = (
                f"{output.name}  ·  {_TYPE_LABELS[output.type]}  ·  "
                f"source {source_name}"
            )
            if output.description:
                text += f"  —  {output.description}"
            self._output_list.addItem(text)
        if self._outputs:
            row = (
                0
                if selected_row is None
                else min(max(0, selected_row), len(self._outputs) - 1)
            )
            self._output_list.setCurrentRow(row)
        self._update_output_actions()

    def _update_output_actions(self) -> None:
        row = self._output_list.currentRow()
        count = len(self._outputs)
        self._output_actions["add"].setEnabled(bool(self._variables))
        self._output_actions["add"].setToolTip(
            "" if self._variables else "Declare a Variable before adding an Output."
        )
        self._output_actions["edit"].setEnabled(row >= 0)
        self._output_actions["remove"].setEnabled(row >= 0)
        self._output_actions["up"].setEnabled(row > 0)
        self._output_actions["down"].setEnabled(0 <= row < count - 1)

    def _add_output(self) -> None:
        if not self._variables:
            return
        dialog = FlowOutputEditDialog(self._variables, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        candidate = [*self._outputs, dialog.definition()]
        if self._commit_outputs(candidate):
            self._refresh_outputs(len(self._outputs) - 1)

    def _edit_output(self) -> None:
        row = self._output_list.currentRow()
        if row < 0:
            return
        existing = self._outputs[row]
        dialog = FlowOutputEditDialog(self._variables, existing, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = dialog.definition()
        if replacement.source_variable_id != existing.source_variable_id:
            reply = QMessageBox.question(
                self,
                "Retarget Output",
                f"Retarget '{existing.name}' to a different Variable? External "
                "callers will receive the new source value.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        candidate = list(self._outputs)
        candidate[row] = replacement
        if self._commit_outputs(candidate):
            self._refresh_outputs(row)

    def _remove_output(self) -> None:
        row = self._output_list.currentRow()
        if row < 0:
            return
        existing = self._outputs[row]
        reference_count = count_flow_value_references(self._commands, existing.id)
        local_impact = (
            f" {reference_count} command reference(s) in this flow will become stale."
            if reference_count
            else ""
        )
        reply = QMessageBox.question(
            self,
            "Remove Output",
            f"Remove Output '{existing.name}'?{local_impact} Saved Run Flow callers "
            "may also require repair.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        candidate = list(self._outputs)
        del candidate[row]
        if self._commit_outputs(candidate):
            self._refresh_outputs(row)

    def _move_output(self, offset: int) -> None:
        row = self._output_list.currentRow()
        target = row + offset
        if row < 0 or target < 0 or target >= len(self._outputs):
            return
        self._outputs[row], self._outputs[target] = (
            self._outputs[target],
            self._outputs[row],
        )
        self._refresh_outputs(target)

    def _commit_outputs(self, candidate: Sequence[FlowOutputDefinition]) -> bool:
        try:
            catalog = FlowValueCatalog.build(
                self._inputs,
                self._variables,
                candidate,
            )
        except FlowValueDeclarationError as exc:
            QMessageBox.warning(self, "Invalid Outputs", str(exc))
            return False
        self._outputs = list(catalog.outputs)
        return True

    def _validate_and_accept(self) -> None:
        try:
            FlowValueCatalog.build(self._inputs, self._variables, self._outputs)
        except FlowValueDeclarationError as exc:
            QMessageBox.warning(self, "Invalid Flow Values", str(exc))
            return
        self.accept()

    def inputs(self) -> list[FlowInputDefinition]:
        return list(self._inputs)

    def variables(self) -> list[FlowVariableDefinition]:
        return list(self._variables)

    def outputs(self) -> list[FlowOutputDefinition]:
        return list(self._outputs)


__all__ = [
    "FlowOutputEditDialog",
    "FlowValueEditDialog",
    "FlowValuesDialog",
    "count_flow_value_references",
]
