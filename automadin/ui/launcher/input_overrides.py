"""One-run UUID-keyed Input overrides for value-model version 1 flows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.execution.flow_values import (
    FlowRuntimeValueError,
    resolve_flow_inputs,
)
from automadin.models.flow_value import FlowInputDefinition
from automadin.models.typed_value import (
    TypedValue,
    ValueCoercionError,
    canonical_typed_value_text,
    coerce_typed_value,
)


class InputOverridesWidget(QWidget):
    def __init__(
        self,
        *,
        mode: Literal["manual", "saved"] = "manual",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        self._definitions: list[FlowInputDefinition] = []
        self._saved_overrides: Mapping[str, Any] = {}
        self._unknown_saved_ids: tuple[str, ...] = ()
        self._stale_cleanup_confirmed = False
        self._rows: dict[str, tuple[QCheckBox, QLineEdit]] = {}
        self._form = QFormLayout(self)
        self._form.setHorizontalSpacing(14)
        self._form.setVerticalSpacing(10)

    def set_inputs(
        self,
        definitions: Sequence[FlowInputDefinition],
        saved_overrides: Mapping[str, Any] | object | None = None,
    ) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self._definitions = list(definitions)
        self._saved_overrides = (
            saved_overrides if isinstance(saved_overrides, Mapping) else {}
        )
        known_ids = {definition.id for definition in definitions}
        self._unknown_saved_ids = tuple(
            sorted(str(key) for key in self._saved_overrides if key not in known_ids)
        )
        self._stale_cleanup_confirmed = False
        self._rows.clear()

        if self._unknown_saved_ids and self._mode == "saved":
            stale_label = QLabel(
                f"{len(self._unknown_saved_ids)} stale saved override(s) no longer "
                "match this flow."
            )
            stale_label.setWordWrap(True)
            cleanup_button = QPushButton("Remove stale overrides")

            def _confirm_cleanup() -> None:
                reply = QMessageBox.question(
                    self,
                    "Remove Stale Input Overrides",
                    "Remove saved Input overrides that no longer match this flow?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._stale_cleanup_confirmed = True
                    stale_label.setText(
                        "Stale saved overrides will be removed when this setup is saved."
                    )
                    cleanup_button.setEnabled(False)

            cleanup_button.clicked.connect(_confirm_cleanup)
            cleanup_row = QHBoxLayout()
            cleanup_row.addWidget(stale_label, 1)
            cleanup_row.addWidget(cleanup_button)
            cleanup_widget = QWidget()
            cleanup_widget.setLayout(cleanup_row)
            self._form.addRow(cleanup_widget)

        for definition in definitions:
            has_saved = definition.id in self._saved_overrides
            missing_required = definition.required and not has_saved
            baseline = (
                self._saved_overrides[definition.id]
                if has_saved
                else definition.default
                if not definition.required
                else ""
            )
            checkbox = QCheckBox(
                "Required"
                if missing_required
                else "Save value"
                if self._mode == "saved"
                else "Override"
            )
            selected = missing_required or (self._mode == "saved" and has_saved)
            checkbox.setChecked(selected)
            checkbox.setEnabled(not missing_required)
            editor = QLineEdit(canonical_typed_value_text(baseline))
            editor.setEnabled(selected)
            checkbox.toggled.connect(editor.setEnabled)
            if definition.description:
                editor.setToolTip(definition.description)

            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            row.addWidget(checkbox)
            row.addWidget(editor, 1)
            row_widget = QWidget()
            row_widget.setLayout(row)
            baseline_label = (
                "saved value"
                if has_saved
                else "required"
                if definition.required
                else f"default {canonical_typed_value_text(definition.default)}"
            )
            label = f"{definition.name} ({definition.type.value}; {baseline_label})"
            self._form.addRow(label, row_widget)
            self._rows[definition.id] = (checkbox, editor)
        self.setVisible(bool(definitions or self._unknown_saved_ids))

    def overrides(self) -> dict[str, TypedValue]:
        if self._unknown_saved_ids and (
            self._mode == "manual" or not self._stale_cleanup_confirmed
        ):
            raise FlowRuntimeValueError(
                "Saved Input overrides contain stale IDs. Remove stale overrides "
                "before continuing."
            )
        definitions = {definition.id: definition for definition in self._definitions}
        saved_for_resolution = {
            input_id: value
            for input_id, value in self._saved_overrides.items()
            if input_id in definitions
        }
        result: dict[str, TypedValue] = {}
        for input_id, (checkbox, editor) in self._rows.items():
            if not checkbox.isChecked():
                continue
            definition = definitions[input_id]
            try:
                result[input_id] = coerce_typed_value(
                    definition.type,
                    editor.text(),
                    subject=f"Input '{definition.name}'",
                )
            except ValueCoercionError as exc:
                raise FlowRuntimeValueError(str(exc)) from exc
        if self._mode == "saved":
            resolve_flow_inputs(self._definitions, result, {})
        else:
            resolve_flow_inputs(
                self._definitions,
                saved_for_resolution,
                result,
            )
        return result


class ManualInputOverridesDialog(QDialog):
    def __init__(
        self,
        run_name: str,
        definitions: Sequence[FlowInputDefinition],
        saved_overrides: Mapping[str, Any] | object | None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._overrides: dict[str, TypedValue] | None = None
        self.setWindowTitle(f"Run Inputs — {run_name}")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        hint = QLabel(
            "Choose one-run overrides. Unchecked Inputs keep the saved value or "
            "flow default; these changes are not saved back to the run setup."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._widget = InputOverridesWidget()
        self._widget.set_inputs(definitions, saved_overrides)
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
        except FlowRuntimeValueError as exc:
            QMessageBox.warning(self, "Invalid Flow Inputs", str(exc))
            return
        self.accept()

    def overrides(self) -> dict[str, TypedValue]:
        if self._overrides is None:
            raise RuntimeError("The run Input dialog was not accepted.")
        return dict(self._overrides)


__all__ = ["InputOverridesWidget", "ManualInputOverridesDialog"]
