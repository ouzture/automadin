"""Side-effect-free live preview for the Read Screen Text command."""

from __future__ import annotations

import copy
import logging
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
from automadin.services.ocr import ScreenTextOcrEngine
from automadin.services.screen_capture import CaptureFrame, capture_source_matches_session
from automadin.services.screen_text_reading import (
    ScreenTextReadResult,
    ScreenTextReadingError,
    read_screen_text,
)
from automadin.ui.capture_controller import (
    CaptureBusyError,
    CaptureOperationController,
    CaptureStartError,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)


class _PreviewSignals(QObject):
    completed = Signal(str, object, str)  # operation id, result, safe error


class _ScreenTextPreviewTask(QRunnable):
    """Resolve an OCR region and recognise it away from the GUI thread."""

    def __init__(
        self,
        operation_id: str,
        screenshot: bytes,
        command: FlowCommand,
        database: Any,
        engine: ScreenTextOcrEngine,
    ) -> None:
        super().__init__()
        self.operation_id = operation_id
        self._screenshot = screenshot
        self._params = copy.deepcopy(command.params)
        self._database = database
        self._engine = engine
        self.signals = _PreviewSignals()

    @Slot()
    def run(self) -> None:
        try:
            image_names = self._params.get("images", [])
            if not isinstance(image_names, list) or not all(
                isinstance(name, str) for name in image_names
            ):
                raise ScreenTextReadingError(
                    "Preview requires literal anchor images. Use Run Step when "
                    "the command builds them at runtime."
                )

            def resolve_value(name: str, default: object, _type: ValueType) -> object:
                value = self._params.get(name, default)
                if isinstance(value, dict):
                    raise ScreenTextReadingError(
                        "Preview requires literal region values. Use Run Step when "
                        "this command uses Inputs or Variables."
                    )
                return value

            result = read_screen_text(
                self._screenshot,
                self._params,
                self._database,
                self._engine,
                resolve_value=resolve_value,
                image_names=image_names,
            )
        except ScreenTextReadingError as exc:
            self.signals.completed.emit(self.operation_id, None, str(exc))
        except Exception:
            logger.exception("Screen text preview failed")
            self.signals.completed.emit(
                self.operation_id,
                None,
                "Preview could not complete. Check the Automadin log, then try again.",
            )
        else:
            self.signals.completed.emit(self.operation_id, result, "")


class ScreenTextPreviewDialog(QDialog):
    """Capture a target and show what one Read Screen Text command would read."""

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
        self.setWindowTitle("Preview Screen Text")
        self.resize(900, 720)
        self._command = copy.deepcopy(command)
        self._database = database
        self._device_selector = device_selector
        self._capture_controller = capture_controller
        self._capture_operation_id: str | None = None
        self._preview_operation_id: str | None = None
        self._closed = False
        self._current_frame: CaptureFrame | None = None
        self._build_ui()
        capture_controller.capture_succeeded.connect(self._on_capture_succeeded)
        capture_controller.capture_failed.connect(self._on_capture_failed)
        capture_controller.capture_cancelled.connect(self._on_capture_cancelled)
        capture_controller.capture_finished.connect(self._on_capture_finished)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        note = QLabel(
            "Preview captures the selected target and reads this command's region. "
            "It does not update Variables or execute the flow."
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
        self._result.setMaximumHeight(170)
        self._result.setPlaceholderText("The OCR result will appear here.")
        layout.addWidget(self._result)
        self._status = QLabel("Capture a connected target to begin.")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {COLORS['text_secondary']};")
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
        engine = session.data.get("screen_text_ocr_engine")
        if not isinstance(engine, ScreenTextOcrEngine):
            self._set_status(
                "No OCR engine is ready for this session. Set up Tesseract first.",
                error=True,
            )
            return
        self._current_frame = frame
        operation = str(uuid4())
        self._preview_operation_id = operation
        self._set_status("Resolving the region and recognising text…")
        task = _ScreenTextPreviewTask(
            operation,
            frame.png_bytes,
            self._command,
            self._database,
            engine,
        )
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
    def _on_preview_completed(
        self,
        operation_id: str,
        result: object,
        error: str,
    ) -> None:
        if self._closed or operation_id != self._preview_operation_id:
            return
        self._preview_operation_id = None
        self._capture_button.setEnabled(True)
        self._capture_button.setText("Capture and Preview")
        if error or not isinstance(result, ScreenTextReadResult):
            self._set_status(error or "Preview returned an invalid result.", error=True)
            return
        self._show_overlay(result)
        raw = result.recognized_text.rstrip("\r\n")
        self._result.setPlainText(
            f"Raw text:\n{raw}\n\nParsed {type(result.value).__name__}: {result.value!r}"
        )
        anchor = (
            f" anchored to '{result.prepared.matched_image}'"
            if result.prepared.matched_image
            else ""
        )
        self._set_status(
            f"Preview completed{anchor}. No flow values were changed.", error=False
        )

    def _show_overlay(self, result: ScreenTextReadResult) -> None:
        frame = self._current_frame
        if frame is None:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(frame.png_bytes):
            self._set_status("Preview image could not be displayed.", error=True)
            return
        painter = QPainter(pixmap)
        try:
            if result.prepared.matched_box is not None:
                anchor = result.prepared.matched_box
                painter.setPen(QPen(QColor(COLORS["success"]), 3))
                painter.drawRect(
                    anchor.left,
                    anchor.top,
                    anchor.right - anchor.left,
                    anchor.bottom - anchor.top,
                )
            box = result.prepared.box
            painter.setPen(QPen(QColor(COLORS["warning"]), 3))
            painter.drawRect(
                box.left,
                box.top,
                box.right - box.left,
                box.bottom - box.top,
            )
        finally:
            painter.end()
        self._image_label.setPixmap(
            pixmap.scaled(
                760,
                460,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
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
