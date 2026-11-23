"""Editor dialog for reusable visual rules."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.session import DeviceSessionService
from automadin.services.opencv_loader import load_cv2
from automadin.models.resource_path import (
    WorkspaceFolderIndex,
    join_resource_path,
    resource_group_path,
    resource_leaf_name,
)
from automadin.models.visual_rule import VisualRule
from automadin.services.screen_capture import (
    CaptureFrame,
    capture_source_matches_session,
)
from automadin.services.visual_detection import decode_screenshot, detect_blobs
from automadin.ui.capture_controller import (
    CaptureBusyError,
    CaptureOperationController,
    CaptureStartError,
)
from automadin.ui.theme import COLORS
from automadin.ui.editable_combo import set_editable_combo_text
from automadin.ui.widgets.interactive_screenshot import InteractiveScreenshotWidget


cv2 = load_cv2()


class VisualRuleDialog(QDialog):
    _COLOR_PRESETS = {
        "tight": (20, 80, 3, 12, 12),
        "normal": (10, 90, 6, 25, 25),
        "loose": (5, 95, 10, 40, 40),
        "very_loose": (1, 99, 16, 65, 65),
    }
    _SIZE_PRESETS = {
        "tiny": (10, 4000),
        "small": (50, 12000),
        "medium": (200, 40000),
        "large": (800, 120000),
    }
    _SHAPE_PRESETS = {
        "off": 0.0,
        "loose": 0.15,
        "roundish": 0.35,
    }

    def __init__(
        self,
        existing: VisualRule | None = None,
        session_service: DeviceSessionService | None = None,
        device_selector=None,
        known_paths: list[str] | None = None,
        known_folders: list[str] | None = None,
        parent=None,
        *,
        capture_controller: CaptureOperationController | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Visual Rule")
        self.resize(1160, 760)
        self._existing = existing
        self._device_selector = device_selector
        self._capture_controller = capture_controller
        self._connected_capture_available = capture_controller is not None
        self._known_paths = list(known_paths or [])
        self._known_folders = list(known_folders or [])
        self._folder_index = WorkspaceFolderIndex(
            explicit_groups=self._known_folders,
            resource_paths=self._known_paths,
        )
        self._capture_operation_id: str | None = None
        self._current_screenshot: bytes | None = None
        self._current_image = None
        self._last_sample_patch = None
        self._last_sample_area: int | None = None
        self._multi_ranges: list[dict[str, int]] = []
        self._build_ui()
        if self._capture_controller is not None:
            self._capture_controller.busy_changed.connect(
                self._on_capture_busy_changed
            )
            self._capture_controller.capture_succeeded.connect(
                self._on_capture_succeeded
            )
            self._capture_controller.capture_failed.connect(
                self._on_capture_failed
            )
            self._capture_controller.capture_cancelled.connect(
                self._on_capture_cancelled
            )
            self._capture_controller.capture_finished.connect(
                self._on_capture_finished
            )
        self._on_capture_busy_changed(
            self._capture_controller.is_busy
            if self._capture_controller is not None
            else False
        )
        if existing is not None:
            self._set_rule(existing)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Calibration Screenshot"))
        toolbar.addStretch()

        self._capture_btn = QPushButton("Capture")
        self._capture_btn.setProperty("cssClass", "primary")
        self._capture_btn.clicked.connect(self._capture_screenshot)
        self._capture_btn.setEnabled(
            self._connected_capture_available
            and self._device_selector is not None
        )
        toolbar.addWidget(self._capture_btn)

        self._import_btn = QPushButton("Import")
        self._import_btn.clicked.connect(self._import_screenshot)
        toolbar.addWidget(self._import_btn)

        self._preview_btn = QPushButton("Preview Rule")
        self._preview_btn.setProperty("cssClass", "success")
        self._preview_btn.clicked.connect(lambda: self._preview_rule(show_messages=True))
        toolbar.addWidget(self._preview_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._clear_overlays)
        toolbar.addWidget(self._clear_btn)
        left_layout.addLayout(toolbar)

        self._screenshot_widget = InteractiveScreenshotWidget()
        self._screenshot_widget.setText("Capture or import a screenshot, then click a target color.")
        self._screenshot_widget.set_crop_requires_shift(True)
        self._screenshot_widget.image_clicked.connect(self._on_image_clicked)
        self._screenshot_widget.coord_clicked.connect(self._on_image_clicked)
        self._screenshot_widget.crop_selected.connect(self._on_area_selected)
        left_layout.addWidget(self._screenshot_widget, 1)

        self._status_label = QLabel(
            "Pick Pixel creates a color blob rule. Sample Area creates a color region rule."
        )
        self._status_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        self._status_label.setWordWrap(True)
        left_layout.addWidget(self._status_label)
        splitter.addWidget(left)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(10)

        basics = QGroupBox("Rule")
        basics_form = QFormLayout(basics)
        basics_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self._group_combo = QComboBox()
        self._group_combo.setEditable(True)
        self._group_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._group_combo.addItem("")
        self._group_combo.addItems(self._folder_index.folders)
        self._group_combo.setToolTip(
            "Leave blank for No folder. Use / for nested folders."
        )
        basics_form.addRow("Folder", self._group_combo)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Rule name")
        basics_form.addRow("Name", self._name_edit)

        self._type_combo = QComboBox()
        self._type_combo.addItem("Color Blob", "color_blob")
        self._type_combo.addItem("Color Region", "color_region")
        self._type_combo.addItem("Multi Color Region", "multi_color_region")
        basics_form.addRow("Type", self._type_combo)
        right_layout.addWidget(basics)

        sample = QGroupBox("Guided Builder")
        sample_grid = QGridLayout(sample)
        self._selection_mode = QComboBox()
        self._selection_mode.addItem("Pick Pixel", "pixel")
        self._selection_mode.addItem("Sample Area", "sample_area")
        self._selection_mode.addItem("Multi Sample Area", "multi_sample_area")
        self._selection_mode.addItem("Search Area", "search_area")
        self._selection_mode.currentIndexChanged.connect(self._on_selection_mode_changed)

        self._sample_radius = self._spin(0, 20, 3)
        self._color_flex = QComboBox()
        self._color_flex.addItem("Tight", "tight")
        self._color_flex.addItem("Normal", "normal")
        self._color_flex.addItem("Loose", "loose")
        self._color_flex.addItem("Very Loose", "very_loose")
        self._color_flex.setCurrentIndex(1)
        self._color_flex.currentIndexChanged.connect(self._reapply_guided_controls)

        self._size_preset = QComboBox()
        self._size_preset.addItem("From Sample", "from_sample")
        self._size_preset.addItem("Tiny", "tiny")
        self._size_preset.addItem("Small", "small")
        self._size_preset.addItem("Medium", "medium")
        self._size_preset.addItem("Large", "large")
        self._size_preset.currentIndexChanged.connect(self._reapply_guided_controls)

        self._shape_preset = QComboBox()
        self._shape_preset.addItem("Off", "off")
        self._shape_preset.addItem("Loose", "loose")
        self._shape_preset.addItem("Round-ish", "roundish")
        self._shape_preset.currentIndexChanged.connect(self._reapply_guided_controls)

        sample_grid.addWidget(QLabel("Mode"), 0, 0)
        sample_grid.addWidget(self._selection_mode, 0, 1)
        sample_grid.addWidget(QLabel("Pixel Radius"), 1, 0)
        sample_grid.addWidget(self._sample_radius, 1, 1)
        sample_grid.addWidget(QLabel("Color Flexibility"), 2, 0)
        sample_grid.addWidget(self._color_flex, 2, 1)
        sample_grid.addWidget(QLabel("Minimum Size"), 3, 0)
        sample_grid.addWidget(self._size_preset, 3, 1)
        sample_grid.addWidget(QLabel("Shape Strictness"), 4, 0)
        sample_grid.addWidget(self._shape_preset, 4, 1)
        self._multi_count_label = QLabel("0 colors")
        self._add_multi_btn = QPushButton("Add Current")
        self._add_multi_btn.clicked.connect(self._add_current_range_to_multi)
        self._clear_multi_btn = QPushButton("Clear Colors")
        self._clear_multi_btn.clicked.connect(self._clear_multi_ranges)
        multi_buttons = QHBoxLayout()
        multi_buttons.setContentsMargins(0, 0, 0, 0)
        multi_buttons.addWidget(self._add_multi_btn)
        multi_buttons.addWidget(self._clear_multi_btn)
        sample_grid.addWidget(QLabel("Multi Colors"), 5, 0)
        sample_grid.addWidget(self._multi_count_label, 5, 1)
        sample_grid.addLayout(multi_buttons, 6, 0, 1, 2)
        self._swatch = QLabel("sample")
        self._swatch.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._swatch.setMinimumHeight(32)
        self._swatch.setStyleSheet(
            f"background: {COLORS['input_bg']}; border: 1px solid {COLORS['border']};"
        )
        sample_grid.addWidget(self._swatch, 0, 2, 5, 1)
        right_layout.addWidget(sample)

        color = QGroupBox("Advanced: Color Range")
        color_grid = QGridLayout(color)
        self._hue_min = self._spin(0, 179, 0)
        self._hue_max = self._spin(0, 179, 179)
        self._sat_min = self._spin(0, 255, 0)
        self._sat_max = self._spin(0, 255, 255)
        self._val_min = self._spin(0, 255, 0)
        self._val_max = self._spin(0, 255, 255)
        self._add_range_row(color_grid, 0, "Hue", self._hue_min, self._hue_max)
        self._add_range_row(color_grid, 1, "Sat", self._sat_min, self._sat_max)
        self._add_range_row(color_grid, 2, "Value", self._val_min, self._val_max)
        right_layout.addWidget(color)

        shape = QGroupBox("Advanced: Blob Filter")
        shape_grid = QGridLayout(shape)
        self._min_area = self._spin(0, 999999, 20)
        self._max_area = self._spin(1, 9999999, 999999)
        self._roundness = self._double_spin(0.0, 1.0, 0.0, 0.05)
        shape_grid.addWidget(QLabel("Min Area"), 0, 0)
        shape_grid.addWidget(self._min_area, 0, 1)
        shape_grid.addWidget(QLabel("Max Area"), 0, 2)
        shape_grid.addWidget(self._max_area, 0, 3)
        shape_grid.addWidget(QLabel("Min Roundness"), 1, 0)
        shape_grid.addWidget(self._roundness, 1, 1)
        right_layout.addWidget(shape)

        crop = QGroupBox("Advanced: Search Bounds (%)")
        crop_grid = QGridLayout(crop)
        self._crop_top = self._double_spin(0.0, 95.0, 0.0, 1.0)
        self._crop_bottom = self._double_spin(0.0, 95.0, 0.0, 1.0)
        self._crop_left = self._double_spin(0.0, 95.0, 0.0, 1.0)
        self._crop_right = self._double_spin(0.0, 95.0, 0.0, 1.0)
        crop_grid.addWidget(QLabel("Top"), 0, 0)
        crop_grid.addWidget(self._crop_top, 0, 1)
        crop_grid.addWidget(QLabel("Bottom"), 0, 2)
        crop_grid.addWidget(self._crop_bottom, 0, 3)
        crop_grid.addWidget(QLabel("Left"), 1, 0)
        crop_grid.addWidget(self._crop_left, 1, 1)
        crop_grid.addWidget(QLabel("Right"), 1, 2)
        crop_grid.addWidget(self._crop_right, 1, 3)
        right_layout.addWidget(crop)

        self._preview_stats = QLabel("No preview yet")
        self._preview_stats.setStyleSheet(f"color: {COLORS['text_secondary']};")
        right_layout.addWidget(self._preview_stats)
        right_layout.addStretch()

        scroll.setWidget(right)
        splitter.addWidget(scroll)
        splitter.setSizes([760, 360])

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _add_range_row(
        layout: QGridLayout,
        row: int,
        label: str,
        minimum: QSpinBox,
        maximum: QSpinBox,
    ) -> None:
        layout.addWidget(QLabel(f"{label} Min"), row, 0)
        layout.addWidget(minimum, row, 1)
        layout.addWidget(QLabel(f"{label} Max"), row, 2)
        layout.addWidget(maximum, row, 3)

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    @staticmethod
    def _double_spin(
        minimum: float, maximum: float, value: float, step: float
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        spin.setValue(value)
        return spin

    def _set_rule(self, rule: VisualRule) -> None:
        set_editable_combo_text(
            self._group_combo,
            self._folder_index.canonicalize_existing(
                resource_group_path(rule.name)
            ),
        )
        self._name_edit.setText(resource_leaf_name(rule.name))
        type_idx = self._type_combo.findData(rule.type)
        self._type_combo.setCurrentIndex(max(0, type_idx))
        self._multi_ranges = [dict(item) for item in rule.color_ranges]
        self._update_multi_count_label()
        self._hue_min.setValue(rule.hue_min)
        self._hue_max.setValue(rule.hue_max)
        self._sat_min.setValue(rule.sat_min)
        self._sat_max.setValue(rule.sat_max)
        self._val_min.setValue(rule.value_min)
        self._val_max.setValue(rule.value_max)
        self._min_area.setValue(rule.min_area)
        self._max_area.setValue(rule.max_area)
        self._roundness.setValue(rule.min_roundness)
        self._crop_top.setValue(rule.crop_top_percent)
        self._crop_bottom.setValue(rule.crop_bottom_percent)
        self._crop_left.setValue(rule.crop_left_percent)
        self._crop_right.setValue(rule.crop_right_percent)

    def get_rule(self) -> VisualRule:
        rule_type = str(self._type_combo.currentData() or "color_blob")
        color_ranges = [dict(item) for item in self._multi_ranges]
        if rule_type == "multi_color_region" and not color_ranges:
            color_ranges = [self._current_color_range()]
        return VisualRule(
            name=join_resource_path(
                self._folder_index.canonicalize(
                    self._group_combo.currentText()
                ),
                self._name_edit.text(),
            ),
            type=rule_type,
            hue_min=self._hue_min.value(),
            hue_max=self._hue_max.value(),
            sat_min=self._sat_min.value(),
            sat_max=self._sat_max.value(),
            value_min=self._val_min.value(),
            value_max=self._val_max.value(),
            min_area=self._min_area.value(),
            max_area=self._max_area.value(),
            min_roundness=self._roundness.value(),
            crop_top_percent=self._crop_top.value(),
            crop_bottom_percent=self._crop_bottom.value(),
            crop_left_percent=self._crop_left.value(),
            crop_right_percent=self._crop_right.value(),
            color_ranges=color_ranges,
        )

    def accept(self) -> None:
        try:
            join_resource_path(
                self._folder_index.canonicalize(
                    self._group_combo.currentText()
                ),
                self._name_edit.text(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Rule Name", str(exc))
            self._name_edit.setFocus()
            return
        self._detach_capture_operation()
        super().accept()

    def _capture_screenshot(self) -> None:
        controller = self._capture_controller
        if (
            controller is None
            or not self._connected_capture_available
            or self._device_selector is None
        ):
            QMessageBox.information(self, "No Device", "No device selector is available.")
            return
        session = self._device_selector.get_selected_session()
        if session is None:
            QMessageBox.information(self, "No Device", "Connect a device first.")
            return
        self._capture_btn.setText("Capturing...")
        self._status_label.setText("Capturing screenshot...")
        try:
            self._capture_operation_id = controller.capture_session(session)
        except (CaptureBusyError, CaptureStartError) as exc:
            self._capture_btn.setText("Capture")
            self._status_label.setText(str(exc))
            QMessageBox.information(self, "Capture Busy", str(exc))

    def _on_capture_succeeded(
        self,
        operation_id: str,
        frame: object,
    ) -> None:
        if operation_id != self._capture_operation_id:
            return
        if not isinstance(frame, CaptureFrame):
            self._status_label.setText(
                "Screenshot operation failed: unexpected result format."
            )
            QMessageBox.warning(
                self,
                "Capture Failed",
                "The screenshot result had an unexpected format.",
            )
            return
        if frame.source.session_id is not None:
            current = (
                self._device_selector.get_selected_session()
                if self._device_selector is not None
                else None
            )
            if current is None or not capture_source_matches_session(
                frame.source,
                current,
            ):
                self._status_label.setText(
                    "Capture discarded because the selected target changed."
                )
                return
        self._set_screenshot(frame.png_bytes)

    def _on_capture_failed(self, operation_id: str, message: str) -> None:
        if operation_id != self._capture_operation_id:
            return
        self._status_label.setText(f"Screenshot operation failed: {message}")
        QMessageBox.warning(self, "Capture Failed", message)

    def _on_capture_cancelled(self, operation_id: str) -> None:
        if operation_id != self._capture_operation_id:
            return
        self._status_label.setText("Screenshot operation cancelled.")

    def _on_capture_finished(self, operation_id: str) -> None:
        if operation_id != self._capture_operation_id:
            return
        self._capture_operation_id = None
        self._capture_btn.setText("Capture")

    def _on_capture_busy_changed(self, busy: bool) -> None:
        capture_available = (
            self._connected_capture_available
            and self._device_selector is not None
        )
        self._capture_btn.setEnabled(capture_available and not busy)
        self._import_btn.setEnabled(
            self._capture_controller is not None and not busy
        )

    def _detach_capture_operation(self) -> None:
        """Logically cancel this dialog's work; the app owner retains its worker."""
        operation_id = self._capture_operation_id
        controller = self._capture_controller
        if controller is not None and controller.is_operation_active(operation_id):
            controller.cancel(operation_id)
        self._capture_operation_id = None
        self._capture_btn.setText("Capture")

    def reject(self) -> None:
        self._detach_capture_operation()
        super().reject()

    def closeEvent(self, event) -> None:
        self._detach_capture_operation()
        super().closeEvent(event)

    def _import_screenshot(self) -> None:
        controller = self._capture_controller
        if controller is None:
            self._status_label.setText(
                "Screenshot import is unavailable in this window."
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Screenshot", "", "Images (*.png *.jpg *.jpeg *.bmp *.tiff)"
        )
        if not path:
            return
        self._status_label.setText("Importing screenshot...")
        try:
            self._capture_operation_id = controller.import_file(path)
        except (CaptureBusyError, CaptureStartError) as exc:
            self._status_label.setText(str(exc))
            QMessageBox.information(self, "Capture Busy", str(exc))

    def _set_screenshot(self, screenshot: bytes) -> None:
        try:
            self._current_image = decode_screenshot(screenshot)
        except Exception as exc:
            QMessageBox.warning(self, "Screenshot Error", str(exc))
            return
        self._current_screenshot = screenshot
        pixmap = QPixmap()
        pixmap.loadFromData(screenshot)
        self._screenshot_widget.set_screenshot_pixmap(pixmap)
        h, w = self._current_image.shape[:2]
        self._status_label.setText(
            f"Screenshot loaded: {w} x {h}. Use Pick Pixel or Sample Area to teach the rule."
        )
        self._preview_stats.setText("No preview yet")

    def _on_selection_mode_changed(self) -> None:
        mode = self._selection_mode.currentData()
        if mode == "sample_area":
            self._screenshot_widget.set_crop_requires_shift(False)
            self._status_label.setText("Drag an area around one example object.")
        elif mode == "multi_sample_area":
            self._screenshot_widget.set_crop_requires_shift(False)
            self._status_label.setText("Drag each color area to add it to a multi-color rule.")
        elif mode == "search_area":
            self._screenshot_widget.set_crop_requires_shift(False)
            self._status_label.setText("Drag the screen region where Automadin should search.")
        else:
            self._screenshot_widget.set_crop_requires_shift(True)
            self._status_label.setText("Click a representative pixel on the object.")

    def _on_image_clicked(self, x: int, y: int, _pct_x: float, _pct_y: float) -> None:
        mode = self._selection_mode.currentData()
        if mode == "search_area":
            self._status_label.setText("Search Area mode uses drag selection, not click.")
            return
        self._type_combo.setCurrentIndex(max(0, self._type_combo.findData("color_blob")))
        self._sample_point(x, y)

    def _sample_point(self, x: int, y: int) -> None:
        if self._current_image is None:
            return
        height, width = self._current_image.shape[:2]
        radius = self._sample_radius.value()
        x1 = max(0, x - radius)
        x2 = min(width - 1, x + radius)
        y1 = max(0, y - radius)
        y2 = min(height - 1, y + radius)
        patch = self._current_image[y1:y2 + 1, x1:x2 + 1]
        self._apply_sample_patch(patch, sample_area=patch.shape[0] * patch.shape[1])
        self._status_label.setText(f"Sampled pixel area at ({x}, {y}). Preview updated.")

    def _apply_sample_patch(
        self,
        patch,
        sample_area: int | None = None,
        add_to_multi: bool = False,
    ) -> None:
        if patch is None or patch.size == 0:
            return
        self._last_sample_patch = patch.copy()
        self._last_sample_area = sample_area
        hsv_patch = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        pixels = hsv_patch.reshape(-1, 3).astype(np.float32)
        low_q, high_q, hue_pad, sat_pad, value_pad = self._COLOR_PRESETS[
            str(self._color_flex.currentData())
        ]
        hue = pixels[:, 0]
        sat = pixels[:, 1]
        value = pixels[:, 2]

        median_hue = float(np.median(hue))
        hue_delta = ((hue - median_hue + 90.0) % 180.0) - 90.0
        low_delta = float(np.percentile(hue_delta, low_q)) - hue_pad
        high_delta = float(np.percentile(hue_delta, high_q)) + hue_pad
        self._set_hue_range_from_bounds(median_hue + low_delta, median_hue + high_delta)

        self._sat_min.setValue(max(0, int(np.percentile(sat, low_q) - sat_pad)))
        self._sat_max.setValue(min(255, int(np.percentile(sat, high_q) + sat_pad)))
        self._val_min.setValue(max(0, int(np.percentile(value, low_q) - value_pad)))
        self._val_max.setValue(min(255, int(np.percentile(value, high_q) + value_pad)))
        self._apply_size_preset()
        self._apply_shape_preset()

        bgr = np.median(patch.reshape(-1, 3), axis=0)
        b, g, r = [int(channel) for channel in bgr]
        hsv = np.median(pixels, axis=0)
        self._swatch.setStyleSheet(
            f"background: {QColor(r, g, b).name()}; border: 1px solid {COLORS['border']};"
            f" color: {COLORS['text_on_accent']}; font-weight: 600;"
        )
        self._swatch.setText(f"H {int(hsv[0])}  S {int(hsv[1])}  V {int(hsv[2])}")
        if add_to_multi:
            self._multi_ranges.append(self._current_color_range())
            self._update_multi_count_label()
        self._preview_rule(show_messages=False)

    def _current_color_range(self) -> dict[str, int]:
        return {
            "hue_min": self._hue_min.value(),
            "hue_max": self._hue_max.value(),
            "sat_min": self._sat_min.value(),
            "sat_max": self._sat_max.value(),
            "value_min": self._val_min.value(),
            "value_max": self._val_max.value(),
        }

    def _add_current_range_to_multi(self) -> None:
        self._type_combo.setCurrentIndex(max(0, self._type_combo.findData("multi_color_region")))
        self._multi_ranges.append(self._current_color_range())
        self._update_multi_count_label()
        self._status_label.setText("Added current color range to multi-color rule.")
        self._preview_rule(show_messages=False)

    def _clear_multi_ranges(self) -> None:
        self._multi_ranges.clear()
        self._update_multi_count_label()
        self._status_label.setText("Cleared multi-color samples.")
        self._preview_rule(show_messages=False)

    def _update_multi_count_label(self) -> None:
        count = len(self._multi_ranges)
        self._multi_count_label.setText(f"{count} color{'s' if count != 1 else ''}")

    def _set_hue_range_from_bounds(self, low: float, high: float) -> None:
        if high - low >= 179.0:
            self._hue_min.setValue(0)
            self._hue_max.setValue(179)
            return
        low = low % 180.0
        high = high % 180.0
        if low <= high:
            self._hue_min.setValue(int(max(0, min(179, low))))
            self._hue_max.setValue(int(max(0, min(179, high))))
        else:
            self._hue_min.setValue(int(low))
            self._hue_max.setValue(int(high))

    def _apply_size_preset(self) -> None:
        preset = str(self._size_preset.currentData())
        if preset == "from_sample" and self._last_sample_area:
            area = max(1, int(self._last_sample_area))
            self._min_area.setValue(max(1, int(area * 0.20)))
            self._max_area.setValue(max(2, int(area * 4.0)))
            return
        if preset in self._SIZE_PRESETS:
            min_area, max_area = self._SIZE_PRESETS[preset]
            self._min_area.setValue(min_area)
            self._max_area.setValue(max_area)

    def _apply_shape_preset(self) -> None:
        preset = str(self._shape_preset.currentData())
        self._roundness.setValue(self._SHAPE_PRESETS.get(preset, 0.0))

    def _reapply_guided_controls(self) -> None:
        if self._last_sample_patch is not None:
            self._apply_sample_patch(self._last_sample_patch, self._last_sample_area)
        else:
            self._apply_size_preset()
            self._apply_shape_preset()
            self._preview_rule(show_messages=False)

    def _on_area_selected(self, rect: QRect) -> None:
        if self._current_image is None or rect.width() <= 0 or rect.height() <= 0:
            return
        mode = self._selection_mode.currentData()
        if mode == "sample_area":
            self._type_combo.setCurrentIndex(max(0, self._type_combo.findData("color_region")))
            self._sample_rect(rect)
        elif mode == "multi_sample_area":
            self._type_combo.setCurrentIndex(max(0, self._type_combo.findData("multi_color_region")))
            self._sample_rect(rect, add_to_multi=True)
        else:
            self._apply_search_area(rect)

    def _sample_rect(self, rect: QRect, add_to_multi: bool = False) -> None:
        if self._current_image is None:
            return
        height, width = self._current_image.shape[:2]
        x1 = max(0, min(width - 1, rect.left()))
        y1 = max(0, min(height - 1, rect.top()))
        x2 = max(x1 + 1, min(width, rect.left() + rect.width()))
        y2 = max(y1 + 1, min(height, rect.top() + rect.height()))
        patch = self._current_image[y1:y2, x1:x2]
        self._apply_sample_patch(
            patch,
            sample_area=(x2 - x1) * (y2 - y1),
            add_to_multi=add_to_multi,
        )
        if add_to_multi:
            self._status_label.setText(
                f"Added sample area {x2 - x1} x {y2 - y1}. Multi-color rule now has {len(self._multi_ranges)} colors."
            )
        else:
            self._status_label.setText(
                f"Sampled area {x2 - x1} x {y2 - y1}. Color and size suggestions updated."
            )

    def _apply_search_area(self, rect: QRect) -> None:
        if self._current_image is None:
            return
        height, width = self._current_image.shape[:2]
        self._crop_left.setValue(max(0.0, rect.left() / width * 100.0))
        self._crop_top.setValue(max(0.0, rect.top() / height * 100.0))
        right_gap = max(0, width - (rect.left() + rect.width()))
        bottom_gap = max(0, height - (rect.top() + rect.height()))
        self._crop_right.setValue(right_gap / width * 100.0)
        self._crop_bottom.setValue(bottom_gap / height * 100.0)
        self._status_label.setText("Crop bounds updated from selection. Preview to test.")
        self._preview_rule(show_messages=False)

    def _preview_rule(self, show_messages: bool = True) -> None:
        if self._current_image is None:
            if show_messages:
                QMessageBox.information(self, "No Screenshot", "Capture or import a screenshot first.")
            return
        try:
            blobs = detect_blobs(self._current_image, self.get_rule())
        except Exception as exc:
            if show_messages:
                QMessageBox.warning(self, "Preview Failed", str(exc))
            return

        self._screenshot_widget.clear_overlays()
        labels = [f"{idx + 1}" for idx in range(len(blobs))]
        self._screenshot_widget.draw_rect_overlays(
            [blob.rect for blob in blobs],
            labels=labels,
            color=COLORS["overlay_best"],
        )
        if blobs:
            areas = [blob.area for blob in blobs]
            count_hint = ""
            if len(blobs) > 40:
                count_hint = " Too many: tighten color, increase min size, or set a search area."
            elif len(blobs) == 1:
                count_hint = " Good for unique target; loosen if the object appears in multiple places."
            self._preview_stats.setText(
                f"Detected {len(blobs)} blob(s). Area range: {min(areas):.0f}-{max(areas):.0f}.{count_hint}"
            )
        else:
            self._preview_stats.setText(
                "Detected 0 blobs. Loosen color, lower min size, or sample a larger area."
            )

    def _clear_overlays(self) -> None:
        self._screenshot_widget.clear_overlays()
