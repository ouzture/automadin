"""Preview and copy an allowlist-based Automadin support report."""

from __future__ import annotations

import logging
from collections.abc import Callable
from inspect import Signature, signature
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import (
    QEvent,
    QEventLoop,
    QObject,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QClipboard,
    QCloseEvent,
    QDesktopServices,
    QFontDatabase,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.services.support_report import (
    SupportReport,
    validated_logs_directory,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.services.support_report_process import collect_support_report
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)

_DEFERRED_INPUT_EVENTS = frozenset(
    {
        QEvent.Type.KeyPress,
        QEvent.Type.KeyRelease,
        QEvent.Type.Shortcut,
        QEvent.Type.ShortcutOverride,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
        QEvent.Type.Wheel,
        QEvent.Type.ContextMenu,
        QEvent.Type.InputMethod,
        QEvent.Type.TabletPress,
        QEvent.Type.TabletMove,
        QEvent.Type.TabletRelease,
        QEvent.Type.TouchBegin,
        QEvent.Type.TouchUpdate,
        QEvent.Type.TouchEnd,
        QEvent.Type.TouchCancel,
        QEvent.Type.Gesture,
        QEvent.Type.GestureOverride,
        QEvent.Type.DragEnter,
        QEvent.Type.DragMove,
        QEvent.Type.DragLeave,
        QEvent.Type.Drop,
    }
)


class _Clipboard(Protocol):
    def setText(self, text: str, mode: QClipboard.Mode = ...) -> None: ...

    def text(self, mode: QClipboard.Mode = ...) -> str: ...


class _SupportReportWorker(QObject):
    completed = Signal(object)

    def __init__(
        self,
        db_path: str | Path,
        data_dir: str | Path,
        builder: Callable[..., SupportReport],
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._data_dir = data_dir
        self._builder = builder

    @Slot()
    def run(self) -> None:
        report: SupportReport | None = None
        try:
            if not QThread.currentThread().isInterruptionRequested():
                cancel_callback = (
                    QThread.currentThread().isInterruptionRequested
                )
                arguments = (
                    self._db_path,
                    self._data_dir,
                    cancel_callback,
                )
                keyword_arguments: dict[str, object] = {}
                try:
                    builder_signature: Signature | None = signature(
                        self._builder
                    )
                except (TypeError, ValueError):
                    builder_signature = None
                if builder_signature is None:
                    arguments = arguments[:2]
                else:
                    try:
                        builder_signature.bind(*arguments)
                    except TypeError:
                        try:
                            builder_signature.bind(
                                *arguments[:2],
                                cancel_requested=cancel_callback,
                            )
                        except TypeError:
                            arguments = arguments[:2]
                        else:
                            arguments = arguments[:2]
                            keyword_arguments["cancel_requested"] = (
                                cancel_callback
                            )
                report = self._builder(*arguments, **keyword_arguments)
        except Exception:
            if not QThread.currentThread().isInterruptionRequested():
                logger.exception(
                    "Could not build the allowlist-based support report"
                )
        if QThread.currentThread().isInterruptionRequested():
            report = None
        self.completed.emit(report)


class DiagnosticsReportLoaderDialog(QDialog):
    """Collect Diagnostics facts off the GUI thread and retain worker ownership."""

    _REVEAL_DELAY_MS = 150

    def __init__(
        self,
        db_path: str | Path,
        data_dir: str | Path,
        parent: QWidget | None = None,
        *,
        report_builder: Callable[..., SupportReport] = (
            collect_support_report
        ),
    ) -> None:
        super().__init__(parent)
        self._started = False
        self._close_requested = False
        self._pending_report: SupportReport | None = None
        self._collection_failed = False
        self._loader_revealed = False
        self._worker_completed = False
        self._exec_loop: QEventLoop | None = None
        self._input_guard_active = False

        self.setWindowTitle("Diagnostics")
        self.setModal(True)
        self.setMinimumSize(600, 220)
        self.resize(640, 240)

        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(self._REVEAL_DELAY_MS)
        self._reveal_timer.timeout.connect(self._reveal_if_needed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        self.title_label = QLabel("Preparing support report")
        self.title_label.setStyleSheet("font-size: 15pt; font-weight: 650;")
        layout.addWidget(self.title_label)

        self.status_label = QLabel(
            "Reading aggregate workspace counts and fixed diagnostic-file metadata."
        )
        self.status_label.setObjectName("diagnosticsLoaderStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setObjectName("diagnosticsLoaderProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Preparing support report")
        layout.addWidget(self.progress)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        actions.addStretch(1)
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button = close_box.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText("Cancel")
        close_box.rejected.connect(self.reject)
        actions.addWidget(close_box)
        layout.addLayout(actions)

        self._thread = QThread(self)
        self._worker = _SupportReportWorker(db_path, data_dir, report_builder)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.completed.connect(self._worker.deleteLater)
        self._worker.completed.connect(self._on_worker_completed)
        self._thread.finished.connect(self._on_thread_finished)

    @property
    def report(self) -> SupportReport | None:
        return self._pending_report

    @property
    def collection_failed(self) -> bool:
        return self._collection_failed

    def exec(self) -> int:
        """Collect first and show this modal only when collection is not fast."""
        if self._started:
            raise RuntimeError("Diagnostics collection has already started.")
        self.setResult(QDialog.DialogCode.Rejected)
        event_loop = QEventLoop()
        self._exec_loop = event_loop
        self._install_input_guard()
        try:
            self._start_collection(deferred_reveal=True)
            event_loop.exec()
        finally:
            self._remove_input_guard()
            self._exec_loop = None
        return self.result()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._started:
            self._loader_revealed = True
            self._start_collection(deferred_reveal=False)

    def done(self, result: int) -> None:
        self._remove_input_guard()
        super().done(result)
        if self._exec_loop is not None and self._exec_loop.isRunning():
            self._exec_loop.quit()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._input_guard_active:
            if event.type() in _DEFERRED_INPUT_EVENTS:
                return True
            if event.type() == QEvent.Type.Close:
                return True
        return super().eventFilter(watched, event)

    def _install_input_guard(self) -> None:
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
            self._input_guard_active = True

    def _remove_input_guard(self) -> None:
        if not self._input_guard_active:
            return
        self._input_guard_active = False
        application = QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)

    def _start_collection(self, *, deferred_reveal: bool) -> None:
        self._started = True
        if deferred_reveal:
            self._reveal_timer.start()
        try:
            self._thread.start()
        except BaseException:
            logger.exception("Could not start Diagnostics collection worker")
            if qthread_start_may_have_succeeded(self._thread):
                self._thread.requestInterruption()
                self.status_label.setText(
                    "Diagnostics started but its startup could not be confirmed. "
                    "Waiting for the retained worker to finish safely."
                )
                return
            self._worker_completed = True
            self._present_collection_failure()

    @Slot()
    def _reveal_if_needed(self) -> None:
        if (
            self._close_requested
            or self._worker_completed
            or self._loader_revealed
        ):
            return
        self._remove_input_guard()
        self._loader_revealed = True
        self.show()

    @Slot(object)
    def _on_worker_completed(self, report: SupportReport | None) -> None:
        self._worker_completed = True
        self._pending_report = report
        self._thread.quit()

    @Slot()
    def _on_thread_finished(self) -> None:
        self._remove_input_guard()
        if self._close_requested:
            self._reveal_timer.stop()
            self.done(QDialog.DialogCode.Rejected)
            return
        if self._pending_report is not None:
            self._reveal_timer.stop()
            self.done(QDialog.DialogCode.Accepted)
            return

        self._present_collection_failure()

    def _present_collection_failure(self) -> None:
        self._collection_failed = True
        self._reveal_timer.stop()
        self._remove_input_guard()
        self.title_label.setText("Support report unavailable")
        self.progress.hide()
        self.status_label.setText(
            "The support report could not be prepared. No information was copied. "
            "Close this window and try Diagnostics again."
        )
        self.close_button.setText("Close")
        self.close_button.setEnabled(True)
        self.setMinimumSize(640, 260)
        self.resize(max(self.width(), 640), max(self.height(), 260))
        if not self._loader_revealed:
            self._loader_revealed = True
            self.show()

    def reject(self) -> None:
        if self._thread.isRunning():
            self._request_close()
            return
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread.isRunning():
            self._request_close()
            event.ignore()
            return
        super().closeEvent(event)

    def _request_close(self) -> None:
        self._close_requested = True
        self._thread.requestInterruption()
        self.close_button.setEnabled(False)
        self.status_label.setText(
            "Closing after the current local diagnostics check finishes..."
        )


class DiagnosticsDialog(QDialog):
    """Show exactly what Copy writes and keep raw log sharing explicit."""

    def __init__(
        self,
        report: SupportReport,
        parent: QWidget | None = None,
        *,
        directory_opener: Callable[[QUrl], bool] | None = None,
        clipboard: _Clipboard | None = None,
    ) -> None:
        super().__init__(parent)
        self._report = report
        self._directory_opener = directory_opener or QDesktopServices.openUrl
        self._clipboard = clipboard

        self.setWindowTitle("Diagnostics")
        self.setModal(True)
        self.setMinimumSize(760, 600)
        self.resize(860, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("Support report")
        title.setStyleSheet("font-size: 15pt; font-weight: 650;")
        layout.addWidget(title)

        intro = QLabel(
            "Review exactly what Copy Support Report will place on the clipboard. "
            "It includes app, platform, and runtime versions, aggregate workspace "
            "counts, and fixed Automadin log filenames, sizes, and timestamps. "
            "The copied report does not include database records, workflow/profile/"
            "device/account names, URLs, environment variables, local paths, or "
            "log messages."
        )
        intro.setObjectName("diagnosticsPrivacyNotice")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        warning_frame = QFrame()
        warning_frame.setObjectName("diagnosticsWarningFrame")
        warning_frame.setStyleSheet(
            f"""
            QFrame#diagnosticsWarningFrame {{
                background-color: {COLORS["bg_elevated"]};
                border: 2px solid {COLORS["warning"]};
                border-radius: 6px;
            }}
            QFrame#diagnosticsWarningFrame QLabel {{
                background-color: transparent;
                border: none;
                color: {COLORS["text_primary"]};
            }}
            """
        )
        warning_layout = QHBoxLayout(warning_frame)
        warning_layout.setContentsMargins(12, 9, 12, 9)
        log_warning = QLabel(
            "Warning: Raw diagnostic log files and the local logs-folder path can "
            "contain sensitive automation, account, or user information. Review "
            "them yourself before attaching, copying, or sharing them."
        )
        log_warning.setObjectName("diagnosticsLogWarning")
        log_warning.setWordWrap(True)
        log_warning.setStyleSheet("font-weight: 650;")
        warning_layout.addWidget(log_warning)
        layout.addWidget(warning_frame)

        self.preview = QPlainTextEdit()
        self.preview.setObjectName("diagnosticsReportPreview")
        self.preview.setReadOnly(True)
        self.preview.setPlainText(report.text)
        self.preview.setAccessibleName("Support report preview")
        preview_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        if preview_font.pointSizeF() < 10.5:
            preview_font.setPointSizeF(10.5)
        self.preview.setFont(preview_font)
        layout.addWidget(self.preview, 1)

        status_text = "Ready to copy."
        if report.logs_directory is None:
            status_text += f" Logs folder: {report.snapshot.diagnostics.status}."
        self.status_label = QLabel(status_text)
        self.status_label.setObjectName("diagnosticsStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        self.copy_button = QPushButton("Copy Support Report")
        self.copy_button.setObjectName("copySupportReportButton")
        self.copy_button.setProperty("cssClass", "primary")
        self.copy_button.setAccessibleDescription(
            "Copy the exact support report preview to the system clipboard."
        )
        self.copy_button.clicked.connect(self._copy_report)
        actions.addWidget(self.copy_button)

        self.open_logs_button = QPushButton("Open Logs Folder")
        self.open_logs_button.setObjectName("openLogsFolderButton")
        self.open_logs_button.setAccessibleDescription(
            "Open the local diagnostics folder. Review log contents before sharing."
        )
        self.open_logs_button.setEnabled(report.logs_directory is not None)
        self.open_logs_button.clicked.connect(self._open_logs_folder)
        actions.addWidget(self.open_logs_button)

        self.copy_logs_path_button = QPushButton("Copy Logs Path")
        self.copy_logs_path_button.setObjectName("copyLogsPathButton")
        self.copy_logs_path_button.setAccessibleDescription(
            "Copy only the local logs-folder path. The path can contain user "
            "information and is not part of the support report."
        )
        self.copy_logs_path_button.setEnabled(report.logs_directory is not None)
        self.copy_logs_path_button.clicked.connect(self._copy_logs_path)
        actions.addWidget(self.copy_logs_path_button)

        if report.logs_directory is None:
            unavailable_reason = (
                f"Logs folder: {report.snapshot.diagnostics.status}."
            )
            self.open_logs_button.setToolTip(unavailable_reason)
            self.copy_logs_path_button.setToolTip(unavailable_reason)
            self.open_logs_button.setAccessibleDescription(unavailable_reason)
            self.copy_logs_path_button.setAccessibleDescription(unavailable_reason)

        actions.addStretch(1)
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        actions.addWidget(close_box)
        layout.addLayout(actions)

        close_button = close_box.button(QDialogButtonBox.StandardButton.Close)
        QWidget.setTabOrder(self.preview, self.copy_button)
        QWidget.setTabOrder(self.copy_button, self.open_logs_button)
        QWidget.setTabOrder(self.open_logs_button, self.copy_logs_path_button)
        QWidget.setTabOrder(self.copy_logs_path_button, close_button)
        self.preview.setFocus()

    def _active_clipboard(self) -> _Clipboard:
        return self._clipboard or QApplication.clipboard()

    def _copy_text(self, text: str, *, success: str, failure: str) -> bool:
        try:
            clipboard = self._active_clipboard()
            clipboard.setText(text)
            copied = clipboard.text()
        except Exception:
            copied = ""
        confirmed = copied == text
        self.status_label.setText(success if confirmed else failure)
        return confirmed

    def _copy_report(self) -> None:
        self._copy_text(
            self._report.text,
            success="Support report copied to the clipboard.",
            failure=(
                "Could not confirm the clipboard copy. Try Copy Support Report again."
            ),
        )

    def _copy_logs_path(self) -> None:
        logs_directory = self._report.logs_directory
        if logs_directory is None:
            self.copy_logs_path_button.setEnabled(False)
            self.status_label.setText("The logs folder is not available.")
            return
        validated = validated_logs_directory(logs_directory)
        if validated is None:
            self.open_logs_button.setEnabled(False)
            self.copy_logs_path_button.setEnabled(False)
            self.status_label.setText("The logs folder is no longer available.")
            return
        self._copy_text(
            str(validated),
            success="Logs-folder path copied to the clipboard.",
            failure="Could not confirm the path copy. Try Copy Logs Path again.",
        )

    def _open_logs_folder(self) -> None:
        logs_directory = self._report.logs_directory
        if logs_directory is None:
            self.open_logs_button.setEnabled(False)
            self.status_label.setText("The logs folder is not available.")
            return
        validated = validated_logs_directory(logs_directory)
        if validated is None:
            self.open_logs_button.setEnabled(False)
            self.copy_logs_path_button.setEnabled(False)
            self.status_label.setText("The logs folder is no longer available.")
            return
        try:
            opened = bool(
                self._directory_opener(QUrl.fromLocalFile(str(validated)))
            )
        except Exception:
            opened = False
        self.status_label.setText(
            "Open request sent to File Explorer."
            if opened
            else (
                "Could not open the logs folder. Use Copy Logs Path and open it "
                "in File Explorer."
            )
        )
