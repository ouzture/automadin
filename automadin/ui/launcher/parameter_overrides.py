"""Reusable widgets for saved and transient flow parameter overrides."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from automadin.models.flow_parameter import (
    FlowParameterDefinition,
    FlowParameterError,
    ParameterValue,
    canonical_parameter_text,
    coerce_parameter_value,
)


class ParameterOverridesWidget(QGroupBox):
    def __init__(
        self,
        title: str = "Flow Parameters",
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self._definitions: list[FlowParameterDefinition] = []
        self._rows: dict[str, tuple[QCheckBox, QLineEdit]] = {}
        self._unknown_overrides: tuple[str, ...] = ()
        self._form = QFormLayout(self)
        self._add_empty_label()

    def _add_empty_label(self) -> None:
        empty_label = QLabel("The selected flow declares no parameters.")
        empty_label.setWordWrap(True)
        self._form.addRow(empty_label)

    def set_parameters(
        self,
        definitions: list[FlowParameterDefinition],
        overrides: Mapping[str, Any] | object | None = None,
        *,
        select_all: bool = False,
    ) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self._definitions = list(definitions)
        safe_overrides = overrides if isinstance(overrides, Mapping) else {}
        known_names = {definition.name for definition in definitions}
        self._unknown_overrides = tuple(
            sorted(
                str(name)
                for name in safe_overrides
                if not isinstance(name, str) or name not in known_names
            )
        )
        self._rows.clear()
        if not definitions:
            self._add_empty_label()
            self.setVisible(False)
            return

        for definition in definitions:
            checkbox = QCheckBox("Override")
            editor = QLineEdit()
            has_override = definition.name in safe_overrides
            initial = (
                safe_overrides[definition.name]
                if has_override
                else definition.default
            )
            editor.setText(canonical_parameter_text(initial))
            editor.setEnabled(select_all or has_override)
            checkbox.setChecked(select_all or has_override)
            checkbox.toggled.connect(editor.setEnabled)
            if definition.description:
                editor.setToolTip(definition.description)
            row = QHBoxLayout()
            row.addWidget(checkbox)
            row.addWidget(editor, 1)
            row_widget = QWidget()
            row_widget.setLayout(row)
            label = (
                f"{definition.name} "
                f"({definition.type.value}; default "
                f"{canonical_parameter_text(definition.default)}):"
            )
            self._form.addRow(label, row_widget)
            self._rows[definition.name] = (checkbox, editor)
        self.setVisible(True)

    def overrides(self) -> dict[str, ParameterValue]:
        if self._unknown_overrides:
            names = ", ".join(self._unknown_overrides)
            raise FlowParameterError(
                f"Unknown flow parameter override(s): {names}."
            )
        definitions = {
            definition.name: definition for definition in self._definitions
        }
        result: dict[str, ParameterValue] = {}
        for name, (checkbox, editor) in self._rows.items():
            if not checkbox.isChecked():
                continue
            definition = definitions[name]
            result[name] = coerce_parameter_value(
                definition.type,
                editor.text(),
                name=name,
            )
        return result


class ManualParameterOverridesDialog(QDialog):
    def __init__(
        self,
        run_name: str,
        definitions: list[FlowParameterDefinition],
        effective_values: Mapping[str, ParameterValue],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._overrides: dict[str, ParameterValue] | None = None
        self.setWindowTitle(f"Run Parameters — {run_name}")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "These values apply only to this launch and are not saved back to "
            "the flow or run setup."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._widget = ParameterOverridesWidget("One-run overrides")
        self._widget.set_parameters(
            definitions,
            effective_values,
            select_all=True,
        )
        layout.addWidget(self._widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        try:
            self._overrides = self._widget.overrides()
        except FlowParameterError as exc:
            QMessageBox.warning(self, "Invalid Flow Parameters", str(exc))
            return
        self.accept()

    def overrides(self) -> dict[str, ParameterValue]:
        if self._overrides is None:
            raise RuntimeError("The run parameter dialog was not accepted.")
        return dict(self._overrides)
