"""Flow parameter declaration dialogs."""

from __future__ import annotations

from PySide6.QtWidgets import (
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
    QVBoxLayout,
    QWidget,
)

from automadin.models.flow_parameter import (
    FlowParameterDefinition,
    FlowParameterError,
    FlowParameterType,
    canonical_parameter_text,
)


class FlowParameterEditDialog(QDialog):
    def __init__(
        self,
        existing: FlowParameterDefinition | None = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._definition: FlowParameterDefinition | None = None
        self.setWindowTitle("Edit Flow Parameter" if existing else "Add Flow Parameter")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. wait_duration")
        form.addRow("Name:", self._name_edit)

        self._type_combo = QComboBox()
        labels = {
            FlowParameterType.TEXT: "Text",
            FlowParameterType.INTEGER: "Integer",
            FlowParameterType.NUMBER: "Number",
            FlowParameterType.BOOLEAN: "Boolean",
            FlowParameterType.DURATION_MS: "Duration",
        }
        for parameter_type in FlowParameterType:
            self._type_combo.addItem(labels[parameter_type], parameter_type)
        form.addRow("Type:", self._type_combo)

        self._default_edit = QLineEdit()
        form.addRow("Default:", self._default_edit)
        self._description_edit = QLineEdit()
        self._description_edit.setPlaceholderText("Optional guidance shown when running")
        form.addRow("Description:", self._description_edit)
        layout.addLayout(form)

        hint = QLabel(
            "Durations accept values such as 300 ms, 1.5 s, or 2 min. "
            "Names are case-sensitive."
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
            self._default_edit.setText(canonical_parameter_text(existing.default))
            self._description_edit.setText(existing.description)

    def _validate_and_accept(self) -> None:
        try:
            self._definition = FlowParameterDefinition(
                self._name_edit.text().strip(),
                self._type_combo.currentData(),
                self._default_edit.text(),
                self._description_edit.text().strip(),
            )
        except FlowParameterError as exc:
            QMessageBox.warning(self, "Invalid Flow Parameter", str(exc))
            return
        self.accept()

    def definition(self) -> FlowParameterDefinition:
        if self._definition is None:
            raise RuntimeError("The flow parameter dialog was not accepted.")
        return self._definition


class FlowParametersDialog(QDialog):
    def __init__(
        self,
        parameters: list[FlowParameterDefinition],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._parameters = list(parameters)
        self.setWindowTitle("Flow Parameters")
        self.setMinimumSize(620, 380)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Declare typed inputs resolved before each run. Parameters are immutable "
            "during execution and take precedence over runtime variables with the "
            "same name."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._edit_parameter)
        layout.addWidget(self._list, 1)

        actions = QHBoxLayout()
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_parameter)
        actions.addWidget(add_button)
        edit_button = QPushButton("Edit")
        edit_button.clicked.connect(self._edit_parameter)
        actions.addWidget(edit_button)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._remove_parameter)
        actions.addWidget(remove_button)
        actions.addStretch()
        up_button = QPushButton("Move up")
        up_button.clicked.connect(lambda: self._move_selected(-1))
        actions.addWidget(up_button)
        down_button = QPushButton("Move down")
        down_button.clicked.connect(lambda: self._move_selected(1))
        actions.addWidget(down_button)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _refresh(self, selected_row: int | None = None) -> None:
        self._list.clear()
        for definition in self._parameters:
            text = (
                f"{definition.name}  ·  {definition.type.value}  ·  "
                f"default {canonical_parameter_text(definition.default)}"
            )
            if definition.description:
                text += f"  —  {definition.description}"
            self._list.addItem(text)
        if self._parameters:
            row = (
                min(max(0, selected_row), len(self._parameters) - 1)
                if selected_row is not None
                else 0
            )
            self._list.setCurrentRow(row)

    def _add_parameter(self) -> None:
        dialog = FlowParameterEditDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        definition = dialog.definition()
        if any(item.name == definition.name for item in self._parameters):
            QMessageBox.warning(
                self,
                "Duplicate Flow Parameter",
                f"Flow parameter '{definition.name}' already exists.",
            )
            return
        self._parameters.append(definition)
        self._refresh(len(self._parameters) - 1)

    def _edit_parameter(self, *_args: object) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        dialog = FlowParameterEditDialog(self._parameters[row], parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        definition = dialog.definition()
        if any(
            index != row and item.name == definition.name
            for index, item in enumerate(self._parameters)
        ):
            QMessageBox.warning(
                self,
                "Duplicate Flow Parameter",
                f"Flow parameter '{definition.name}' already exists.",
            )
            return
        self._parameters[row] = definition
        self._refresh(row)

    def _remove_parameter(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        del self._parameters[row]
        self._refresh(row)

    def _move_selected(self, offset: int) -> None:
        row = self._list.currentRow()
        target = row + offset
        if row < 0 or target < 0 or target >= len(self._parameters):
            return
        self._parameters[row], self._parameters[target] = (
            self._parameters[target],
            self._parameters[row],
        )
        self._refresh(target)

    def parameters(self) -> list[FlowParameterDefinition]:
        return list(self._parameters)
