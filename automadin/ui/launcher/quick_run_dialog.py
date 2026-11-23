"""One-run flow selection for the already connected target."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from automadin.execution.flow_values import FlowRuntimeValueError
from automadin.models.typed_value import TypedValue
from automadin.services.database import DatabaseService
from automadin.ui.launcher.input_overrides import InputOverridesWidget


class QuickRunDialog(QDialog):
    """Collect one transient flow invocation for a connected session."""

    def __init__(
        self,
        database: DatabaseService,
        target_label: str,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._accepted_flow_name = ""
        self._accepted_overrides: dict[str, TypedValue] | None = None
        self.setWindowTitle("Quick Run")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        intro = QLabel(
            "Run one flow once on the target that is already connected. "
            "Nothing in this dialog is saved as a Run Setup."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self._flow = QComboBox()
        self._flow.setAccessibleName("Quick Run flow")
        folder_index = self._database.workspace_folder_index()
        for name in self._database.list_flows():
            self._flow.addItem(folder_index.display_path(name), name)
        self._flow.currentIndexChanged.connect(self._flow_changed)
        form.addRow("Flow", self._flow)

        target = QLabel(target_label)
        target.setWordWrap(True)
        target.setAccessibleName("Quick Run connected target")
        form.addRow("Connected target", target)
        layout.addLayout(form)

        self._inputs = InputOverridesWidget(mode="manual")
        layout.addWidget(self._inputs)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        run_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        run_button.setText("Run Once")
        run_button.setEnabled(self._flow.count() > 0)
        self._buttons.accepted.connect(self._validate_and_accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        self._flow_changed()

    def _flow_changed(self, _index: int = -1) -> None:
        flow_name = self._flow.currentData()
        flow = self._database.load_flow(flow_name) if isinstance(flow_name, str) else None
        self._inputs.set_inputs(flow.inputs if flow is not None else ())

    def _validate_and_accept(self) -> None:
        flow_name = self._flow.currentData()
        if not isinstance(flow_name, str) or not flow_name:
            QMessageBox.warning(self, "No Flow", "Save and select a flow first.")
            return
        try:
            overrides = self._inputs.overrides()
        except FlowRuntimeValueError as exc:
            QMessageBox.warning(self, "Invalid Flow Inputs", str(exc))
            return
        self._accepted_flow_name = flow_name
        self._accepted_overrides = overrides
        self.accept()

    def flow_name(self) -> str:
        if not self._accepted_flow_name:
            raise RuntimeError("The Quick Run dialog was not accepted.")
        return self._accepted_flow_name

    def input_overrides(self) -> dict[str, TypedValue]:
        if self._accepted_overrides is None:
            raise RuntimeError("The Quick Run dialog was not accepted.")
        return dict(self._accepted_overrides)


__all__ = ["QuickRunDialog"]
