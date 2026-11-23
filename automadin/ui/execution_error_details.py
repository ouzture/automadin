"""Reusable flow-error actions and details dialog."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.diagnostics import diagnostics_log_path
from automadin.execution.error_report import ExecutionErrorReport
from automadin.ui.theme import COLORS
from automadin.ui.widgets.buttons import configure_button
from automadin.ui.widgets.wrapping_layout import WrappingLayout


def _current_log_path() -> Path | None:
    data_dir = os.environ.get("AUTOMADIN_DATA_DIR")
    if not data_dir:
        return None
    return diagnostics_log_path(data_dir)


class ExecutionErrorDialog(QDialog):
    """Display and copy the complete local traceback for one run error."""

    def __init__(self, report: ExecutionErrorReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._report = report
        self.setWindowTitle(f"Execution Error · {report.reference}")
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        summary = QLabel(report.log_message())
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color: {COLORS['error']}; font-weight: 600;")
        layout.addWidget(summary)

        privacy_note = QLabel(
            "Technical details can contain local file paths or page information. "
            "Review them before sharing."
        )
        privacy_note.setWordWrap(True)
        privacy_note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(privacy_note)

        details = QPlainTextEdit(report.copy_text())
        details.setReadOnly(True)
        details.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(details, 1)

        actions = QHBoxLayout()
        copy_button = QPushButton("Copy details")
        copy_button.clicked.connect(self._copy_details)
        actions.addWidget(copy_button)
        open_button = QPushButton("Open full log")
        open_button.setEnabled(_current_log_path() is not None)
        open_button.clicked.connect(self._open_full_log)
        actions.addWidget(open_button)
        actions.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        layout.addLayout(actions)

    def _copy_details(self) -> None:
        QApplication.clipboard().setText(self._report.copy_text())

    def _open_full_log(self) -> None:
        log_path = _current_log_path()
        if log_path is not None:
            target = log_path if log_path.is_file() else log_path.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


class ExecutionErrorBar(QFrame):
    """Compact actions shown beneath a log after an execution error."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._report: ExecutionErrorReport | None = None
        self.setObjectName("executionErrorBar")
        self.setStyleSheet(
            f"#executionErrorBar {{ background: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['error']}; border-radius: 5px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(4)
        self._label = QLabel("Error details are available")
        self._label.setStyleSheet(f"color: {COLORS['error']}; font-weight: 600;")
        layout.addWidget(self._label)

        actions_widget = QWidget()
        actions = WrappingLayout(actions_widget)
        actions.setContentsMargins(0, 0, 0, 0)

        view_button = QPushButton("View details")
        configure_button(view_button, density="compact", minimum_width=88)
        view_button.clicked.connect(self._view_details)
        actions.addWidget(view_button)

        copy_button = QPushButton("Copy details")
        configure_button(
            copy_button,
            appearance="outline",
            density="compact",
            minimum_width=88,
        )
        copy_button.setToolTip(
            "Copy technical details. Review them before sharing because they can "
            "contain local paths or page information."
        )
        copy_button.clicked.connect(self._copy_details)
        actions.addWidget(copy_button)

        logs_button = QPushButton("Open full log")
        configure_button(
            logs_button,
            appearance="outline",
            density="compact",
            minimum_width=96,
        )
        logs_button.setEnabled(_current_log_path() is not None)
        logs_button.clicked.connect(self._open_full_log)
        actions.addWidget(logs_button)
        layout.addWidget(actions_widget)
        self.hide()

    def show_report(self, report: object) -> None:
        if not isinstance(report, ExecutionErrorReport):
            return
        self._report = report
        self._label.setText(f"Error reference {report.reference}")
        self.show()

    def clear_report(self) -> None:
        self._report = None
        self.hide()

    @property
    def report(self) -> ExecutionErrorReport | None:
        return self._report

    def _view_details(self) -> None:
        if self._report is not None:
            ExecutionErrorDialog(self._report, self).exec()

    def _copy_details(self) -> None:
        if self._report is not None:
            QApplication.clipboard().setText(self._report.copy_text())

    def _open_full_log(self) -> None:
        log_path = _current_log_path()
        if log_path is not None:
            target = log_path if log_path.is_file() else log_path.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
