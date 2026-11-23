"""Flow-level resolution scaling settings dialog."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from automadin.models.resolution_scaling import (
    ResolutionScalingMode,
    ResolutionScalingSettings,
)


class ResolutionScalingDialog(QDialog):
    """Edit the authored surface used by one flow."""

    def __init__(
        self,
        settings: ResolutionScalingSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Resolution-aware execution")
        self.setMinimumWidth(470)

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Scale stored image templates and pixel-based geometry when the run "
            "surface differs from the surface used to create this flow. The "
            "reference is the browser viewport, Android display, or Windows app "
            "window—not necessarily the monitor. Percent-based values are never "
            "scaled twice."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        form = QFormLayout()
        self._enabled = QCheckBox("Enable adaptive resolution scaling")
        self._enabled.setObjectName("resolutionScalingEnabled")
        self._enabled.setChecked(settings.mode is ResolutionScalingMode.ADAPTIVE)
        form.addRow("", self._enabled)

        self._reference_width = QSpinBox()
        self._reference_width.setObjectName("resolutionReferenceWidth")
        self._reference_width.setRange(2, 16_384)
        self._reference_width.setSuffix(" px")
        self._reference_width.setValue(max(2, settings.reference_width or 1920))
        form.addRow("Reference width:", self._reference_width)

        self._reference_height = QSpinBox()
        self._reference_height.setObjectName("resolutionReferenceHeight")
        self._reference_height.setRange(2, 16_384)
        self._reference_height.setSuffix(" px")
        self._reference_height.setValue(max(2, settings.reference_height or 1080))
        form.addRow("Reference height:", self._reference_height)

        self._scale_pixel_geometry = QCheckBox(
            "Scale pixel coordinates, offsets, and regions"
        )
        self._scale_pixel_geometry.setObjectName("resolutionScalePixelGeometry")
        self._scale_pixel_geometry.setChecked(settings.scale_pixel_geometry)
        self._scale_pixel_geometry.setToolTip(
            "Percentage-based coordinates and regions remain relative and are "
            "not scaled again."
        )
        form.addRow("", self._scale_pixel_geometry)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_settings)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._enabled.toggled.connect(self._refresh_enabled_state)
        self._refresh_enabled_state()

    def _refresh_enabled_state(self) -> None:
        enabled = self._enabled.isChecked()
        self._reference_width.setEnabled(enabled)
        self._reference_height.setEnabled(enabled)
        self._scale_pixel_geometry.setEnabled(enabled)

    def settings(self) -> ResolutionScalingSettings:
        mode = (
            ResolutionScalingMode.ADAPTIVE
            if self._enabled.isChecked()
            else ResolutionScalingMode.OFF
        )
        return ResolutionScalingSettings(
            mode,
            self._reference_width.value(),
            self._reference_height.value(),
            self._scale_pixel_geometry.isChecked(),
        )

    def _accept_settings(self) -> None:
        try:
            self.settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Reference Surface", str(exc))
            return
        self.accept()


__all__ = ["ResolutionScalingDialog"]
