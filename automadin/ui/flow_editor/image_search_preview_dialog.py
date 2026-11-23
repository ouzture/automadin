"""Side-effect-free preview for image actions and their scoped search area."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType
from automadin.services.find_image import FindImageService, MatchResult
from automadin.services.image_search_region import (
    ImageSearchRegionError,
    SearchSurface,
    prepare_search_region,
    to_absolute_match,
)
from automadin.services.screen_capture import CaptureFrame, capture_source_matches_session
from automadin.ui.capture_controller import (
    CaptureBusyError,
    CaptureOperationController,
    CaptureStartError,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImageSearchPreviewResult:
    surface: SearchSurface
    matched_image: str | None
    match: MatchResult | None


class _PreviewSignals(QObject):
    completed = Signal(str, object, str)


class _ImageSearchPreviewTask(QRunnable):
    def __init__(
        self,
        operation_id: str,
        screenshot: bytes,
        command: FlowCommand,
        database: Any,
    ) -> None:
        super().__init__()
        self.operation_id = operation_id
        self._screenshot = screenshot
        self._params = copy.deepcopy(command.params)
        self._database = database
        self.signals = _PreviewSignals()

    @Slot()
    def run(self) -> None:
        try:
            target_names = self._literal_images("images", fallback="image_name")
            anchor_names = self._literal_images("search_anchor_images")

            def resolve_value(name: str, default: object, _type: ValueType) -> object:
                value = self._params.get(name, default)
                if isinstance(value, dict):
                    raise ImageSearchRegionError(
                        "Preview requires literal values. Use Run Step when this "
                        "command uses Inputs or Variables."
                    )
                return value

            surface = prepare_search_region(
                self._screenshot,
                self._params,
                self._database,
                resolve_value=resolve_value,
                anchor_image_names=anchor_names,
            )
            service = FindImageService()
            threshold = float(resolve_value("threshold", 0.9, ValueType.NUMBER))
            color_match = bool(
                resolve_value("color_match", False, ValueType.BOOLEAN)
            )
            color_tolerance = float(
                resolve_value("color_tolerance", 0.3, ValueType.NUMBER)
            )
            matched_image = None
            match = None
            for image_name in target_names:
                template = self._database.load_image(image_name)
                if template is None:
                    continue
                relative = service.find(
                    surface.png, template, threshold, color_match, color_tolerance
                )
                if relative is not None:
                    matched_image = image_name
                    match = to_absolute_match(relative, surface)
                    break
            result = ImageSearchPreviewResult(surface, matched_image, match)
        except (ImageSearchRegionError, TypeError, ValueError) as exc:
            self.signals.completed.emit(self.operation_id, None, str(exc))
        except Exception:
            logger.exception("Image search preview failed")
            self.signals.completed.emit(
                self.operation_id,
                None,
                "Preview could not complete. Check the Automadin log, then try again.",
            )
        else:
            self.signals.completed.emit(self.operation_id, result, "")

    def _literal_images(self, key: str, *, fallback: str | None = None) -> list[str]:
        value = self._params.get(key)
        if value is None and fallback is not None:
            value = self._params.get(fallback)
        if value is None and key == "search_anchor_images":
            return []
        if isinstance(value, str):
            return [value] if value else []
        if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
            raise ImageSearchRegionError(
                "Preview requires literal image names. Use Run Step when images are dynamic."
            )
        return [name for name in value if name]


class ImageSearchPreviewDialog(QDialog):
    """Capture a target and preview one image command without running it."""

    def __init__(
        self,
        command: FlowCommand,
        database: Any,
        *,
        device_selector: Any,
        capture_controller: CaptureOperationController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview Image Search")
        self.resize(900, 720)
        self._command = copy.deepcopy(command)
        self._database = database
        self._device_selector = device_selector
        self._capture_controller = capture_controller
        self._capture_operation_id: str | None = None
        self._preview_operation_id: str | None = None
        self._current_frame: CaptureFrame | None = None
        self._closed = False
        self._build_ui()
        capture_controller.capture_succeeded.connect(self._on_capture_succeeded)
        capture_controller.capture_failed.connect(self._on_capture_failed)
        capture_controller.capture_cancelled.connect(self._on_capture_cancelled)
        capture_controller.capture_finished.connect(self._on_capture_finished)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        note = QLabel(
            "Preview captures the selected target, searches only this command's "
            "configured scope, and does not click or change flow values."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(note)
        self._image_label = QLabel("No screenshot captured yet")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(480, 280)
        self._image_label.setStyleSheet(
            f"background: {COLORS['bg_surface']}; color: {COLORS['text_tertiary']};"
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._image_label)
        layout.addWidget(scroll, 1)
        self._result = QPlainTextEdit()
        self._result.setReadOnly(True)
        self._result.setMaximumHeight(130)
        self._result.setPlaceholderText("The scoped image-search result will appear here.")
        layout.addWidget(self._result)
        self._status = QLabel("Capture a connected target to begin.")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        buttons = QHBoxLayout()
        self._capture_button = QPushButton("Capture and Preview")
        self._capture_button.setProperty("cssClass", "primary")
        self._capture_button.clicked.connect(self._capture)
        buttons.addWidget(self._capture_button)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _capture(self) -> None:
        session = self._device_selector.get_selected_session()
        if session is None:
            self._set_status("Connect to a target first.", error=True)
            return
        self._result.clear()
        self._capture_button.setEnabled(False)
        self._capture_button.setText("Capturing…")
        self._set_status("Capturing the selected target…")
        try:
            self._capture_operation_id = self._capture_controller.capture_session(session)
        except (CaptureBusyError, CaptureStartError) as exc:
            self._capture_button.setEnabled(True)
            self._capture_button.setText("Capture and Preview")
            self._set_status(str(exc), error=True)

    @Slot(str, object)
    def _on_capture_succeeded(self, operation_id: str, frame: object) -> None:
        if self._closed or operation_id != self._capture_operation_id:
            return
        if not isinstance(frame, CaptureFrame):
            self._set_status("Capture returned an invalid image.", error=True)
            return
        session = self._device_selector.get_selected_session()
        if session is None or not capture_source_matches_session(frame.source, session):
            self._set_status("Target changed during capture. Capture again.", error=True)
            return
        self._current_frame = frame
        operation = str(uuid4())
        self._preview_operation_id = operation
        self._set_status("Resolving the search scope and matching the template…")
        task = _ImageSearchPreviewTask(operation, frame.png_bytes, self._command, self._database)
        task.signals.completed.connect(self._on_preview_completed)
        QThreadPool.globalInstance().start(task)

    @Slot(str, str)
    def _on_capture_failed(self, operation_id: str, message: str) -> None:
        if not self._closed and operation_id == self._capture_operation_id:
            self._set_status(f"Capture failed: {message}", error=True)

    @Slot(str)
    def _on_capture_cancelled(self, operation_id: str) -> None:
        if not self._closed and operation_id == self._capture_operation_id:
            self._set_status("Capture was cancelled.", error=True)

    @Slot(str)
    def _on_capture_finished(self, operation_id: str) -> None:
        if operation_id == self._capture_operation_id:
            self._capture_operation_id = None
            if not self._closed and self._preview_operation_id is None:
                self._capture_button.setEnabled(True)
                self._capture_button.setText("Capture and Preview")

    @Slot(str, object, str)
    def _on_preview_completed(self, operation_id: str, result: object, error: str) -> None:
        if self._closed or operation_id != self._preview_operation_id:
            return
        self._preview_operation_id = None
        self._capture_button.setEnabled(True)
        self._capture_button.setText("Capture and Preview")
        if error or not isinstance(result, ImageSearchPreviewResult):
            self._set_status(error or "Preview returned an invalid result.", error=True)
            return
        self._show_overlay(result)
        scope = result.surface.box
        if result.match is None:
            self._result.setPlainText(
                f"No target template matched inside {scope.width} × {scope.height} search scope."
            )
            self._set_status("Preview completed: no target match.")
            return
        self._result.setPlainText(
            f"Matched '{result.matched_image}' at ({result.match.center_x}, {result.match.center_y})\n"
            f"Confidence: {result.match.confidence:.2%}\n"
            f"Search scope: {scope.width} × {scope.height} at ({scope.left}, {scope.top})"
        )
        self._set_status("Preview completed. No click or flow values were changed.")

    def _show_overlay(self, result: ImageSearchPreviewResult) -> None:
        frame = self._current_frame
        if frame is None:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(frame.png_bytes):
            self._set_status("Preview image could not be displayed.", error=True)
            return
        painter = QPainter(pixmap)
        try:
            if result.surface.matched_box is not None:
                anchor = result.surface.matched_box
                painter.setPen(QPen(QColor(COLORS["success"]), 3))
                painter.drawRect(anchor.left, anchor.top, anchor.width, anchor.height)
            scope = result.surface.box
            painter.setPen(QPen(QColor(COLORS["warning"]), 3))
            painter.drawRect(scope.left, scope.top, scope.width, scope.height)
            if result.match is not None:
                match = result.match
                painter.setPen(QPen(QColor(COLORS["accent"]), 3))
                painter.drawRect(match.x, match.y, match.width, match.height)
        finally:
            painter.end()
        self._image_label.setPixmap(
            pixmap.scaled(760, 460, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self._status.setStyleSheet(
            f"color: {COLORS['error'] if error else COLORS['text_secondary']};"
        )
        self._status.setText(message)

    def closeEvent(self, event) -> None:
        self._closed = True
        operation = self._capture_operation_id
        if operation is not None and self._capture_controller.is_operation_active(operation):
            self._capture_controller.cancel(operation)
        super().closeEvent(event)
