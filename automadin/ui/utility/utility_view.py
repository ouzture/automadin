"""Utility view — interactive screenshot display + template image catalog.

Features:
- Screenshot capture from connected device
- Mouse hover shows pixel + percentage coordinates
- Ctrl+click copies coordinates to clipboard
- Shift+drag draws crop rectangle for template creation
- Save cropped region as template to database
- Template image catalog with find-on-screen overlay
"""

from __future__ import annotations

import os
from io import BytesIO

from PySide6.QtCore import QObject, QRect, QRunnable, QSize, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.session import DeviceSessionService
from automadin.models.flow import FlowCommand
from automadin.models.resource_path import (
    canonicalize_resource_group,
    find_resource_collision,
    join_resource_path,
    normalize_resource_path,
)
from automadin.services.database import DatabaseService
from automadin.services.find_image import FindImageService
from automadin.services.image_search_region import (
    ImageSearchRegionError,
    prepare_search_region,
    to_absolute_match,
)
from automadin.services.ocr import configured_tesseract_engine
from automadin.services.screen_text_reading import ScreenTextReadResult, ScreenTextReadingError, read_screen_text
from automadin.services.screen_text_options import (
    SCREEN_TEXT_DECIMAL_SEPARATOR_CHOICES,
    SCREEN_TEXT_DURATION_FORMAT_CHOICES,
    SCREEN_TEXT_RECOGNITION_MODE_CHOICES,
    SCREEN_TEXT_RESULT_TYPE_CHOICES,
    SCREEN_TEXT_VALUE_MATCH_MODE_CHOICES,
    ScreenTextChoice,
)
from automadin.services.screen_text_preprocessing import (
    SCREEN_TEXT_PREPROCESSING_PROFILES,
)
from automadin.services.screen_capture import (
    CaptureFrame,
    capture_source_matches_session,
)
from automadin.ui.capture_controller import (
    CaptureBusyError,
    CaptureOperationController,
    CaptureStartError,
)
from automadin.ui.image_group_filter import ImageGroupFilterBar
from automadin.ui.image_name_tree import (
    image_group_parts,
    image_leaf_name,
    image_matches_top_group,
    image_sort_key,
    top_level_image_groups,
)
from automadin.ui.resource_path_dialog import ResourcePathDialog
from automadin.ui.resource_path_dialog import ResourceFolderDialog
from automadin.services.image_files import normalize_image_bytes_to_png
from automadin.services.template_images import crop_template_png
from automadin.ui.theme import COLORS
from automadin.ui.widgets.interactive_screenshot import InteractiveScreenshotWidget

# ── Main Utility View ──────────────────────────────────────────────────


class _OcrTestSignals(QObject):
    completed = Signal(int, object, str)


def _add_screen_text_choices(
    combo: QComboBox,
    choices: tuple[ScreenTextChoice, ...],
) -> None:
    for choice in choices:
        combo.addItem(choice.label, choice.value)
        if choice.description:
            combo.setItemData(
                combo.count() - 1,
                choice.description,
                Qt.ItemDataRole.ToolTipRole,
            )


class _VisionOcrTask(QRunnable):
    """Run the configured OCR engine away from Vision Lab's GUI thread."""

    def __init__(
        self,
        screenshot: bytes,
        params: dict[str, object],
        image_names: list[str],
        database: DatabaseService,
        engine: object,
        screenshot_generation: int,
        ocr_options: dict[str, object],
    ) -> None:
        super().__init__()
        self._screenshot = screenshot
        self._params = dict(params)
        self._image_names = list(image_names)
        self._database = database
        self._engine = engine
        self._screenshot_generation = screenshot_generation
        self._ocr_options = dict(ocr_options)
        self.signals = _OcrTestSignals()

    @Slot()
    def run(self) -> None:
        params = {
            **self._params,
            **self._ocr_options,
            "allow_empty": True,
        }
        try:
            result = read_screen_text(
                self._screenshot,
                params,
                self._database,
                self._engine,
                resolve_value=lambda name, default, _type: params.get(name, default),
                image_names=self._image_names,
            )
        except ScreenTextReadingError as exc:
            self.signals.completed.emit(self._screenshot_generation, None, str(exc))
        except Exception:
            self.signals.completed.emit(
                self._screenshot_generation,
                None,
                "OCR preview could not complete. Check OCR setup and try again.",
            )
        else:
            self.signals.completed.emit(self._screenshot_generation, result, "")


