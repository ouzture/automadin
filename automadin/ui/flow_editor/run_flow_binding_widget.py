"""Structured version-1 Run Flow binding editor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow
from automadin.models.resource_path import resource_display_label, resource_sort_key
from automadin.models.run_flow_binding import RunFlowBinding, RunFlowBindingError
from automadin.models.typed_value import ValueType, is_value_type_assignable
from automadin.models.value_reference import ValueReference
from automadin.ui.flow_editor.param_editor import NoScrollComboBox, ParamEditorWidget
from automadin.ui.theme import COLORS

_GLOBAL_PARAM_NAMES = frozenset({"pause_ms", "min_wait_time"})


class RunFlowBindingWidget(QWidget):
    """Edit one rename-safe child Flow contract directly on a command card."""

    def __init__(
        self,
        params: Mapping[str, Any],
        *,
        db: Any,
        parent_flow: AutomationFlow,
        builtin_types: Mapping[str, ValueType],
        on_change: Callable[[dict[str, object]], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._parent_flow = parent_flow
        self._parent_catalog = parent_flow.value_catalog
        self._builtin_types = dict(builtin_types)
        self._on_change = on_change
        self._binding: RunFlowBinding | None = None
        self._child: AutomationFlow | None = None
        self._input_mappings: dict[str, ValueReference] = {}
        self._output_mappings: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        target_label = QLabel("Child Flow")
        target_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-weight: 600;"
        )
        layout.addWidget(target_label)
        self._target_picker = NoScrollComboBox()
        self._target_picker.setObjectName("runFlowTargetPicker")
        layout.addWidget(self._target_picker)

        self._warning = QLabel()
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet(f"color: {COLORS['error']};")
        self._warning.setVisible(False)
        layout.addWidget(self._warning)

        self._mappings_host = QWidget()
        self._mappings_layout = QVBoxLayout(self._mappings_host)
        self._mappings_layout.setContentsMargins(0, 2, 0, 0)
        self._mappings_layout.setSpacing(10)
        layout.addWidget(self._mappings_host)

        binding_params = {
            name: value for name, value in params.items() if name not in _GLOBAL_PARAM_NAMES
        }
        if binding_params:
            try:
                self._binding = RunFlowBinding.from_params(binding_params)
            except RunFlowBindingError as exc:
                self._warning.setText(
                    f"This saved Run Flow binding is invalid and must be repaired: {exc}"
                )
                self._warning.setVisible(True)
        if self._binding is not None:
            self._input_mappings = dict(self._binding.input_mappings)
            self._output_mappings = dict(self._binding.output_mappings)

        self._populate_targets()
        self._target_picker.currentIndexChanged.connect(self._target_changed)
        self._load_selected_child()
        self._rebuild_mappings()

    def _typed_flows(self) -> list[AutomationFlow]:
        if self._db is None or not hasattr(self._db, "list_flows"):
            return []
        flows: list[AutomationFlow] = []
        for name in self._db.list_flows():
            flow = self._db.load_flow(name)
            if (
                flow is not None
                and flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
                and flow.flow_id != self._parent_flow.flow_id
            ):
                flows.append(flow)
        return sorted(flows, key=lambda flow: resource_sort_key(flow.name))

    def _populate_targets(self) -> None:
        self._target_picker.blockSignals(True)
        self._target_picker.clear()
        self._target_picker.addItem("Select a child flow…", "")
        folder_index = (
            self._db.workspace_folder_index()
            if self._db is not None and hasattr(self._db, "workspace_folder_index")
            else None
        )
        known_ids: set[str] = set()
        for flow in self._typed_flows():
            known_ids.add(flow.flow_id)
            display_path = (
                folder_index.display_path(flow.name)
                if folder_index is not None
                else flow.name
            )
            self._target_picker.addItem(
                resource_display_label(display_path),
                flow.flow_id,
            )
            self._target_picker.setItemData(
                self._target_picker.count() - 1,
                display_path,
                Qt.ItemDataRole.ToolTipRole,
            )
        selected_id = self._binding.flow_id if self._binding is not None else ""
        if selected_id and selected_id not in known_ids:
            self._target_picker.addItem(
                f"Unavailable child Flow ({selected_id})",
                selected_id,
            )
        index = self._target_picker.findData(selected_id)
        self._target_picker.setCurrentIndex(max(0, index))
        self._target_picker.blockSignals(False)

    def _load_selected_child(self) -> None:
        flow_id = self._target_picker.currentData()
        self._child = (
            self._db.load_flow_by_id(flow_id)
            if isinstance(flow_id, str)
            and flow_id
            and self._db is not None
            and hasattr(self._db, "load_flow_by_id")
            else None
        )
        if (
            self._child is not None
            and self._child.value_model_version != CURRENT_FLOW_VALUE_MODEL_VERSION
        ):
            self._child = None

    def _target_changed(self, _index: int) -> None:
        flow_id = self._target_picker.currentData()
        if not isinstance(flow_id, str) or not flow_id:
            self._binding = None
            self._child = None
            self._input_mappings.clear()
            self._output_mappings.clear()
            self._rebuild_mappings()
            return
        self._binding = RunFlowBinding(flow_id, {}, {})
        self._input_mappings.clear()
        self._output_mappings.clear()
        self._warning.setVisible(False)
        self._load_selected_child()
        self._emit_binding()
        self._rebuild_mappings()

    def _emit_binding(self) -> None:
        flow_id = self._target_picker.currentData()
        if not isinstance(flow_id, str) or not flow_id:
            return
        self._binding = RunFlowBinding(
            flow_id,
            self._input_mappings,
            self._output_mappings,
        )
        self._on_change(self._binding.to_params())

    def _clear_mappings(self) -> None:
        while self._mappings_layout.count():
            item = self._mappings_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild_mappings(self) -> None:
        self._clear_mappings()
        if self._child is None:
            hint = QLabel("Select an available version-1 Flow to configure mappings.")
            hint.setStyleSheet(f"color: {COLORS['text_secondary']};")
            self._mappings_layout.addWidget(hint)
            return

        child_catalog = self._child.value_catalog
        stale_input_ids = set(self._input_mappings) - {
            definition.id for definition in child_catalog.inputs
        }
        stale_output_ids = set(self._output_mappings) - {
            definition.id for definition in child_catalog.outputs
        }
        if stale_input_ids or stale_output_ids:
            self._mappings_layout.addWidget(
                self._stale_mapping_warning(stale_input_ids, stale_output_ids)
            )

        if child_catalog.inputs:
            heading = QLabel("Child Inputs")
            heading.setStyleSheet("font-weight: 650;")
            self._mappings_layout.addWidget(heading)
            input_form = QFormLayout()
            input_form.setContentsMargins(0, 0, 0, 0)
            input_form.setSpacing(8)
            for definition in child_catalog.inputs:
                input_form.addRow(
                    f"{definition.name} ({definition.type.value})",
                    self._input_mapping_row(definition),
                )
            self._mappings_layout.addLayout(input_form)

        if child_catalog.outputs:
            heading = QLabel("Child Outputs")
            heading.setStyleSheet("font-weight: 650;")
            self._mappings_layout.addWidget(heading)
            output_form = QFormLayout()
            output_form.setContentsMargins(0, 0, 0, 0)
            output_form.setSpacing(8)
            for definition in child_catalog.outputs:
                output_form.addRow(
                    f"{definition.name} ({definition.type.value})",
                    self._output_mapping_picker(definition.id, definition.type),
                )
            self._mappings_layout.addLayout(output_form)

        if not child_catalog.inputs and not child_catalog.outputs:
            hint = QLabel("This child Flow has no Inputs or Outputs to map.")
            hint.setStyleSheet(f"color: {COLORS['text_secondary']};")
            self._mappings_layout.addWidget(hint)

    def _stale_mapping_warning(
        self,
        stale_input_ids: set[str],
        stale_output_ids: set[str],
    ) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        count = len(stale_input_ids) + len(stale_output_ids)
        label = QLabel(f"{count} stale child mapping(s) must be removed or repaired.")
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {COLORS['error']};")
        button = QPushButton("Remove stale mappings")

        def _remove() -> None:
            reply = QMessageBox.question(
                self,
                "Remove Stale Run Flow Mappings",
                "Remove mappings for Inputs or Outputs no longer declared by the child Flow?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            for input_id in stale_input_ids:
                self._input_mappings.pop(input_id, None)
            for output_id in stale_output_ids:
                self._output_mappings.pop(output_id, None)
            self._emit_binding()
            self._rebuild_mappings()

        button.clicked.connect(_remove)
        layout.addWidget(label, 1)
        layout.addWidget(button)
        return container

    def _input_mapping_row(self, definition: Any) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        mapped = definition.id in self._input_mappings
        checkbox = QCheckBox("Required" if definition.required else "Map value")
        checkbox.setChecked(definition.required or mapped)
        checkbox.setEnabled(not definition.required)
        layout.addWidget(checkbox)
        allowed_types = [
            value_type.value
            for value_type in ValueType
            if is_value_type_assignable(value_type, definition.type)
        ]
        editor = ParamEditorWidget(
            {
                "name": definition.id,
                "type": "value_reference",
                "label": "Source value",
                "allowed_value_types": allowed_types,
            },
            self._input_mappings[definition.id].to_dict() if mapped else None,
            lambda _name, value, input_id=definition.id: self._set_input_mapping(
                input_id, value
            ),
            db=self._db,
            value_catalog=self._parent_catalog,
            builtin_types=self._builtin_types,
        )
        editor.setEnabled(definition.required or mapped)
        layout.addWidget(editor)

        def _toggle(checked: bool) -> None:
            editor.setEnabled(checked)
            if not checked:
                self._input_mappings.pop(definition.id, None)
                self._emit_binding()

        checkbox.toggled.connect(_toggle)
        return container

    def _set_input_mapping(self, input_id: str, value: object) -> None:
        candidate = RunFlowBinding(
            self._target_picker.currentData(),
            {**self._input_mappings, input_id: value},
            self._output_mappings,
        )
        self._input_mappings = dict(candidate.input_mappings)
        self._emit_binding()

    def _output_mapping_picker(
        self,
        output_id: str,
        output_type: ValueType,
    ) -> QComboBox:
        combo = NoScrollComboBox()
        combo.setObjectName("runFlowOutputTargetPicker")
        combo.addItem("Do not capture", "")
        compatible_ids: set[str] = set()
        for variable in self._parent_catalog.variables:
            if not is_value_type_assignable(output_type, variable.type):
                continue
            compatible_ids.add(variable.id)
            combo.addItem(
                f"{variable.name} ({variable.type.value})",
                variable.id,
            )
        current = self._output_mappings.get(output_id, "")
        if current and current not in compatible_ids:
            variable = self._parent_catalog.variable_by_id(current)
            label = (
                f"Incompatible Variable — {variable.name} ({variable.type.value})"
                if variable is not None
                else f"Missing Variable ({current})"
            )
            combo.addItem(label, current)
        combo.setCurrentIndex(max(0, combo.findData(current)))

        def _changed(_index: int) -> None:
            selected = combo.currentData()
            if isinstance(selected, str) and selected:
                self._output_mappings[output_id] = selected
            else:
                self._output_mappings.pop(output_id, None)
            self._emit_binding()

        combo.currentIndexChanged.connect(_changed)
        return combo


__all__ = ["RunFlowBindingWidget"]
