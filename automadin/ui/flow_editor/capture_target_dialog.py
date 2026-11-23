"""Focused screenshot-to-template workflow for the flow editor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from uuid import uuid4

from PySide6.QtCore import QObject, QRect, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QPixmap, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from automadin.models.resource_path import (
    canonicalize_resource_group,
    find_resource_collision,
    join_resource_path,
    normalize_resource_path,
)
from automadin.services.database import DatabaseService, ImageRevision
from automadin.services.find_image import FindImageService, MatchResult
from automadin.services.screen_capture import (
    CaptureFrame,
    capture_source_matches_session,
)
from automadin.services.template_images import crop_template_png
from automadin.ui.capture_controller import (
    CaptureBusyError,
    CaptureOperationController,
    CaptureStartError,
)
from automadin.ui.theme import COLORS
from automadin.ui.editable_combo import set_editable_combo_text
from automadin.ui.widgets.interactive_screenshot import InteractiveScreenshotWidget


_MATCH_TEST_MAX_CANDIDATES = 2_000
_MATCH_TEST_MAX_MATCHES = 25


class _MatchTestSignals(QObject):
    completed = Signal(str, object, bool)  # operation_id, list[MatchResult], failed


class _MatchTestTask(QRunnable):
    """Bounded template test executed outside the GUI thread."""

    def __init__(
        self,
        operation_id: str,
        screenshot: bytes,
        template: bytes,
        *,
        threshold: float,
        color_match: bool,
        color_tolerance: float,
    ) -> None:
        super().__init__()
        self.operation_id = operation_id
        self.screenshot = screenshot
        self.template = template
        self.threshold = threshold
        self.color_match = color_match
        self.color_tolerance = color_tolerance
        self.signals = _MatchTestSignals()

    @Slot()
    def run(self) -> None:
        try:
            results = FindImageService().find_all(
                self.screenshot,
                self.template,
                threshold=self.threshold,
                color_match=self.color_match,
                color_tolerance=self.color_tolerance,
                max_candidates=_MATCH_TEST_MAX_CANDIDATES,
                max_matches=_MATCH_TEST_MAX_MATCHES,
            )
        except Exception:
            self.signals.completed.emit(self.operation_id, [], True)
            return
        self.signals.completed.emit(self.operation_id, results, False)


@dataclass(frozen=True, slots=True)
class CaptureTargetDraft:
    """Validated, in-memory result returned for guarded persistence."""

    name: str
    png_bytes: bytes
    width: int
    height: int
    source_label: str
    parameter_updates: tuple[tuple[str, object], ...]
    replace_revision: ImageRevision | None = None
    source_surface_width: int = 0
    source_surface_height: int = 0
    source_coordinate_space: str = ""

    def updates_dict(self) -> dict[str, object]:
        return dict(self.parameter_updates)


class CaptureTargetDialog(QDialog):
    """Capture/import, crop, test, name, and stage one template image."""

    def __init__(
        self,
        db: DatabaseService,
        *,
        device_selector: Any | None = None,
        capture_controller: CaptureOperationController | None = None,
        supported_params: Iterable[str] = (),
        current_params: dict[str, Any] | None = None,
        suggested_group: str = "",
        current_document_name: str = "",
        current_document_images: Iterable[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Capture Target Image")
        self.setModal(True)
        self.setObjectName("captureTargetDialog")

        self._db = db
        self._device_selector = device_selector
        self._capture_controller = capture_controller
        self._supported_params = frozenset(str(name) for name in supported_params)
        self._current_params = dict(current_params or {})
        self._suggested_group = suggested_group
        self._current_document_name = current_document_name
        self._current_document_images = frozenset(
            normalize_resource_path(name).casefold()
            for name in current_document_images
            if normalize_resource_path(name)
        )
        self._current_frame: CaptureFrame | None = None
        self._selection_rect: QRect | None = None
        self._capture_operation_id: str | None = None
        self._starting_operation = False
        self._draft: CaptureTargetDraft | None = None
        self._syncing_bounds = False
        self._closed = False
        self._match_test_operation_id: str | None = None
        self._match_test_stale = False
        self._match_tasks: dict[str, _MatchTestTask] = {}
        self._image_names = [
            str(item["name"]) for item in self._db.list_images()
        ]

        self._build_ui()
        self._configure_geometry()
        self._connect_capture_controller()
        self._connect_device_selector()
        self._update_name_preview()
        if self._capture_controller is not None:
            self._on_capture_busy_changed(self._capture_controller.is_busy)
        else:
            self._update_actions()
        if self._capture_btn.isEnabled():
            self._capture_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        elif self._import_btn.isEnabled():
            self._import_btn.setFocus(Qt.FocusReason.OtherFocusReason)

    @property
    def draft(self) -> CaptureTargetDraft | None:
        return self._draft

    def set_commit_error(self, message: str) -> None:
        """Keep staged inputs intact so a guarded write can be retried."""
        self._draft = None
        self._set_status(message, error=True)
        self._refresh_image_names()
        self._update_name_preview()
        self._update_actions()

    # ── Construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        self._title_label = QLabel("Capture Target Image")
        self._title_label.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 14pt; font-weight: 650;"
        )
        root.addWidget(self._title_label)

        self._instruction_label = QLabel(
            "Capture the current target, then drag around the image you want "
            "Automadin to find."
        )
        self._instruction_label.setWordWrap(True)
        self._instruction_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
        )
        root.addWidget(self._instruction_label)

        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        source_caption = QLabel("Source")
        source_caption.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; "
            "font-weight: 650;"
        )
        source_row.addWidget(source_caption)
        self._source_label = QLabel("No screenshot loaded")
        self._source_label.setObjectName("captureSourceLabel")
        self._source_label.setWordWrap(True)
        self._source_label.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 10.5pt;"
        )
        source_row.addWidget(self._source_label, 1)
        root.addLayout(source_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self._capture_btn = QPushButton("Capture Current Screen")
        self._capture_btn.setObjectName("captureCurrentScreenButton")
        self._capture_btn.setProperty("cssClass", "primary")
        self._capture_btn.setMinimumHeight(36)
        self._capture_btn.setShortcut("Alt+C")
        self._capture_btn.clicked.connect(self._capture_current_screen)
        toolbar.addWidget(self._capture_btn)

        self._import_btn = QPushButton("Import Image...")
        self._import_btn.setObjectName("importCaptureImageButton")
        self._import_btn.setMinimumHeight(36)
        self._import_btn.setShortcut("Alt+I")
        self._import_btn.clicked.connect(self._import_image)
        toolbar.addWidget(self._import_btn)
        toolbar.addStretch()

        self._operation_label = QLabel("Ready")
        self._operation_label.setObjectName("captureOperationLabel")
        self._operation_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
        )
        toolbar.addWidget(self._operation_label)
        root.addLayout(toolbar)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setObjectName("captureTargetSplitter")
        self._splitter.setChildrenCollapsible(False)

        canvas_host = QWidget()
        canvas_layout = QVBoxLayout(canvas_host)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(7)

        self._screenshot_widget = InteractiveScreenshotWidget()
        self._screenshot_widget.setObjectName("captureTargetScreenshot")
        self._screenshot_widget.setAccessibleName("Screenshot crop workspace")
        self._screenshot_widget.setAccessibleDescription(
            "Drag over the screenshot to select the image Automadin should find."
        )
        self._screenshot_widget.set_crop_requires_shift(False)
        self._screenshot_widget.setText(
            "No screenshot yet — capture a connected target or import an image"
        )
        self._screenshot_widget.crop_selected.connect(self._on_crop_selected)
        canvas_layout.addWidget(self._screenshot_widget, 1)

        self._canvas_hint = QLabel(
            "Drag: select target  ·  Wheel: zoom  ·  Middle-drag: pan  ·  "
            "Double-click: fit"
        )
        self._canvas_hint.setWordWrap(True)
        self._canvas_hint.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt;"
        )
        canvas_layout.addWidget(self._canvas_hint)
        self._splitter.addWidget(canvas_host)

        self._settings_scroll = QScrollArea()
        self._settings_scroll.setObjectName("captureTargetSettingsScroll")
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._settings_scroll.setMinimumWidth(310)

        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(10, 0, 4, 4)
        settings_layout.setSpacing(10)
        self._build_selection_section(settings_layout)
        self._add_separator(settings_layout)
        self._build_name_section(settings_layout)
        if self._has_detection_settings:
            self._add_separator(settings_layout)
            self._build_match_section(settings_layout)
        settings_layout.addStretch()
        self._settings_scroll.setWidget(settings)
        self._splitter.addWidget(self._settings_scroll)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([860, 340])
        root.addWidget(self._splitter, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self._status_label = QLabel("")
        self._status_label.setObjectName("captureTargetStatus")
        self._status_label.setWordWrap(True)
        self._status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._status_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
        )
        footer.addWidget(self._status_label, 1)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setMinimumHeight(36)
        self._cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self._cancel_btn)

        self._save_btn = QPushButton("Save & Use")
        self._save_btn.setObjectName("saveAndUseCaptureTargetButton")
        self._save_btn.setProperty("cssClass", "primary")
        self._save_btn.setMinimumHeight(36)
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self.accept)
        footer.addWidget(self._save_btn)
        root.addLayout(footer)

        save_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        save_shortcut.activated.connect(self.accept)

        QWidget.setTabOrder(self._capture_btn, self._import_btn)
        QWidget.setTabOrder(self._import_btn, self._x_spin)
        QWidget.setTabOrder(self._x_spin, self._y_spin)
        QWidget.setTabOrder(self._y_spin, self._width_spin)
        QWidget.setTabOrder(self._width_spin, self._height_spin)
        QWidget.setTabOrder(self._height_spin, self._clear_selection_btn)
        QWidget.setTabOrder(self._clear_selection_btn, self._group_combo)
        QWidget.setTabOrder(self._group_combo, self._name_edit)
        if self._has_detection_settings:
            match_order: list[QWidget] = [self._name_edit]
            if "threshold" in self._supported_params:
                match_order.append(self._threshold_spin)
            if "color_match" in self._supported_params:
                match_order.append(self._color_match_check)
            if "color_tolerance" in self._supported_params:
                match_order.append(self._tolerance_spin)
            match_order.extend(
                [self._test_btn, self._cancel_btn, self._save_btn]
            )
            for current, following in zip(match_order, match_order[1:]):
                QWidget.setTabOrder(current, following)
        else:
            QWidget.setTabOrder(self._name_edit, self._cancel_btn)
            QWidget.setTabOrder(self._cancel_btn, self._save_btn)

    @property
    def _has_detection_settings(self) -> bool:
        return bool(
            self._supported_params
            & {"threshold", "color_match", "color_tolerance"}
        )

    @staticmethod
    def _section_heading(text: str) -> QLabel:
        heading = QLabel(text)
        heading.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 11.25pt; "
            "font-weight: 650;"
        )
        return heading

    @staticmethod
    def _add_separator(layout: QVBoxLayout) -> None:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {COLORS['border']};")
        layout.addWidget(line)

    def _build_selection_section(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section_heading("Selection"))

        self._selection_label = QLabel("Drag on the screenshot to select a target.")
        self._selection_label.setObjectName("captureSelectionLabel")
        self._selection_label.setWordWrap(True)
        self._selection_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
        )
        layout.addWidget(self._selection_label)

        self._preview_label = QLabel("No selection")
        self._preview_label.setObjectName("captureTargetPreview")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumHeight(132)
        self._preview_label.setStyleSheet(
            f"background-color: {COLORS['input_bg']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; "
            f"color: {COLORS['text_tertiary']}; font-size: 10pt;"
        )
        layout.addWidget(self._preview_label)

        bounds = QGridLayout()
        bounds.setHorizontalSpacing(8)
        bounds.setVerticalSpacing(6)
        self._x_spin = self._new_bound_spin("Selection X coordinate")
        self._y_spin = self._new_bound_spin("Selection Y coordinate")
        self._width_spin = self._new_bound_spin("Selection width")
        self._height_spin = self._new_bound_spin("Selection height")
        for spin in (
            self._x_spin,
            self._y_spin,
            self._width_spin,
            self._height_spin,
        ):
            spin.valueChanged.connect(self._on_bound_value_changed)

        for index, (label_text, spin) in enumerate(
            (
                ("X", self._x_spin),
                ("Y", self._y_spin),
                ("Width", self._width_spin),
                ("Height", self._height_spin),
            )
        ):
            field = QWidget()
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(4)
            label = QLabel(label_text)
            label.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10pt;"
            )
            field_layout.addWidget(label)
            field_layout.addWidget(spin)
            bounds.addWidget(field, index // 2, index % 2)
        layout.addLayout(bounds)

        self._clear_selection_btn = QPushButton("Clear Selection")
        self._clear_selection_btn.setMinimumHeight(34)
        self._clear_selection_btn.clicked.connect(self._clear_selection)
        layout.addWidget(self._clear_selection_btn)

    @staticmethod
    def _new_bound_spin(accessible_name: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 0)
        spin.setMinimumHeight(34)
        spin.setAccessibleName(accessible_name)
        return spin

    def _build_name_section(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section_heading("Save as template"))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self._group_combo = QComboBox()
        self._group_combo.setObjectName("captureTargetFolder")
        self._group_combo.setEditable(True)
        self._group_combo.setMinimumHeight(34)
        folders = self._db.list_resource_folders()
        self._group_combo.addItems(folders)
        set_editable_combo_text(self._group_combo, self._suggested_group)
        self._group_combo.editTextChanged.connect(self._update_name_preview)
        form.addRow("Folder", self._group_combo)

        self._name_edit = QLineEdit()
        self._name_edit.setObjectName("captureTargetName")
        self._name_edit.setMinimumHeight(34)
        self._name_edit.setPlaceholderText("e.g. collect reward")
        self._name_edit.textChanged.connect(self._update_name_preview)
        form.addRow("Name", self._name_edit)
        layout.addLayout(form)

        self._path_preview = QLabel("Template path: —")
        self._path_preview.setObjectName("captureTargetPathPreview")
        self._path_preview.setWordWrap(True)
        self._path_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._path_preview.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; "
            "font-family: 'Cascadia Code', 'Consolas', monospace;"
        )
        layout.addWidget(self._path_preview)

        self._name_validation = QLabel("")
        self._name_validation.setObjectName("captureTargetNameValidation")
        self._name_validation.setWordWrap(True)
        self._name_validation.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt;"
        )
        layout.addWidget(self._name_validation)

    def _build_match_section(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section_heading("Match test"))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(8)

        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setObjectName("captureTargetThreshold")
        self._threshold_spin.setRange(0.01, 1.0)
        self._threshold_spin.setSingleStep(0.01)
        self._threshold_spin.setDecimals(2)
        self._threshold_spin.setMinimumHeight(34)
        self._threshold_spin.setValue(
            self._numeric_param("threshold", 0.90)
        )
        self._threshold_spin.valueChanged.connect(
            self._on_match_settings_changed
        )
        if "threshold" in self._supported_params:
            form.addRow("Threshold", self._threshold_spin)

        self._color_match_check = QCheckBox("Require similar colors")
        self._color_match_check.setObjectName("captureTargetColorMatch")
        self._color_match_check.setChecked(
            bool(self._current_params.get("color_match", False))
        )
        self._color_match_check.toggled.connect(
            self._on_match_settings_changed
        )
        if "color_match" in self._supported_params:
            form.addRow("Color match", self._color_match_check)

        self._tolerance_spin = QDoubleSpinBox()
        self._tolerance_spin.setObjectName("captureTargetColorTolerance")
        self._tolerance_spin.setRange(0.0, 1.0)
        self._tolerance_spin.setSingleStep(0.01)
        self._tolerance_spin.setDecimals(2)
        self._tolerance_spin.setMinimumHeight(34)
        self._tolerance_spin.setValue(
            self._numeric_param("color_tolerance", 0.30)
        )
        self._tolerance_spin.valueChanged.connect(
            self._on_match_settings_changed
        )
        self._tolerance_spin.setEnabled(self._color_match_check.isChecked())
        self._color_match_check.toggled.connect(self._tolerance_spin.setEnabled)
        if "color_tolerance" in self._supported_params:
            form.addRow("Color tolerance", self._tolerance_spin)
        layout.addLayout(form)

        self._test_btn = QPushButton("Test on Full Screenshot")
        self._test_btn.setMinimumHeight(34)
        self._test_btn.setShortcut("Alt+T")
        self._test_btn.clicked.connect(self._test_selection)
        layout.addWidget(self._test_btn)

        self._test_result_label = QLabel("Testing is optional.")
        self._test_result_label.setWordWrap(True)
        self._test_result_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
        )
        layout.addWidget(self._test_result_label)

    def _numeric_param(self, name: str, default: float) -> float:
        value = self._current_params.get(name, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _configure_geometry(self) -> None:
        screen = self.screen()
        if screen is None and self.parentWidget() is not None:
            screen = self.parentWidget().screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(1180, 720)
            self.setMinimumSize(780, 560)
            return

        available = screen.availableGeometry()
        width = min(1280, max(760, available.width() - 48))
        height = min(800, max(560, available.height() - 48))
        minimum_width = min(920, max(680, available.width() - 32))
        minimum_height = min(600, max(520, available.height() - 32))
        self.setMinimumSize(minimum_width, minimum_height)
        self.resize(width, height)
        self.move(
            available.center().x() - width // 2,
            available.center().y() - height // 2,
        )

    # ── Capture lifecycle ─────────────────────────────────────────

    def _connect_capture_controller(self) -> None:
        controller = self._capture_controller
        if controller is None:
            return
        controller.busy_changed.connect(self._on_capture_busy_changed)
        controller.capture_succeeded.connect(self._on_capture_succeeded)
        controller.capture_failed.connect(self._on_capture_failed)
        controller.capture_cancelled.connect(self._on_capture_cancelled)
        controller.capture_finished.connect(self._on_capture_finished)

    def _connect_device_selector(self) -> None:
        selector = self._device_selector
        if selector is None:
            return
        connected = getattr(selector, "session_connected", None)
        disconnected = getattr(selector, "session_disconnected", None)
        if connected is not None and hasattr(connected, "connect"):
            connected.connect(self._on_target_changed)
        if disconnected is not None and hasattr(disconnected, "connect"):
            disconnected.connect(self._on_target_changed)

    def _capture_current_screen(self) -> None:
        controller = self._capture_controller
        if controller is None:
            self._set_status(
                "Screenshot capture is unavailable in this window.",
                error=True,
            )
            return
        session = self._selected_session()
        if session is None:
            self._set_status("Connect to a target first.", error=True)
            return
        self._operation_label.setText("Capturing...")
        self._capture_btn.setText("Capturing...")
        self._set_status(
            "Capturing the selected target..."
            + (" The previous screenshot will stay available if this fails."
               if self._current_frame is not None else "")
        )
        self._starting_operation = True
        try:
            self._capture_operation_id = controller.capture_session(session)
        except (CaptureBusyError, CaptureStartError) as exc:
            self._capture_operation_id = None
            self._capture_btn.setText("Capture Current Screen")
            self._operation_label.setText(
                "Busy" if controller.is_busy else "Error"
            )
            self._set_status(str(exc), error=True)
        finally:
            self._starting_operation = False
        self._update_actions()

    def _import_image(self) -> None:
        controller = self._capture_controller
        if controller is None:
            self._set_status(
                "Screenshot import is unavailable in this window.",
                error=True,
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Screenshot",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)",
        )
        if not path:
            return
        self._operation_label.setText("Importing...")
        self._set_status(
            "Importing the selected image..."
            + (" The previous screenshot will stay available if this fails."
               if self._current_frame is not None else "")
        )
        self._starting_operation = True
        try:
            self._capture_operation_id = controller.import_file(path)
        except (CaptureBusyError, CaptureStartError) as exc:
            self._capture_operation_id = None
            self._operation_label.setText(
                "Busy" if controller.is_busy else "Error"
            )
            self._set_status(str(exc), error=True)
        finally:
            self._starting_operation = False
        self._update_actions()

    def _on_capture_succeeded(self, operation_id: str, frame: object) -> None:
        if self._closed or operation_id != self._capture_operation_id:
            return
        if not isinstance(frame, CaptureFrame):
            self._set_status(
                "Capture failed because the result was not a valid image.",
                error=True,
            )
            return
        if frame.source.session_id is not None:
            session = self._selected_session()
            if session is None or not capture_source_matches_session(
                frame.source,
                session,
            ):
                self._set_status(
                    "Capture discarded because the selected target changed. "
                    "Try again.",
                    error=True,
                )
                return
        pixmap = QPixmap()
        if not pixmap.loadFromData(frame.png_bytes):
            self._set_status(
                "Capture failed because the image could not be displayed.",
                error=True,
            )
            return
        self._current_frame = frame
        self._screenshot_widget.set_screenshot_pixmap(pixmap)
        self._source_label.setText(
            f"{frame.source.kind.value.title()} · {frame.source.label}"
        )
        self._operation_label.setText("Ready")
        self._set_status(
            f"Loaded {frame.width} × {frame.height}. Drag around the target image."
        )
        self._configure_bounds(frame.width, frame.height)
        self._clear_selection()

    def _on_capture_failed(self, operation_id: str, message: str) -> None:
        if self._closed or operation_id != self._capture_operation_id:
            return
        suffix = (
            " Previous screenshot kept." if self._current_frame is not None else ""
        )
        self._set_status(f"Capture failed: {message}{suffix}", error=True)
        self._operation_label.setText("Error")

    def _on_capture_cancelled(self, operation_id: str) -> None:
        if self._closed or operation_id != self._capture_operation_id:
            return
        self._set_status("Screenshot operation cancelled.")
        self._operation_label.setText("Ready")

    def _on_capture_finished(self, operation_id: str) -> None:
        if operation_id != self._capture_operation_id:
            return
        self._capture_operation_id = None
        self._capture_btn.setText(
            "Recapture Current Screen"
            if self._current_frame is not None
            else "Capture Current Screen"
        )
        self._update_actions()

    def _on_capture_busy_changed(self, busy: bool) -> None:
        if self._closed:
            return
        owns_operation = (
            self._capture_operation_id is not None or self._starting_operation
        )
        if busy and not owns_operation:
            self._operation_label.setText("Busy")
            self._set_status("Another screenshot operation is finishing.")
        elif not busy and not owns_operation:
            self._operation_label.setText("Ready")
            if (
                self._status_label.text()
                == "Another screenshot operation is finishing."
            ):
                self._set_status(
                    "Ready to recapture or adjust the current selection."
                    if self._current_frame is not None
                    else "Ready to capture or import a screenshot."
                )
        self._update_actions()

    def _selected_session(self) -> Any | None:
        selector = self._device_selector
        if selector is None or not hasattr(selector, "get_selected_session"):
            return None
        return selector.get_selected_session()

    def _on_target_changed(self, *_args: object) -> None:
        self._update_actions()

    def _detach_owned_operation(self) -> None:
        operation_id = self._capture_operation_id
        self._capture_operation_id = None
        controller = self._capture_controller
        if (
            operation_id is not None
            and controller is not None
            and controller.is_operation_active(operation_id)
        ):
            controller.cancel(operation_id)

    # ── Selection and preview ─────────────────────────────────────

    def _configure_bounds(self, width: int, height: int) -> None:
        self._syncing_bounds = True
        try:
            self._x_spin.setRange(0, max(0, width - 1))
            self._y_spin.setRange(0, max(0, height - 1))
            self._width_spin.setRange(1, max(1, width))
            self._height_spin.setRange(1, max(1, height))
        finally:
            self._syncing_bounds = False

    def _on_crop_selected(self, rect: QRect) -> None:
        frame = self._current_frame
        if frame is None:
            return
        clamped = self._clamped_rect(rect, frame.width, frame.height)
        self._set_selection(clamped)

    @staticmethod
    def _clamped_rect(rect: QRect, width: int, height: int) -> QRect:
        x = max(0, min(rect.x(), max(0, width - 1)))
        y = max(0, min(rect.y(), max(0, height - 1)))
        crop_width = max(0, min(rect.width(), width - x))
        crop_height = max(0, min(rect.height(), height - y))
        return QRect(x, y, crop_width, crop_height)

    def _set_selection(self, rect: QRect) -> None:
        self._selection_rect = QRect(rect)
        self._syncing_bounds = True
        try:
            self._x_spin.setValue(rect.x())
            self._y_spin.setValue(rect.y())
            self._width_spin.setMaximum(
                max(1, self._current_frame.width - rect.x())
                if self._current_frame is not None
                else 1
            )
            self._height_spin.setMaximum(
                max(1, self._current_frame.height - rect.y())
                if self._current_frame is not None
                else 1
            )
            self._width_spin.setValue(max(1, rect.width()))
            self._height_spin.setValue(max(1, rect.height()))
        finally:
            self._syncing_bounds = False
        self._screenshot_widget.set_crop_selection_original(rect)
        self._refresh_selection_preview(invalidate_test=True)

    def _on_bound_value_changed(self, _value: int) -> None:
        if self._syncing_bounds or self._current_frame is None:
            return
        self._syncing_bounds = True
        try:
            max_width = max(1, self._current_frame.width - self._x_spin.value())
            max_height = max(1, self._current_frame.height - self._y_spin.value())
            self._width_spin.setMaximum(max_width)
            self._height_spin.setMaximum(max_height)
            self._width_spin.setValue(min(self._width_spin.value(), max_width))
            self._height_spin.setValue(min(self._height_spin.value(), max_height))
            rect = QRect(
                self._x_spin.value(),
                self._y_spin.value(),
                self._width_spin.value(),
                self._height_spin.value(),
            )
        finally:
            self._syncing_bounds = False
        self._selection_rect = rect
        self._screenshot_widget.set_crop_selection_original(rect)
        self._refresh_selection_preview(invalidate_test=True)

    def _refresh_selection_preview(self, *, invalidate_test: bool) -> None:
        frame = self._current_frame
        rect = self._selection_rect
        if frame is None or rect is None or rect.width() < 1 or rect.height() < 1:
            self._selection_label.setText(
                "Drag on the screenshot to select a target."
            )
            self._preview_label.clear()
            self._preview_label.setText("No selection")
            self._update_actions()
            return

        self._selection_label.setText(
            f"Selection: {rect.width()} × {rect.height()} px "
            f"at {rect.x()}, {rect.y()}"
        )
        pixmap = QPixmap()
        if pixmap.loadFromData(frame.png_bytes):
            cropped = pixmap.copy(rect)
            preview = cropped.scaled(
                self._preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._preview_label.setPixmap(preview)
        if self._has_detection_settings and invalidate_test:
            self._invalidate_match_result("Selection changed — test again.")
        self._update_actions()

    def _clear_selection(self) -> None:
        self._selection_rect = None
        self._screenshot_widget.clear_crop_selection()
        self._selection_label.setText(
            "Drag on the screenshot to select a target."
        )
        self._preview_label.clear()
        self._preview_label.setText("No selection")
        if self._has_detection_settings:
            self._invalidate_match_result("Testing is optional.")
        self._update_actions()

    # ── Name, collision, and match test ───────────────────────────

    def _candidate_name(self) -> tuple[str | None, str | None]:
        leaf = self._name_edit.text().strip()
        if not leaf:
            return None, "Enter a template name."
        try:
            group = canonicalize_resource_group(
                self._group_combo.currentText(),
                self._db.list_resource_paths(),
                preferred_groups=self._db.list_resource_folders(),
            )
            return join_resource_path(group, leaf), None
        except ValueError as exc:
            return None, str(exc)
        except Exception:
            return None, "The template path could not be validated."

    def _update_name_preview(self, *_args: object) -> None:
        if not hasattr(self, "_path_preview"):
            return
        name, error = self._candidate_name()
        if error:
            self._path_preview.setText("Template path: —")
            self._name_validation.setText(error)
            self._name_validation.setStyleSheet(
                f"color: {COLORS['text_primary']}; font-size: 10pt;"
            )
        else:
            assert name is not None
            self._path_preview.setText(f"Template path: {name}")
            collision = find_resource_collision(name, self._image_names)
            if collision is None:
                self._name_validation.setText("Name is available.")
                color = COLORS["text_secondary"]
            else:
                self._name_validation.setText(
                    f"Existing image: {collision}. Replacement requires confirmation."
                )
                color = COLORS["text_primary"]
            self._name_validation.setStyleSheet(
                f"color: {color}; font-size: 10pt;"
            )
        self._update_actions()

    def _refresh_image_names(self) -> None:
        try:
            self._image_names = [
                str(item["name"]) for item in self._db.list_images()
            ]
        except Exception:
            self._image_names = []

    def _review_collision(
        self,
        candidate: str,
    ) -> tuple[str, ImageRevision | None] | None:
        try:
            revision = self._db.image_revision(candidate)
        except Exception:
            self._set_status(
                "The image library changed and could not be reviewed. Try again.",
                error=True,
            )
            return None
        if revision is None:
            return candidate, None

        try:
            references = self._db.image_references(revision.name)
        except Exception:
            self._set_status(
                "The image's saved-flow usage could not be reviewed. "
                "No replacement was made.",
                error=True,
            )
            return None
        reference_text = ""
        current_document_uses_image = (
            normalize_resource_path(revision.name).casefold()
            in self._current_document_images
        )
        if current_document_uses_image:
            reference_text = (
                "\n\nCurrent open flow:\n"
                f"• {self._current_document_name or 'Untitled'} "
                "(current unsaved state)"
            )
        if references:
            visible = references[:6]
            reference_text += "\n\nUsed by saved flows:\n" + "\n".join(
                f"• {name}" for name in visible
            )
            if len(references) > len(visible):
                reference_text += (
                    f"\n• and {len(references) - len(visible)} more"
                )

        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Replace Existing Image?")
        message.setText(
            f"'{revision.name}' already exists ({revision.width} × "
            f"{revision.height})."
        )
        message.setInformativeText(
            "Replacing it with the current "
            f"{self._selection_rect.width() if self._selection_rect else 0} × "
            f"{self._selection_rect.height() if self._selection_rect else 0} "
            "selection updates every flow that uses this image."
            f"{reference_text}"
        )
        choose_name = message.addButton(
            "Use Different Name",
            QMessageBox.ButtonRole.RejectRole,
        )
        replace = message.addButton(
            "Replace Existing",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel = message.addButton(
            QMessageBox.StandardButton.Cancel
        )
        message.setDefaultButton(choose_name)
        message.exec()
        clicked = message.clickedButton()
        if clicked is replace:
            return revision.name, revision
        if clicked is choose_name:
            self._name_edit.setFocus()
            self._name_edit.selectAll()
        elif clicked is cancel:
            self._set_status("Save cancelled. The current crop is unchanged.")
        return None

    def _test_selection(self) -> None:
        frame = self._current_frame
        rect = self._selection_rect
        if (
            frame is None
            or rect is None
            or self._match_test_operation_id is not None
        ):
            return
        try:
            template, _, _ = crop_template_png(
                frame.png_bytes,
                rect.x(),
                rect.y(),
                rect.width(),
                rect.height(),
            )
        except Exception:
            self._test_result_label.setText(
                "The selection could not be tested. Adjust it and try again."
            )
            self._test_result_label.setStyleSheet(
                f"color: {COLORS['text_primary']}; font-size: 10.5pt;"
            )
            return

        operation_id = str(uuid4())
        task = _MatchTestTask(
            operation_id,
            frame.png_bytes,
            template,
            threshold=self._threshold_spin.value(),
            color_match=self._color_match_check.isChecked(),
            color_tolerance=self._tolerance_spin.value(),
        )
        task.signals.completed.connect(self._on_match_test_completed)
        self._match_tasks[operation_id] = task
        self._match_test_operation_id = operation_id
        self._match_test_stale = False
        self._screenshot_widget.clear_overlays()
        self._test_result_label.setText("Testing the selection...")
        self._test_result_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
        )
        self._update_actions()
        QThreadPool.globalInstance().start(task)

    def _on_match_test_completed(
        self,
        operation_id: str,
        result: object,
        failed: bool,
    ) -> None:
        self._match_tasks.pop(operation_id, None)
        if operation_id != self._match_test_operation_id:
            return
        self._match_test_operation_id = None
        stale = self._match_test_stale
        self._match_test_stale = False
        if self._closed:
            return
        if stale:
            self._test_result_label.setText("Settings changed — test again.")
            self._test_result_label.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
            )
        elif failed:
            self._test_result_label.setText(
                "The selection could not be tested. Adjust it and try again."
            )
            self._test_result_label.setStyleSheet(
                f"color: {COLORS['text_primary']}; font-size: 10.5pt;"
            )
        elif (
            isinstance(result, list)
            and result
            and all(isinstance(match, MatchResult) for match in result)
        ):
            best_confidence = max(match.confidence for match in result)
            if len(result) >= _MATCH_TEST_MAX_MATCHES:
                summary = f"At least {len(result)} distinct matches"
            elif len(result) == 1:
                summary = "Found 1 distinct match"
            else:
                summary = f"Found {len(result)} distinct matches"
            guidance = (
                " — consider a larger or more distinctive crop"
                if len(result) > 1
                else ""
            )
            self._test_result_label.setText(
                f"{summary} · best {best_confidence:.1%} confidence{guidance}"
            )
            self._test_result_label.setStyleSheet(
                f"color: {COLORS['text_primary']}; font-size: 10.5pt;"
            )
            self._screenshot_widget.draw_match_overlays(result)
        else:
            self._test_result_label.setText(
                "No match found at the current settings."
            )
            self._test_result_label.setStyleSheet(
                f"color: {COLORS['text_primary']}; font-size: 10.5pt;"
            )
        self._update_actions()

    def _on_match_settings_changed(self, _value: object) -> None:
        self._invalidate_match_result("Settings changed — test again.")

    def _invalidate_match_result(self, message: str) -> None:
        if self._match_test_operation_id is not None:
            self._match_test_stale = True
        self._screenshot_widget.clear_overlays()
        self._test_result_label.setText(message)
        self._test_result_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
        )
        self._update_actions()

    def _parameter_updates(self) -> tuple[tuple[str, object], ...]:
        updates: list[tuple[str, object]] = []
        if "threshold" in self._supported_params:
            updates.append(("threshold", self._threshold_spin.value()))
        if "color_match" in self._supported_params:
            color_match = self._color_match_check.isChecked()
            updates.append(("color_match", color_match))
            if color_match and "color_tolerance" in self._supported_params:
                updates.append(
                    ("color_tolerance", self._tolerance_spin.value())
                )
        elif "color_tolerance" in self._supported_params:
            updates.append(("color_tolerance", self._tolerance_spin.value()))
        return tuple(updates)

    # ── Accept/reject and state ───────────────────────────────────

    def accept(self) -> None:
        if not self._can_save():
            self._set_status(
                "Load a screenshot, select at least 2 × 2 pixels, and enter "
                "a valid template name.",
                error=True,
            )
            return
        frame = self._current_frame
        rect = self._selection_rect
        assert frame is not None and rect is not None
        candidate, error = self._candidate_name()
        if error or candidate is None:
            self._set_status(error or "Enter a valid template name.", error=True)
            return
        reviewed = self._review_collision(candidate)
        if reviewed is None:
            self._update_actions()
            return
        name, replace_revision = reviewed
        try:
            png_bytes, width, height = crop_template_png(
                frame.png_bytes,
                rect.x(),
                rect.y(),
                rect.width(),
                rect.height(),
            )
        except ValueError as exc:
            self._set_status(str(exc), error=True)
            self._update_actions()
            return
        self._draft = CaptureTargetDraft(
            name=name,
            png_bytes=png_bytes,
            width=width,
            height=height,
            source_label=frame.source.label,
            parameter_updates=self._parameter_updates(),
            replace_revision=replace_revision,
            source_surface_width=frame.width,
            source_surface_height=frame.height,
            source_coordinate_space=frame.source.coordinate_space.value,
        )
        super().accept()

    def reject(self) -> None:
        self._closed = True
        self._detach_owned_operation()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._closed = True
        self._detach_owned_operation()
        super().closeEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_splitter"):
            compact = event.size().width() < 850
            orientation = (
                Qt.Orientation.Vertical
                if compact
                else Qt.Orientation.Horizontal
            )
            orientation_changed = self._splitter.orientation() != orientation
            self._splitter.setOrientation(orientation)
            self._title_label.setVisible(not compact)
            self._instruction_label.setVisible(not compact)
            self._screenshot_widget.setMinimumSize(
                360 if compact else 480,
                180 if compact else 270,
            )
            self._settings_scroll.setMinimumWidth(0 if compact else 310)
            self._settings_scroll.setMinimumHeight(150 if compact else 0)
            self._preview_label.setMinimumHeight(96 if compact else 132)
            if orientation_changed:
                if compact:
                    total = max(340, self._splitter.height())
                    self._splitter.setSizes(
                        [max(180, round(total * 0.55)), max(150, round(total * 0.45))]
                    )
                else:
                    total = max(780, self._splitter.width())
                    self._splitter.setSizes(
                        [max(480, round(total * 0.72)), max(310, round(total * 0.28))]
                    )
        if self._selection_rect is not None:
            self._refresh_selection_preview(invalidate_test=False)

    def _can_save(self) -> bool:
        frame = self._current_frame
        rect = self._selection_rect
        name, error = self._candidate_name()
        controller_busy = bool(
            self._capture_controller is not None
            and self._capture_controller.is_busy
        )
        return bool(
            frame is not None
            and rect is not None
            and rect.width() >= 2
            and rect.height() >= 2
            and name
            and error is None
            and not controller_busy
        )

    def _update_actions(self) -> None:
        if not hasattr(self, "_save_btn"):
            return
        controller = self._capture_controller
        busy = bool(controller is not None and controller.is_busy)
        self._capture_btn.setEnabled(
            controller is not None
            and self._selected_session() is not None
            and not busy
        )
        self._import_btn.setEnabled(controller is not None and not busy)
        has_frame = self._current_frame is not None
        has_selection = self._selection_rect is not None
        for spin in (
            self._x_spin,
            self._y_spin,
            self._width_spin,
            self._height_spin,
        ):
            spin.setEnabled(has_frame)
        self._clear_selection_btn.setEnabled(has_selection)
        if self._has_detection_settings:
            self._test_btn.setEnabled(
                has_frame
                and has_selection
                and self._selection_rect is not None
                and self._selection_rect.width() >= 2
                and self._selection_rect.height() >= 2
                and not busy
                and self._match_test_operation_id is None
            )
        self._save_btn.setEnabled(self._can_save())

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self._status_label.setText(message)
        self._status_label.setStyleSheet(
            f"color: {COLORS['text_primary' if error else 'text_secondary']}; "
            "font-size: 10.5pt;"
        )