class UtilityView(QWidget):
    """Screenshot viewer + template image catalog."""

    image_selected = Signal(str)  # emits image name
    resource_renamed = Signal(str, str, str)  # kind, old_name, new_name
    resource_deleted = Signal(str, str)  # kind, name

    def __init__(
        self,
        db: DatabaseService,
        session_service: DeviceSessionService,
        device_selector=None,
        *,
        capture_controller: CaptureOperationController | None = None,
    ) -> None:
        super().__init__()
        self._db = db
        self._device_selector = device_selector  # shared from MainWindow
        self._capture_controller = capture_controller
        self._find_service = FindImageService()
        self._ocr_engine = configured_tesseract_engine(db)
        self._current_screenshot: bytes | None = None
        self._current_capture_frame: CaptureFrame | None = None
        self._capture_operation_id: str | None = None
        self._ocr_test_active = False
        self._screenshot_generation = 0
        self._closed = False
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

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Main content area
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        content_layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: screenshot display ──
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        # Screenshot header with capture button
        screenshot_header = QHBoxLayout()
        screenshot_header.addWidget(QLabel("Screenshot"))
        screenshot_header.addStretch()

        self._clear_overlays_btn = QPushButton("Clear Overlays")
        self._clear_overlays_btn.setEnabled(False)
        self._clear_overlays_btn.setToolTip("Remove match highlight rectangles")
        self._clear_overlays_btn.clicked.connect(self._clear_overlays)
        screenshot_header.addWidget(self._clear_overlays_btn)

        self._import_screenshot_btn = QPushButton("Import")
        self._import_screenshot_btn.setToolTip("Load a screenshot from disk")
        self._import_screenshot_btn.clicked.connect(self._on_import_screenshot)
        screenshot_header.addWidget(self._import_screenshot_btn)

        self._export_screenshot_btn = QPushButton("Export")
        self._export_screenshot_btn.setToolTip("Save current screenshot to disk")
        self._export_screenshot_btn.setEnabled(False)
        self._export_screenshot_btn.clicked.connect(self._on_export_screenshot)
        screenshot_header.addWidget(self._export_screenshot_btn)

        self._capture_btn = QPushButton("Capture")
        self._capture_btn.setProperty("cssClass", "primary")
        self._capture_btn.setToolTip("Capture screenshot from connected device")
        self._capture_btn.clicked.connect(self._capture_screenshot)
        screenshot_header.addWidget(self._capture_btn)
        left_layout.addLayout(screenshot_header)

        # Interactive screenshot widget
        self._screenshot_widget = InteractiveScreenshotWidget()
        self._screenshot_widget.setText("No screenshot — connect a device and click Capture")
        self._screenshot_widget.coords_changed.connect(self._on_coords_changed)
        self._screenshot_widget.coords_cleared.connect(self._on_coords_cleared)
        self._screenshot_widget.crop_selected.connect(self._on_crop_selected)
        self._screenshot_widget.coord_clicked.connect(self._on_coord_clicked)
        left_layout.addWidget(self._screenshot_widget, stretch=1)

        # ── Coordinate info panel ──
        coord_panel = QFrame()
        coord_panel.setStyleSheet(
            f"QFrame {{ background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; "
            f"padding: 4px 8px; }}"
        )
        coord_layout = QHBoxLayout(coord_panel)
        coord_layout.setContentsMargins(8, 4, 8, 4)
        coord_layout.setSpacing(16)

        self._coord_pixel_label = QLabel("Pixel: —")
        self._coord_pixel_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; "
            f"font-family: 'Cascadia Code', 'Consolas', monospace; border: none;"
        )
        coord_layout.addWidget(self._coord_pixel_label)

        self._coord_pct_label = QLabel("Percent: —")
        self._coord_pct_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; "
            f"font-family: 'Cascadia Code', 'Consolas', monospace; border: none;"
        )
        coord_layout.addWidget(self._coord_pct_label)

        coord_layout.addStretch()

        hint_label = QLabel("Scroll: zoom | Mid-drag: pan | Dbl-click: reset | Shift+drag: crop | Ctrl+click: copy coords")
        hint_label.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; "
            f"font-style: italic; border: none;"
        )
        coord_layout.addWidget(hint_label)

        left_layout.addWidget(coord_panel)

        # ── Crop save bar (always visible) ──
        self._crop_bar = QFrame()
        self._crop_bar.setStyleSheet(
            f"QFrame {{ background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; "
            f"padding: 4px 8px; }}"
        )
        crop_layout = QGridLayout(self._crop_bar)
        crop_layout.setContentsMargins(8, 4, 8, 4)
        crop_layout.setHorizontalSpacing(8)
        crop_layout.setVerticalSpacing(4)

        self._crop_info_label = QLabel("Shift+drag on screenshot to select a crop region")
        self._crop_info_label.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; font-style: italic; border: none;"
        )
        self._crop_info_label.setWordWrap(True)
        self._crop_info_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        crop_layout.addWidget(self._crop_info_label, 0, 0, 1, 6)

        crop_group_label = QLabel("Folder")
        crop_group_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; border: none;"
        )
        crop_layout.addWidget(crop_group_label, 1, 0)

        self._crop_group_combo = QComboBox()
        self._crop_group_combo.setEditable(True)
        self._crop_group_combo.setMinimumWidth(100)
        self._crop_group_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._crop_group_combo.setEnabled(False)
        crop_layout.addWidget(self._crop_group_combo, 1, 1, 1, 2)

        crop_name_label = QLabel("Name")
        crop_name_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; border: none;"
        )
        crop_layout.addWidget(crop_name_label, 1, 3)

        self._crop_name_edit = QLineEdit()
        self._crop_name_edit.setPlaceholderText("template name...")
        self._crop_name_edit.setMinimumWidth(90)
        self._crop_name_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._crop_name_edit.setEnabled(False)
        crop_layout.addWidget(self._crop_name_edit, 1, 4)

        self._save_crop_btn = QPushButton("Save as Template")
        self._save_crop_btn.setProperty("cssClass", "success")
        self._save_crop_btn.setEnabled(False)
        self._save_crop_btn.clicked.connect(self._save_crop_as_template)
        crop_layout.addWidget(self._save_crop_btn, 1, 5)

        self._test_ocr_crop_btn = QPushButton("Test OCR")
        self._test_ocr_crop_btn.setEnabled(False)
        self._test_ocr_crop_btn.setToolTip(
            "Recognize text in the selected crop or Search Scope without changing a flow value"
        )
        self._test_ocr_crop_btn.clicked.connect(self._test_ocr_crop)
        crop_layout.addWidget(self._test_ocr_crop_btn, 2, 0)

        crop_layout.addWidget(QLabel("Region"), 2, 1)
        self._ocr_region_source_combo = QComboBox()
        self._ocr_region_source_combo.addItem("Selected crop", "selected_crop")
        self._ocr_region_source_combo.addItem("Search Scope", "search_scope")
        self._ocr_region_source_combo.setToolTip(
            "Search Scope reuses the fixed or image-anchor box in the panel on the right"
        )
        self._ocr_region_source_combo.currentIndexChanged.connect(
            self._on_ocr_region_source_changed
        )
        self._ocr_region_source_combo.setMinimumWidth(90)
        self._ocr_region_source_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        crop_layout.addWidget(self._ocr_region_source_combo, 2, 2)

        crop_layout.addWidget(QLabel("OCR result"), 2, 3)
        self._ocr_result_type_combo = QComboBox()
        _add_screen_text_choices(
            self._ocr_result_type_combo,
            SCREEN_TEXT_RESULT_TYPE_CHOICES,
        )
        self._ocr_result_type_combo.setMinimumWidth(75)
        self._ocr_result_type_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        crop_layout.addWidget(self._ocr_result_type_combo, 2, 4)

        crop_layout.addWidget(QLabel("Recognition"), 3, 0)
        self._ocr_recognition_mode_combo = QComboBox()
        _add_screen_text_choices(
            self._ocr_recognition_mode_combo,
            SCREEN_TEXT_RECOGNITION_MODE_CHOICES,
        )
        crop_layout.addWidget(self._ocr_recognition_mode_combo, 3, 1, 1, 2)

        crop_layout.addWidget(QLabel("Parsing"), 3, 3)
        self._ocr_value_match_mode_combo = QComboBox()
        _add_screen_text_choices(
            self._ocr_value_match_mode_combo,
            SCREEN_TEXT_VALUE_MATCH_MODE_CHOICES,
        )
        crop_layout.addWidget(self._ocr_value_match_mode_combo, 3, 4)

        crop_layout.addWidget(QLabel("Decimal"), 4, 0)
        self._ocr_decimal_separator_combo = QComboBox()
        _add_screen_text_choices(
            self._ocr_decimal_separator_combo,
            SCREEN_TEXT_DECIMAL_SEPARATOR_CHOICES,
        )
        self._ocr_decimal_separator_combo.setMinimumWidth(75)
        self._ocr_decimal_separator_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        crop_layout.addWidget(self._ocr_decimal_separator_combo, 4, 1, 1, 2)

        crop_layout.addWidget(QLabel("Clock format"), 4, 3)
        self._ocr_duration_format_combo = QComboBox()
        _add_screen_text_choices(
            self._ocr_duration_format_combo,
            SCREEN_TEXT_DURATION_FORMAT_CHOICES,
        )
        crop_layout.addWidget(self._ocr_duration_format_combo, 4, 4)

        crop_layout.addWidget(QLabel("Preprocessing"), 5, 0)
        self._ocr_preprocessing_combo = QComboBox()
        for profile in SCREEN_TEXT_PREPROCESSING_PROFILES:
            self._ocr_preprocessing_combo.addItem(profile.label, profile.key)
            self._ocr_preprocessing_combo.setItemData(
                self._ocr_preprocessing_combo.count() - 1,
                profile.description,
                Qt.ItemDataRole.ToolTipRole,
            )
        self._ocr_preprocessing_combo.setToolTip(
            "Use the same deterministic crop transforms available to Read Screen Text."
        )
        self._ocr_preprocessing_combo.setMinimumWidth(90)
        self._ocr_preprocessing_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        crop_layout.addWidget(self._ocr_preprocessing_combo, 5, 1, 1, 4)

        self._cancel_crop_btn = QPushButton("Cancel")
        self._cancel_crop_btn.setEnabled(False)
        self._cancel_crop_btn.clicked.connect(self._cancel_crop)
        crop_layout.addWidget(self._cancel_crop_btn, 3, 5)
        crop_layout.setColumnStretch(1, 1)
        crop_layout.setColumnStretch(2, 1)
        crop_layout.setColumnStretch(4, 1)

        left_layout.addWidget(self._crop_bar)

        # Match info
        self._match_label = QLabel("")
        self._match_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt;"
        )
        left_layout.addWidget(self._match_label)

        self._ocr_crop_preview = QLabel("OCR crop preview appears here")
        self._ocr_crop_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ocr_crop_preview.setFixedHeight(100)
        self._ocr_crop_preview.setStyleSheet(
            f"background-color: {COLORS['bg_surface']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; "
            f"color: {COLORS['text_tertiary']};"
        )
        left_layout.addWidget(self._ocr_crop_preview)

        self._ocr_result = QPlainTextEdit()
        self._ocr_result.setReadOnly(True)
        self._ocr_result.setPlaceholderText(
            "Select a crop with Shift+drag, then choose Test OCR Crop."
        )
        self._ocr_result.setFixedHeight(105)
        left_layout.addWidget(self._ocr_result)

        splitter.addWidget(left)

        # ── Right: image catalog ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        catalog_header = QHBoxLayout()
        catalog_header.addWidget(QLabel("Template Images"))
        catalog_header.addStretch()

        self._add_image_btn = QPushButton("Add")
        self._add_image_btn.clicked.connect(self._add_image)
        catalog_header.addWidget(self._add_image_btn)

        self._rename_image_btn = QPushButton("Rename")
        self._rename_image_btn.clicked.connect(self._rename_image)
        catalog_header.addWidget(self._rename_image_btn)

        self._delete_image_btn = QPushButton("Delete")
        self._delete_image_btn.setProperty("cssClass", "danger")
        self._delete_image_btn.clicked.connect(self._delete_image)
        catalog_header.addWidget(self._delete_image_btn)

        right_layout.addLayout(catalog_header)

        # Find on screen + settings row
        find_row = QHBoxLayout()
        self._find_btn = QPushButton("Find on Screen")
        self._find_btn.clicked.connect(self._find_on_screen)
        find_row.addWidget(self._find_btn)

        find_row.addWidget(QLabel("Threshold:"))
        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(0.01, 1.0)
        self._threshold_spin.setSingleStep(0.05)
        self._threshold_spin.setDecimals(2)
        self._threshold_spin.setValue(0.90)
        self._threshold_spin.setMinimumWidth(75)
        find_row.addWidget(self._threshold_spin)

        self._color_match_chk = QCheckBox("Color Match")
        find_row.addWidget(self._color_match_chk)

        find_row.addWidget(QLabel("Tolerance:"))
        self._tolerance_spin = QDoubleSpinBox()
        self._tolerance_spin.setRange(0.0, 1.0)
        self._tolerance_spin.setSingleStep(0.05)
        self._tolerance_spin.setDecimals(2)
        self._tolerance_spin.setValue(0.30)
        self._tolerance_spin.setMinimumWidth(75)
        self._tolerance_spin.setEnabled(False)
        find_row.addWidget(self._tolerance_spin)

        self._color_match_chk.toggled.connect(self._tolerance_spin.setEnabled)
        right_layout.addLayout(find_row)

        self._search_scope_group = QGroupBox("Search Scope")
        scope_layout = QVBoxLayout(self._search_scope_group)
        scope_layout.setContentsMargins(8, 8, 8, 8)
        scope_layout.setSpacing(5)

        scope_mode_row = QHBoxLayout()
        scope_mode_row.addWidget(QLabel("Scope:"))
        self._search_region_mode_combo = QComboBox()
        self._search_region_mode_combo.addItem("Full screenshot", "full")
        self._search_region_mode_combo.addItem("Fixed box", "fixed")
        self._search_region_mode_combo.addItem("Image anchor", "image_anchor")
        scope_mode_row.addWidget(self._search_region_mode_combo, 1)
        scope_mode_row.addWidget(QLabel("Units:"))
        self._search_coordinate_unit_combo = QComboBox()
        self._search_coordinate_unit_combo.addItem("Pixels", "pixels")
        self._search_coordinate_unit_combo.addItem("Percent", "percent")
        scope_mode_row.addWidget(self._search_coordinate_unit_combo)
        scope_layout.addLayout(scope_mode_row)

        self._fixed_scope_fields = QFrame()
        fixed_form = QFormLayout(self._fixed_scope_fields)
        fixed_form.setContentsMargins(0, 0, 0, 0)
        fixed_form.setHorizontalSpacing(6)
        fixed_form.setVerticalSpacing(3)
        self._search_start_x_spin = self._search_scope_spin(0, 100000, 0)
        self._search_start_y_spin = self._search_scope_spin(0, 100000, 0)
        self._search_end_x_spin = self._search_scope_spin(0, 100000, 0)
        self._search_end_y_spin = self._search_scope_spin(0, 100000, 0)
        fixed_form.addRow("Start X", self._search_start_x_spin)
        fixed_form.addRow("Start Y", self._search_start_y_spin)
        fixed_form.addRow("End X", self._search_end_x_spin)
        fixed_form.addRow("End Y", self._search_end_y_spin)
        scope_layout.addWidget(self._fixed_scope_fields)

        self._anchored_scope_fields = QFrame()
        anchor_form = QFormLayout(self._anchored_scope_fields)
        anchor_form.setContentsMargins(0, 0, 0, 0)
        anchor_form.setHorizontalSpacing(6)
        anchor_form.setVerticalSpacing(3)
        self._search_anchor_image_combo = QComboBox()
        anchor_form.addRow("Anchor image", self._search_anchor_image_combo)
        self._search_anchor_spin = QComboBox()
        for value, label in (
            ("top_left", "Top left"),
            ("top_right", "Top right"),
            ("bottom_left", "Bottom left"),
            ("bottom_right", "Bottom right"),
            ("center", "Center"),
        ):
            self._search_anchor_spin.addItem(label, value)
        anchor_form.addRow("Anchor point", self._search_anchor_spin)
        self._search_offset_x_spin = self._search_scope_spin(-100000, 100000, 0)
        self._search_offset_y_spin = self._search_scope_spin(-100000, 100000, 0)
        self._search_region_width_spin = self._search_scope_spin(0, 100000, 0)
        self._search_region_height_spin = self._search_scope_spin(0, 100000, 0)
        anchor_form.addRow("Offset X", self._search_offset_x_spin)
        anchor_form.addRow("Offset Y", self._search_offset_y_spin)
        anchor_form.addRow("Region width", self._search_region_width_spin)
        anchor_form.addRow("Region height", self._search_region_height_spin)
        self._search_anchor_threshold_spin = QDoubleSpinBox()
        self._search_anchor_threshold_spin.setRange(0.01, 1.0)
        self._search_anchor_threshold_spin.setDecimals(2)
        self._search_anchor_threshold_spin.setValue(0.90)
        anchor_form.addRow("Anchor threshold", self._search_anchor_threshold_spin)
        self._search_anchor_color_match_chk = QCheckBox("Color match")
        anchor_form.addRow("", self._search_anchor_color_match_chk)
        self._search_anchor_tolerance_spin = QDoubleSpinBox()
        self._search_anchor_tolerance_spin.setRange(0.0, 1.0)
        self._search_anchor_tolerance_spin.setDecimals(2)
        self._search_anchor_tolerance_spin.setValue(0.30)
        self._search_anchor_tolerance_spin.setEnabled(False)
        self._search_anchor_color_match_chk.toggled.connect(
            self._search_anchor_tolerance_spin.setEnabled
        )
        anchor_form.addRow("Anchor tolerance", self._search_anchor_tolerance_spin)
        scope_layout.addWidget(self._anchored_scope_fields)
        self._search_region_mode_combo.currentIndexChanged.connect(
            self._on_search_region_mode_changed
        )
        self._on_search_region_mode_changed()
        right_layout.addWidget(self._search_scope_group)

        # Filter / search
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter templates...")
        self._filter_edit.textChanged.connect(self._filter_images)
        right_layout.addWidget(self._filter_edit)

        self._group_filter = ImageGroupFilterBar()
        self._group_filter.group_changed.connect(lambda _group: self._refresh_image_list())
        right_layout.addWidget(self._group_filter)

        self._image_list = QTreeWidget()
        self._image_list.setHeaderHidden(True)
        self._image_list.setMinimumWidth(250)
        self._image_list.setRootIsDecorated(True)
        self._image_list.setIndentation(18)
        self._image_list.setIconSize(QSize(34, 34))
        self._image_list.currentItemChanged.connect(self._on_image_selected)
        right_layout.addWidget(self._image_list, stretch=1)

        # Image preview
        self._preview_label = QLabel("Select an image")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setFixedHeight(150)
        self._preview_label.setStyleSheet(
            f"background-color: {COLORS['bg_surface']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 8px;"
        )
        right_layout.addWidget(self._preview_label)

        splitter.addWidget(right)
        splitter.setSizes([650, 300])

        content_layout.addWidget(splitter)
        layout.addWidget(content, 1)
        self._refresh_image_list()

    @staticmethod
    def _search_scope_spin(minimum: float, maximum: float, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(2)
        spin.setValue(value)
        return spin

    def _on_search_region_mode_changed(self) -> None:
        mode = self._search_region_mode_combo.currentData()
        self._fixed_scope_fields.setVisible(mode == "fixed")
        self._anchored_scope_fields.setVisible(mode == "image_anchor")

    def _refresh_search_anchor_options(self) -> None:
        if not hasattr(self, "_search_anchor_image_combo"):
            return
        current = self._search_anchor_image_combo.currentData()
        self._search_anchor_image_combo.blockSignals(True)
        self._search_anchor_image_combo.clear()
        self._search_anchor_image_combo.addItem("Choose anchor image…", None)
        for info in self._db.list_images():
            name = str(info["name"])
            self._search_anchor_image_combo.addItem(name, name)
        index = self._search_anchor_image_combo.findData(current)
        self._search_anchor_image_combo.setCurrentIndex(max(0, index))
        self._search_anchor_image_combo.blockSignals(False)

    def _search_region_params(self) -> tuple[dict[str, object], list[str]]:
        mode = str(self._search_region_mode_combo.currentData())
        params: dict[str, object] = {
            "search_region_mode": mode,
            "search_coordinate_unit": self._search_coordinate_unit_combo.currentData(),
        }
        anchor_image_names: list[str] = []
        if mode == "fixed":
            params.update(
                {
                    "search_start_x": self._search_start_x_spin.value(),
                    "search_start_y": self._search_start_y_spin.value(),
                    "search_end_x": self._search_end_x_spin.value(),
                    "search_end_y": self._search_end_y_spin.value(),
                }
            )
        elif mode == "image_anchor":
            anchor_name = self._search_anchor_image_combo.currentData()
            if isinstance(anchor_name, str):
                anchor_image_names.append(anchor_name)
            params.update(
                {
                    "search_anchor": self._search_anchor_spin.currentData(),
                    "search_offset_x": self._search_offset_x_spin.value(),
                    "search_offset_y": self._search_offset_y_spin.value(),
                    "search_region_width": self._search_region_width_spin.value(),
                    "search_region_height": self._search_region_height_spin.value(),
                    "search_anchor_threshold": self._search_anchor_threshold_spin.value(),
                    "search_anchor_color_match": self._search_anchor_color_match_chk.isChecked(),
                    "search_anchor_color_tolerance": self._search_anchor_tolerance_spin.value(),
                }
            )
        return params, anchor_image_names

    # ── Coordinate display ─────────────────────────────────────────────

    def _on_coords_changed(self, px_x: int, px_y: int, pct_x: float, pct_y: float) -> None:
        self._coord_pixel_label.setText(f"Pixel: {px_x}, {px_y}")
        self._coord_pct_label.setText(f"Percent: {pct_x:.1f}%, {pct_y:.1f}%")

    def _on_coords_cleared(self) -> None:
        self._coord_pixel_label.setText("Pixel: —")
        self._coord_pct_label.setText("Percent: —")

    def _on_coord_clicked(self, px_x: int, px_y: int, pct_x: float, pct_y: float) -> None:
        """Copy coordinates to clipboard on Ctrl+click."""
        text = f"{px_x}, {px_y} | {pct_x:.1f}%, {pct_y:.1f}%"
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        self._match_label.setText(f"Copied to clipboard: {text}")

    # ── Crop selection ─────────────────────────────────────────────────

    def _on_crop_selected(self, rect: QRect) -> None:
        """Handle crop rectangle selection."""
        self._crop_bar.setStyleSheet(
            f"QFrame {{ background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['accent']}; border-radius: 6px; "
            f"padding: 4px 8px; }}"
        )
        self._crop_info_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; font-style: normal; border: none;"
        )
        self._crop_info_label.setText(
            f"Crop: {rect.width()}x{rect.height()} at ({rect.x()}, {rect.y()})"
        )
        self._crop_group_combo.setEnabled(True)
        self._crop_name_edit.setEnabled(True)
        self._save_crop_btn.setEnabled(True)
        self._cancel_crop_btn.setEnabled(True)
        self._pending_crop_rect = rect
        self._on_ocr_region_source_changed()
        self._clear_ocr_result()

    def _clear_ocr_result(self) -> None:
        self._ocr_result.clear()
        self._ocr_crop_preview.clear()
        self._ocr_crop_preview.setText("OCR crop preview appears here")

    def _on_ocr_region_source_changed(self) -> None:
        source = self._ocr_region_source_combo.currentData()
        has_crop = isinstance(getattr(self, "_pending_crop_rect", None), QRect)
        self._test_ocr_crop_btn.setEnabled(
            not self._ocr_test_active and (source == "search_scope" or has_crop)
        )
        self._clear_ocr_result()

    def _ocr_region_spec(self) -> tuple[dict[str, object], list[str]]:
        if self._current_screenshot is None:
            raise ValueError("Capture or import a screenshot first.")
        source = self._ocr_region_source_combo.currentData()
        if source == "selected_crop":
            rect = getattr(self, "_pending_crop_rect", None)
            if not isinstance(rect, QRect) or rect.width() <= 0 or rect.height() <= 0:
                raise ValueError("Select a non-empty crop with Shift+drag first.")
            return (
                {
                    "region_mode": "fixed",
                    "coordinate_unit": "pixels",
                    "start_x": rect.x(),
                    "start_y": rect.y(),
                    "end_x": rect.x() + rect.width(),
                    "end_y": rect.y() + rect.height(),
                },
                [],
            )

        search_params, image_names = self._search_region_params()
        search_mode = search_params["search_region_mode"]
        if search_mode == "full":
            from PIL import Image

            with Image.open(BytesIO(self._current_screenshot)) as image:
                width, height = image.size
            return (
                {
                    "region_mode": "fixed",
                    "coordinate_unit": "pixels",
                    "start_x": 0,
                    "start_y": 0,
                    "end_x": width,
                    "end_y": height,
                },
                [],
            )
        if search_mode == "fixed":
            return (
                {
                    "region_mode": "fixed",
                    "coordinate_unit": search_params["search_coordinate_unit"],
                    "start_x": search_params["search_start_x"],
                    "start_y": search_params["search_start_y"],
                    "end_x": search_params["search_end_x"],
                    "end_y": search_params["search_end_y"],
                },
                [],
            )
        return (
            {
                "region_mode": "image_anchor",
                "coordinate_unit": search_params["search_coordinate_unit"],
                "anchor": search_params["search_anchor"],
                "offset_x": search_params["search_offset_x"],
                "offset_y": search_params["search_offset_y"],
                "region_width": search_params["search_region_width"],
                "region_height": search_params["search_region_height"],
                "threshold": search_params["search_anchor_threshold"],
                "color_match": search_params["search_anchor_color_match"],
                "color_tolerance": search_params["search_anchor_color_tolerance"],
            },
            image_names,
        )

    def _test_ocr_crop(self) -> None:
        try:
            params, image_names = self._ocr_region_spec()
        except ValueError as exc:
            self._ocr_result.setPlainText(str(exc))
            return
        self._ocr_test_active = True
        self._test_ocr_crop_btn.setEnabled(False)
        for combo in self._ocr_option_combos():
            combo.setEnabled(False)
        self._ocr_result.setPlainText("Recognizing text…")
        ocr_options = {
            "result_type": str(self._ocr_result_type_combo.currentData()),
            "recognition_mode": str(
                self._ocr_recognition_mode_combo.currentData()
            ),
            "value_match_mode": str(
                self._ocr_value_match_mode_combo.currentData()
            ),
            "decimal_separator": str(
                self._ocr_decimal_separator_combo.currentData()
            ),
            "duration_format": str(self._ocr_duration_format_combo.currentData()),
            "preprocessing": str(self._ocr_preprocessing_combo.currentData()),
        }
        task = _VisionOcrTask(
            self._current_screenshot,
            params,
            image_names,
            self._db,
            self._ocr_engine,
            self._screenshot_generation,
            ocr_options,
        )
        task.signals.completed.connect(self._on_ocr_test_completed)
        QThreadPool.globalInstance().start(task)

    def _on_ocr_test_completed(
        self,
        screenshot_generation: int,
        result: object,
        error: str,
    ) -> None:
        self._ocr_test_active = False
        self._on_ocr_region_source_changed()
        for combo in self._ocr_option_combos():
            combo.setEnabled(True)
        if self._closed or screenshot_generation != self._screenshot_generation:
            return
        if error:
            self._ocr_result.setPlainText(error)
            return
        if not isinstance(result, ScreenTextReadResult):
            self._ocr_result.setPlainText("OCR preview returned an unexpected result.")
            return
        self._screenshot_widget.clear_overlays()
        region_rects = [
            (
                result.prepared.box.left,
                result.prepared.box.top,
                result.prepared.box.width,
                result.prepared.box.height,
            )
        ]
        region_labels = ["OCR region"]
        if result.prepared.matched_box is not None:
            region_rects.insert(
                0,
                (
                    result.prepared.matched_box.left,
                    result.prepared.matched_box.top,
                    result.prepared.matched_box.width,
                    result.prepared.matched_box.height,
                ),
            )
            region_labels.insert(0, f"Anchor: {result.prepared.matched_image}")
        self._screenshot_widget.draw_rect_overlays(
            region_rects, region_labels, color=COLORS["warning"]
        )
        self._clear_overlays_btn.setEnabled(True)
        pixmap = QPixmap()
        pixmap.loadFromData(result.prepared.crop_png)
        self._ocr_crop_preview.setPixmap(
            pixmap.scaled(
                self._ocr_crop_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._ocr_result.setPlainText(
            f"Raw text:\n{result.recognized_text}\n\n"
            f"Parsed {self._ocr_result_type_combo.currentText()}: {result.value!r}\n\n"
            f"Recognition: {self._ocr_recognition_mode_combo.currentText()}\n"
            f"Parsing: {self._ocr_value_match_mode_combo.currentText()}\n"
            f"Preprocessing: {self._ocr_preprocessing_combo.currentText()}\n\n"
            "No flow Variables were changed."
        )

    def _ocr_option_combos(self) -> tuple[QComboBox, ...]:
        return (
            self._ocr_result_type_combo,
            self._ocr_recognition_mode_combo,
            self._ocr_value_match_mode_combo,
            self._ocr_decimal_separator_combo,
            self._ocr_duration_format_combo,
            self._ocr_preprocessing_combo,
        )

    def _save_crop_as_template(self) -> None:
        """Save the cropped region as a template image."""
        if self._current_screenshot is None:
            return
        rect = getattr(self, "_pending_crop_rect", None)
        if rect is None:
            return

        group = canonicalize_resource_group(
            self._crop_group_combo.currentText(),
            self._db.list_resource_paths(),
            preferred_groups=self._db.list_resource_folders(),
        )
        leaf_name = self._crop_name_edit.text().strip()
        if not leaf_name:
            QMessageBox.warning(self, "Name Required", "Enter a template name.")
            return
        try:
            name = join_resource_path(group, leaf_name)
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Invalid Name",
                str(exc),
            )
            return
        collision = find_resource_collision(
            name, [str(item["name"]) for item in self._db.list_images()]
        )
        if collision is not None:
            reply = QMessageBox.question(
                self,
                "Overwrite Template",
                f"A template named '{collision}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            name = collision

        try:
            data, w, h = crop_template_png(
                self._current_screenshot,
                rect.x(),
                rect.y(),
                rect.width(),
                rect.height(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Selection", str(exc))
            return

        frame = self._current_capture_frame
        self._db.save_image(
            name,
            data,
            w,
            h,
            source_surface_width=frame.width if frame is not None else 0,
            source_surface_height=frame.height if frame is not None else 0,
            source_coordinate_space=(
                frame.source.coordinate_space.value if frame is not None else ""
            ),
        )
        self._refresh_image_list()
        self._cancel_crop()
        self._match_label.setText(f"Template '{name}' saved ({w}x{h})")

    def _cancel_crop(self) -> None:
        """Cancel crop selection."""
        self._crop_bar.setStyleSheet(
            f"QFrame {{ background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; "
            f"padding: 4px 8px; }}"
        )
        self._crop_info_label.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; font-style: italic; border: none;"
        )
        self._crop_info_label.setText("Shift+drag on screenshot to select a crop region")
        self._crop_name_edit.clear()
        self._crop_group_combo.setEnabled(False)
        self._crop_name_edit.setEnabled(False)
        self._save_crop_btn.setEnabled(False)
        self._cancel_crop_btn.setEnabled(False)
        self._pending_crop_rect = None
        self._on_ocr_region_source_changed()
        self._clear_ocr_result()
        # Clear the crop rect visual without resetting zoom
        self._screenshot_widget.clear_crop_selection()

    # ── Image catalog ──────────────────────────────────────────────────

    def _refresh_image_list(self) -> None:
        self._image_list.clear()
        filter_text = self._filter_edit.text().strip().lower() if hasattr(self, "_filter_edit") else ""
        group_items: dict[str, QTreeWidgetItem] = {}
        group_font = QFont()
        group_font.setBold(True)

        def ensure_group(
            parent: QTreeWidgetItem | None,
            path: str,
            label: str,
        ) -> QTreeWidgetItem:
            key = path.casefold()
            existing = group_items.get(key)
            if existing is not None:
                return existing
            item = QTreeWidgetItem([label])
            item.setFont(0, group_font)
            item.setForeground(0, QBrush(QColor(COLORS["text_secondary"])))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item.setExpanded(bool(filter_text))
            if parent is None:
                self._image_list.addTopLevelItem(item)
            else:
                parent.addChild(item)
            group_items[key] = item
            return item

        folder_index = self._db.workspace_folder_index()
        images = self._db.list_images()
        display_paths = {
            str(info["name"]): folder_index.display_path(info["name"])
            for info in images
        }
        images.sort(
            key=lambda info: image_sort_key(
                display_paths.get(str(info["name"]), str(info["name"]))
            )
        )
        if hasattr(self, "_group_filter"):
            self._group_filter.set_groups(
                top_level_image_groups(list(display_paths.values()))
            )
        self._refresh_crop_group_options()
        self._refresh_search_anchor_options()
        active_group = (
            self._group_filter.active_group if hasattr(self, "_group_filter") else ""
        )
        for info in images:
            name = info["name"]
            display_path = display_paths.get(str(name), str(name))
            if active_group and not image_matches_top_group(
                display_path, active_group
            ):
                continue
            if filter_text and filter_text not in display_path.lower():
                continue
            parent = None
            path_parts: list[str] = []
            for part in image_group_parts(display_path):
                path_parts.append(part)
                parent = ensure_group(parent, "/".join(path_parts), part)

            text = (
                f"{image_leaf_name(display_path)}  "
                f"({info['width']}x{info['height']})"
            )
            item = QTreeWidgetItem([text])
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            item.setToolTip(0, display_path)
            thumb_bytes = self._db.get_image_thumbnail(name, size=34)
            if thumb_bytes:
                pixmap = QPixmap()
                pixmap.loadFromData(thumb_bytes)
                item.setIcon(0, QIcon(pixmap))
            if parent is None:
                self._image_list.addTopLevelItem(item)
            else:
                parent.addChild(item)
        if filter_text or active_group:
            self._image_list.expandAll()
        else:
            self._image_list.collapseAll()

    def _refresh_crop_group_options(self) -> None:
        if not hasattr(self, "_crop_group_combo"):
            return
        current = self._crop_group_combo.currentText().strip()
        folder_index = self._db.workspace_folder_index()
        current = folder_index.canonicalize_existing(current)
        groups = folder_index.folders
        self._crop_group_combo.blockSignals(True)
        self._crop_group_combo.clear()
        self._crop_group_combo.addItem("")
        for group in groups:
            self._crop_group_combo.addItem(group)
        if current:
            index = self._crop_group_combo.findText(current)
            if index >= 0:
                self._crop_group_combo.setCurrentIndex(index)
            else:
                self._crop_group_combo.setEditText(current)
        self._crop_group_combo.blockSignals(False)

    def _selected_image_name(self) -> str | None:
        item = self._image_list.currentItem()
        if item is None:
            return None
        name = item.data(0, Qt.ItemDataRole.UserRole)
        return str(name) if name else None

    def _build_image_flow_map(self) -> dict[str, list[str]]:
        """Build a reverse map: image_name → [flow_names that reference it]."""
        result: dict[str, list[str]] = {}
        for flow_name in self._db.list_flows():
            flow = self._db.load_flow(flow_name)
            if not flow:
                continue
            used: set[str] = set()
            self._collect_image_refs(flow.commands, used)
            for img in used:
                result.setdefault(img, []).append(flow_name)
        return result

    def _collect_image_refs(self, commands: list[FlowCommand], out: set[str]) -> None:
        for cmd in commands:
            name = cmd.params.get("image_name")
            if name:
                out.add(name)
            images = cmd.params.get("images")
            if isinstance(images, list):
                out.update(str(n) for n in images if n)
            self._collect_image_refs(cmd.children, out)
            self._collect_image_refs(cmd.else_children, out)

    def _filter_images(self, _text: str) -> None:
        self._refresh_image_list()

    def _add_image(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add Template Images",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)",
        )
        if not paths:
            return
        folder = ResourceFolderDialog.get_folder(
            title="Add Images",
            known_paths=self._db.list_resource_paths(),
            known_folders=self._db.list_resource_folders(),
            parent=self,
        )
        if folder is None:
            return

        existing_names = [str(item["name"]) for item in self._db.list_images()]
        prepared: list[tuple[str, str, str | None]] = []
        seen: set[str] = set()
        duplicate_count = 0
        for path in paths:
            leaf = os.path.splitext(os.path.basename(path))[0]
            try:
                name = join_resource_path(folder, leaf)
            except ValueError:
                duplicate_count += 1
                continue
            key = normalize_resource_path(name).casefold()
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            prepared.append(
                (name, path, find_resource_collision(name, existing_names))
            )

        collisions = [collision for _, _, collision in prepared if collision]
        replace_existing = False
        if collisions:
            sample = "\n".join(f"  • {name}" for name in collisions[:8])
            more = f"\n  …and {len(collisions) - 8} more" if len(collisions) > 8 else ""
            reply = QMessageBox.question(
                self,
                "Images Already Exist",
                f"{len(collisions)} image(s) already exist:\n\n{sample}{more}\n\n"
                "Replace them? Choose No to skip them.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return
            replace_existing = reply == QMessageBox.StandardButton.Yes

        imported = 0
        skipped = duplicate_count
        failures: list[str] = []
        for name, path, collision in prepared:
            if collision and not replace_existing:
                skipped += 1
                continue
            target_name = collision or name
            try:
                with open(path, "rb") as file:
                    data, width, height = normalize_image_bytes_to_png(file.read())
                self._db.save_image(target_name, data, width, height)
                imported += 1
            except Exception as exc:
                failures.append(f"{os.path.basename(path)}: {exc}")
        self._refresh_image_list()
        summary = f"Added {imported} image(s)."
        if skipped:
            summary += f" Skipped {skipped}."
        if failures:
            QMessageBox.warning(
                self,
                "Some Images Could Not Be Added",
                summary + "\n\n" + "\n".join(failures[:8]),
            )
        else:
            self._match_label.setText(summary)

    def _delete_image(self) -> None:
        name = self._selected_image_name()
        if name is None:
            return
        affected = self._find_flows_using_image(name)
        if affected:
            flow_list = "\n".join(f"  • {f}" for f in affected)
            msg = (
                f"'{name}' is used in {len(affected)} flow(s):\n\n{flow_list}\n\n"
                "Deleting will leave broken image references in those flows.\n"
                "Delete anyway?"
            )
        else:
            msg = f"Delete template '{name}'?"
        reply = QMessageBox.question(
            self,
            "Delete Template",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self._db.delete_image(name):
                self.resource_deleted.emit("image", name)
            self._refresh_image_list()
            self._preview_label.setText("Select an image")

    def _rename_image(self) -> None:
        old_name = self._selected_image_name()
        if old_name is None:
            return
        new_name = ResourcePathDialog.get_path(
            title="Move or Rename Image",
            item_label="Image",
            current_path=old_name,
            known_paths=self._db.list_resource_paths(),
            known_folders=self._db.list_resource_folders(),
            existing_paths=[str(item["name"]) for item in self._db.list_images()],
            parent=self,
        )
        if new_name is None or new_name == old_name:
            return
        if self._db.load_image(new_name) is not None:
            QMessageBox.warning(self, "Name Taken", f"A template named '{new_name}' already exists.")
            return

        affected = self._find_flows_using_image(old_name)
        if affected:
            flow_list = "\n".join(f"  • {f}" for f in affected)
            reply = QMessageBox.question(
                self, "Template In Use",
                f"'{old_name}' is used in {len(affected)} flow(s):\n\n{flow_list}\n\n"
                "These flows will be updated to use the new name. Proceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._db.rename_image(old_name, new_name)
        self.resource_renamed.emit("image", old_name, new_name)

        self._refresh_image_list()
        suffix = f" (updated {len(affected)} flow(s))" if affected else ""
        self._match_label.setText(f"Renamed '{old_name}' → '{new_name}'{suffix}")

    def refresh_image_library(self) -> None:
        """Public slot used when images change elsewhere in the application."""
        self._refresh_image_list()

    # ── Flow impact utilities ───────────────────────────────────────────

    def _find_flows_using_image(self, image_name: str) -> list[str]:
        """Return names of all flows that reference the given image template."""
        return self._db.image_references(image_name)

    def _update_image_refs(
        self, commands: list[FlowCommand], old_name: str, new_name: str
    ) -> None:
        for cmd in commands:
            if cmd.params.get("image_name") == old_name:
                cmd.params["image_name"] = new_name
            images = cmd.params.get("images")
            if isinstance(images, list):
                cmd.params["images"] = [new_name if n == old_name else n for n in images]
            self._update_image_refs(cmd.children, old_name, new_name)
            self._update_image_refs(cmd.else_children, old_name, new_name)

    def _on_image_selected(
        self, current: QTreeWidgetItem | None, _prev: QTreeWidgetItem | None
    ) -> None:
        if current is None:
            return
        name = current.data(0, Qt.ItemDataRole.UserRole)
        if not name:
            return
        name = str(name)
        data = self._db.load_image(name)
        if data:
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            scaled = pixmap.scaled(
                self._preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._preview_label.setPixmap(scaled)
        self.image_selected.emit(name)

    # ── Screenshot capture ─────────────────────────────────────────────

    def _show_capture_status(self, message: str) -> None:
        """Show capture state without hiding a still-usable previous frame."""
        if self._current_screenshot is None:
            self._screenshot_widget.clear_screenshot()
            self._screenshot_widget.setText(message)
            self._match_label.setText(message)
            return
        self._match_label.setText(f"{message} Previous screenshot kept.")

    def _capture_screenshot(self) -> None:
        controller = self._capture_controller
        if controller is None:
            self._show_capture_status(
                "Screenshot capture is unavailable in this window."
            )
            return
        if self._device_selector is None:
            self._show_capture_status("No device selector available.")
            return
        session = self._device_selector.get_selected_session()
        if session is None:
            self._show_capture_status("Connect a device first.")
            return

        self._capture_btn.setEnabled(False)
        self._capture_btn.setText("Capturing...")
        self._show_capture_status("Capturing screenshot...")

        try:
            self._capture_operation_id = controller.capture_session(session)
        except (CaptureBusyError, CaptureStartError) as exc:
            # A rejected start never emits busy_changed because no worker was
            # created. Restore the complete idle state here so one rejected
            # attempt cannot strand the Capture button disabled or leave a
            # phantom operation ID behind.
            self._capture_operation_id = None
            self._capture_btn.setText("Capture")
            self._on_capture_busy_changed(controller.is_busy)
            self._show_capture_status(str(exc))

    def _on_capture_succeeded(
        self,
        operation_id: str,
        frame: object,
    ) -> None:
        if operation_id != self._capture_operation_id:
            return
        if not isinstance(frame, CaptureFrame):
            self._show_capture_status(
                "Capture failed: the result had an unexpected format."
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
                self._show_capture_status(
                    "Capture discarded because the selected target changed."
                )
                return
        self.set_screenshot(frame.png_bytes, capture_frame=frame)
        self._match_label.setText(
            f"Loaded {frame.width} × {frame.height} from {frame.source.label}"
        )

    def _on_capture_failed(self, operation_id: str, error_msg: str) -> None:
        if operation_id != self._capture_operation_id:
            return
        self._show_capture_status(f"Capture failed: {error_msg}")

    def _on_capture_cancelled(self, operation_id: str) -> None:
        if operation_id != self._capture_operation_id:
            return
        self._show_capture_status("Screenshot operation cancelled.")

    def _on_capture_finished(self, operation_id: str) -> None:
        if operation_id != self._capture_operation_id:
            return
        self._capture_operation_id = None
        self._capture_btn.setText("Capture")

    def _on_capture_busy_changed(self, busy: bool) -> None:
        controller_available = self._capture_controller is not None
        self._capture_btn.setEnabled(
            controller_available
            and self._device_selector is not None
            and not busy
        )
        self._import_screenshot_btn.setEnabled(
            controller_available and not busy
        )

    def closeEvent(self, event) -> None:
        self._closed = True
        super().closeEvent(event)

    def shutdown(self, wait_ms: int = 250) -> bool:
        """Cancel this view's operation and retain shared workers until safe."""
        controller = self._capture_controller
        if controller is None:
            return True
        operation_id = self._capture_operation_id
        if not controller.is_operation_active(operation_id):
            return True
        controller.cancel(operation_id)
        return controller.shutdown(wait_ms)

    def set_screenshot(
        self,
        screenshot_bytes: bytes,
        *,
        capture_frame: CaptureFrame | None = None,
    ) -> None:
        """Set screenshot from external source."""
        self._screenshot_generation += 1
        self._current_screenshot = screenshot_bytes
        self._current_capture_frame = capture_frame
        pixmap = QPixmap()
        pixmap.loadFromData(screenshot_bytes)
        self._screenshot_widget.set_screenshot_pixmap(pixmap)
        self._cancel_crop()  # clear any pending crop
        self._export_screenshot_btn.setEnabled(True)

    def _on_import_screenshot(self) -> None:
        """Load a bounded image file through the shared capture controller."""
        controller = self._capture_controller
        if controller is None:
            self._show_capture_status(
                "Screenshot import is unavailable in this window."
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Screenshot", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)"
        )
        if not path:
            return
        self._show_capture_status("Importing screenshot...")
        try:
            self._capture_operation_id = controller.import_file(path)
        except (CaptureBusyError, CaptureStartError) as exc:
            self._show_capture_status(str(exc))

    def _on_export_screenshot(self) -> None:
        """Save the current screenshot bytes to a PNG file on disk."""
        from PySide6.QtWidgets import QFileDialog
        if not self._current_screenshot:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Screenshot", "screenshot.png", "PNG (*.png)"
        )
        if not path:
            return
        with open(path, "wb") as f:
            f.write(self._current_screenshot)

    def _clear_overlays(self) -> None:
        self._screenshot_widget.clear_overlays()
        self._clear_overlays_btn.setEnabled(False)
        self._match_label.setText("")

    # ── Find on screen ─────────────────────────────────────────────────

    def _find_on_screen(self) -> None:
        if self._current_screenshot is None:
            self._match_label.setText("No screenshot captured")
            return
        name = self._selected_image_name()
        if name is None:
            self._match_label.setText("Select a template image first")
            return
        template = self._db.load_image(name)
        if template is None:
            return

        threshold = self._threshold_spin.value()
        color_match = self._color_match_chk.isChecked()
        color_tolerance = self._tolerance_spin.value()
        region_params, anchor_image_names = self._search_region_params()

        # Clear previous overlays without resetting zoom
        self._screenshot_widget.clear_overlays()

        try:
            search_surface = prepare_search_region(
                self._current_screenshot,
                region_params,
                self._db,
                resolve_value=lambda _name, default, _type: default,
                anchor_image_names=anchor_image_names,
            )
        except ImageSearchRegionError as exc:
            self._match_label.setText(f"Search scope unavailable: {exc}")
            return

        scope_rects = [
            (
                search_surface.box.left,
                search_surface.box.top,
                search_surface.box.width,
                search_surface.box.height,
            )
        ]
        scope_labels = ["Search scope"]
        if search_surface.matched_box is not None:
            scope_rects.insert(
                0,
                (
                    search_surface.matched_box.left,
                    search_surface.matched_box.top,
                    search_surface.matched_box.width,
                    search_surface.matched_box.height,
                ),
            )
            scope_labels.insert(0, f"Anchor: {search_surface.matched_image}")
        self._screenshot_widget.draw_rect_overlays(
            scope_rects, scope_labels, color=COLORS["warning"]
        )
        self._clear_overlays_btn.setEnabled(True)

        results = self._find_service.find_all(
            search_surface.png, template, threshold, color_match, color_tolerance
        )
        results = [to_absolute_match(result, search_surface) for result in results]
        if results:
            best = max(results, key=lambda r: r.confidence)
            self._match_label.setText(
                f"Found {len(results)} match(es) — best at "
                f"({best.center_x}, {best.center_y}) "
                f"confidence: {best.confidence:.2%}"
            )
            self._screenshot_widget.draw_match_overlays(results)
            self._clear_overlays_btn.setEnabled(True)
        else:
            result = self._find_service.find(
                search_surface.png, template, threshold, color_match, color_tolerance
            )
            if result:
                result = to_absolute_match(result, search_surface)
                self._match_label.setText(
                    f"Found at ({result.center_x}, {result.center_y}) "
                    f"confidence: {result.confidence:.2%}"
                )
                self._screenshot_widget.draw_match_overlays([result])
                self._clear_overlays_btn.setEnabled(True)
            else:
                self._match_label.setText("Not found on screen")
