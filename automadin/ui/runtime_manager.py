"""Per-user manager for optional ADB and Playwright browser runtimes."""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from automadin.runtime import (
    configure_runtime,
    inspect_browser_payload,
    managed_adb_path,
    managed_browsers_dir,
    managed_runtime_dir,
)
from automadin.runtime_components import (
    ADB_TERMS_URL,
    install_adb,
    install_playwright_browsers,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)

_INSTALL_FAILURE_MESSAGE = (
    "Installation could not be completed. Check your connection and the "
    "Automadin log, then try again."
)
_INSTALL_START_FAILURE_MESSAGE = (
    "Installation could not be started. Check the Automadin log, then try again."
)
_INSTALL_CANCELLED_MESSAGE = "Installation was cancelled."


class _InstallWorker(QThread):
    progress = Signal(int, str)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, component: str, data_dir: str, parent=None) -> None:
        super().__init__(parent)
        self._component = component
        self._data_dir = data_dir
        self._cancelled = threading.Event()
        self._process: subprocess.Popen | None = None

    def cancel(self) -> None:
        self._cancelled.set()
        try:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
        except Exception:
            logger.exception("Could not terminate the runtime installer process")

    def run(self) -> None:
        try:
            if self._component == "adb":
                path = install_adb(
                    self._data_dir,
                    progress=lambda percent, message: self.progress.emit(percent, message),
                    cancelled=self._cancelled.is_set,
                )
                self.succeeded.emit(f"ADB installed at {path}")
                return

            browser_dir = install_playwright_browsers(
                self._data_dir,
                progress=lambda percent, message: self.progress.emit(percent, message),
                cancelled=self._cancelled.is_set,
                process_started=lambda process: setattr(self, "_process", process),
            )
            self.succeeded.emit(f"Browsers installed at {browser_dir}")
        except Exception:
            if self._cancelled.is_set():
                self.failed.emit(_INSTALL_CANCELLED_MESSAGE)
            else:
                logger.exception(
                    "Runtime component installation failed for %s",
                    self._component,
                )
                self.failed.emit(_INSTALL_FAILURE_MESSAGE)
        finally:
            self._process = None


class RuntimeManagerDialog(QDialog):
    """Download, repair, and inspect Automadin-managed runtime components."""

    def __init__(self, data_dir: str, parent=None) -> None:
        super().__init__(parent)
        self._data_dir = data_dir
        self._worker: _InstallWorker | None = None
        self._pending_result: tuple[str, str] | None = None
        self._cancel_requested = False
        self._close_requested = False
        self.setWindowTitle("Automadin Runtime Manager")
        self.setMinimumSize(720, 560)
        self.resize(780, 620)
        self._build_ui()
        self._refresh_status()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        title = QLabel("Runtime components")
        title.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 17pt; font-weight: 700;"
        )
        layout.addWidget(title)
        intro = QLabel(
            "Browsers and Android tools are downloaded separately into Automadin's "
            "managed per-user directory. No system PATH or browser installation is changed. "
            "Restart an already-open Automadin window after changing components."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10.5pt;")
        layout.addWidget(intro)

        browser_group = QGroupBox("Playwright browsers")
        browser_layout = QVBoxLayout(browser_group)
        browser_text = QLabel("Chromium, Firefox, and WebKit — required for browser devices.")
        browser_text.setWordWrap(True)
        browser_layout.addWidget(browser_text)
        self._browser_package_status = QLabel()
        self._browser_package_status.setWordWrap(True)
        self._browser_package_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        browser_layout.addWidget(self._browser_package_status)
        browser_row = QHBoxLayout()
        self._browser_status = QLabel()
        self._browser_status.setWordWrap(True)
        self._browser_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        browser_row.addWidget(self._browser_status, 1)
        self._browser_button = QPushButton("Install / Repair")
        self._browser_button.setProperty("cssClass", "primary")
        self._browser_button.clicked.connect(lambda: self._start_install("browsers"))
        browser_row.addWidget(self._browser_button)
        browser_layout.addLayout(browser_row)
        layout.addWidget(browser_group)

        adb_group = QGroupBox("Android Debug Bridge (ADB)")
        adb_layout = QVBoxLayout(adb_group)
        adb_text = QLabel(
            "Downloads the current Windows Platform Tools package directly from Google, "
            "then retains only ADB, its required DLLs, and licence notices."
        )
        adb_text.setWordWrap(True)
        adb_layout.addWidget(adb_text)
        self._adb_package_status = QLabel()
        self._adb_package_status.setWordWrap(True)
        self._adb_package_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        adb_layout.addWidget(self._adb_package_status)
        terms_row = QHBoxLayout()
        self._terms_check = QCheckBox("I have read and accept the Android SDK terms")
        self._terms_check.toggled.connect(self._update_adb_button)
        terms_row.addWidget(self._terms_check)
        terms_link = QPushButton("Open terms")
        terms_link.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(ADB_TERMS_URL))
        )
        terms_row.addWidget(terms_link)
        terms_row.addStretch()
        adb_layout.addLayout(terms_row)
        adb_row = QHBoxLayout()
        self._adb_status = QLabel()
        self._adb_status.setWordWrap(True)
        self._adb_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        adb_row.addWidget(self._adb_status, 1)
        self._adb_button = QPushButton("Install / Repair")
        self._adb_button.setProperty("cssClass", "primary")
        self._adb_button.clicked.connect(lambda: self._start_install("adb"))
        adb_row.addWidget(self._adb_button)
        adb_layout.addLayout(adb_row)
        layout.addWidget(adb_group)

        self._operation_status = QLabel()
        self._operation_status.setWordWrap(True)
        self._operation_status.setStyleSheet(f"color: {COLORS['warning']};")
        self._operation_status.setVisible(False)
        layout.addWidget(self._operation_status)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Installation progress will appear here.")
        self._log.setMaximumBlockCount(500)
        layout.addWidget(self._log, 1)

        buttons = QHBoxLayout()
        self._location = QLabel(f"Managed root: {Path(self._data_dir) / 'runtime'}")
        self._location.setStyleSheet(f"color: {COLORS['text_tertiary']};")
        self._location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        buttons.addWidget(self._location, 1)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setVisible(False)
        self._cancel_button.clicked.connect(self._cancel_install)
        buttons.addWidget(self._cancel_button)
        self._close_button = QPushButton("Close")
        self._close_button.clicked.connect(self.close)
        buttons.addWidget(self._close_button)
        layout.addLayout(buttons)

    def _refresh_status(self) -> None:
        configuration = configure_runtime(self._data_dir)

        managed_browser_root = managed_browsers_dir(self._data_dir)
        managed_browser = inspect_browser_payload(managed_browser_root)
        if managed_browser.managed_package_installed and managed_browser.usable:
            browser_package_text = f"Managed package installed and complete: {managed_browser_root}"
            browser_package_ok = True
        elif managed_browser.managed_package_installed:
            missing = ", ".join(engine.title() for engine in managed_browser.missing_engines)
            browser_package_text = (
                f"Managed package installed but needs Repair at {managed_browser_root}. "
                f"Missing executable payload: {missing or 'unknown'}."
            )
            browser_package_ok = False
        elif managed_browser_root.exists():
            missing = ", ".join(engine.title() for engine in managed_browser.missing_engines)
            detail = f" Missing executable payload: {missing}." if missing else ""
            browser_package_text = (
                f"Managed package is incomplete at {managed_browser_root}.{detail} "
                "Select Repair to download a complete replacement."
            )
            browser_package_ok = False
        else:
            browser_package_text = "Managed package not installed."
            browser_package_ok = False
        self._browser_package_status.setText(browser_package_text)
        self._browser_package_status.setStyleSheet(
            f"color: {COLORS['success'] if browser_package_ok else COLORS['warning']};"
        )
        self._browser_button.setText("Repair" if managed_browser_root.exists() else "Install")

        browser_runtime = configuration.browsers
        if browser_runtime.usable and browser_runtime.path is not None:
            payload = inspect_browser_payload(browser_runtime.path)
            executable_lines = "\n".join(
                f"  {engine.title()}: {path}" for engine, path in payload.executable_paths
            )
            browser_status_text = (
                f"Usable browser runtime detected ({browser_runtime.source}): "
                f"{browser_runtime.path}\nVerified executables:\n{executable_lines}"
            )
        else:
            browser_status_text = (
                "Usable browser runtime not detected. Install or Repair the managed package."
            )
        self._browser_status.setText(browser_status_text)
        self._browser_status.setStyleSheet(
            f"color: {COLORS['success'] if browser_runtime.usable else COLORS['warning']};"
        )

        managed_adb = managed_adb_path(self._data_dir)
        managed_adb_root = managed_runtime_dir(self._data_dir) / "platform-tools"
        if managed_adb is not None:
            self._adb_package_status.setText(
                f"Managed package installed: {managed_adb} (package {managed_adb.parent.name})"
            )
            adb_package_ok = True
        elif managed_adb_root.exists():
            self._adb_package_status.setText(
                f"Managed package is incomplete at {managed_adb_root}. "
                "Select Repair to download and verify a complete replacement."
            )
            adb_package_ok = False
        else:
            self._adb_package_status.setText("Managed package not installed.")
            adb_package_ok = False
        self._adb_package_status.setStyleSheet(
            f"color: {COLORS['success'] if adb_package_ok else COLORS['warning']};"
        )
        self._adb_button.setText("Repair" if managed_adb_root.exists() else "Install")

        adb_runtime = configuration.adb
        if adb_runtime.usable and adb_runtime.path is not None:
            adb_status_text = (
                f"Usable ADB detected ({adb_runtime.source}): {adb_runtime.path}\n"
                f"Version: {adb_runtime.version or 'version command succeeded'}"
            )
        else:
            adb_status_text = (
                "Usable ADB not detected. Install or Repair the managed package, "
                "or configure a working external ADB path."
            )
        self._adb_status.setText(adb_status_text)
        self._adb_status.setStyleSheet(
            f"color: {COLORS['success'] if adb_runtime.usable else COLORS['warning']};"
        )
        self._update_adb_button()

    def _update_adb_button(self) -> None:
        idle = self._worker is None
        self._adb_button.setEnabled(idle and self._terms_check.isChecked())

    def _start_install(self, component: str) -> None:
        if self._worker is not None:
            return
        if component == "adb" and not self._terms_check.isChecked():
            QMessageBox.warning(self, "Terms Required", "Accept the Android SDK terms first.")
            return
        self._log.clear()
        self._pending_result = None
        self._cancel_requested = False
        self._close_requested = False
        component_name = "ADB" if component == "adb" else "Playwright browsers"
        self._operation_status.setText(f"Installing {component_name}…")
        self._operation_status.setVisible(True)
        self._progress.setVisible(True)
        self._progress.setRange(0, 100 if component == "adb" else 0)
        self._progress.setValue(0)
        self._browser_button.setEnabled(False)
        self._adb_button.setEnabled(False)
        self._cancel_button.setVisible(True)
        self._cancel_button.setEnabled(True)
        self._cancel_button.setText("Cancel")
        self._close_button.setEnabled(True)
        worker: _InstallWorker | None = None
        try:
            worker = _InstallWorker(component, self._data_dir, self)
            worker.progress.connect(self._on_progress)
            worker.succeeded.connect(self._on_succeeded)
            worker.failed.connect(self._on_failed)
            worker.finished.connect(self._on_worker_finished)
            self._worker = worker
            worker.start()
        except Exception:
            logger.exception(
                "Could not start runtime installation worker for %s",
                component,
            )
            if qthread_start_may_have_succeeded(worker):
                self._pending_result = (
                    "failure",
                    _INSTALL_START_FAILURE_MESSAGE,
                )
                self._operation_status.setText(
                    "Installation could not start cleanly. Waiting for the "
                    "installer worker to stop…"
                )
                try:
                    worker.cancel()
                except Exception:
                    logger.exception(
                        "Could not cancel runtime worker after start failure"
                    )
                return
            if self._worker is worker:
                self._worker = None
            if worker is not None:
                self._release_worker(worker, context="after start failure")
            self._report_start_failure()

    def _on_progress(self, percent: int, message: str) -> None:
        if percent:
            self._progress.setValue(percent)
        self._log.appendPlainText(message)

    def _on_succeeded(self, message: str) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._log.appendPlainText(message)
        self._pending_result = ("success", message)
        self._operation_status.setText("Installation finished. Finalising…")

    def _on_failed(self, message: str) -> None:
        self._log.appendPlainText(f"ERROR: {message}")
        outcome = "cancelled" if message == _INSTALL_CANCELLED_MESSAGE else "failure"
        self._pending_result = (outcome, message)
        self._operation_status.setText("Installation stopped. Finalising…")

    def _on_worker_finished(self) -> None:
        worker = self._worker
        if worker is None:
            return

        result = self._pending_result
        close_requested = self._close_requested
        self._worker = None
        self._release_worker(worker)
        self._reset_operation_ui()

        if close_requested:
            self._close_requested = False
            self.close()
            return

        refresh_failed = False
        try:
            self._refresh_status()
        except Exception:
            logger.exception("Could not refresh runtime status after installation")
            refresh_failed = True

        if result is not None:
            outcome, message = result
            if outcome == "success":
                QMessageBox.information(self, "Runtime Ready", message)
            elif outcome == "cancelled":
                QMessageBox.information(self, "Installation Cancelled", message)
            else:
                QMessageBox.warning(self, "Installation Failed", message)

        if refresh_failed:
            QMessageBox.warning(
                self,
                "Runtime Status Unavailable",
                "Runtime status could not be refreshed after the operation. "
                "Check the Automadin log and reopen Runtime Manager.",
            )

    def _reset_operation_ui(self) -> None:
        self._progress.setVisible(False)
        self._operation_status.setVisible(False)
        self._cancel_button.setVisible(False)
        self._cancel_button.setEnabled(True)
        self._cancel_button.setText("Cancel")
        self._close_button.setEnabled(True)
        self._browser_button.setEnabled(True)
        self._pending_result = None
        self._cancel_requested = False
        self._update_adb_button()

    def _report_start_failure(self) -> None:
        self._reset_operation_ui()
        self._log.appendPlainText(f"ERROR: {_INSTALL_START_FAILURE_MESSAGE}")
        QMessageBox.warning(
            self,
            "Installation Could Not Start",
            _INSTALL_START_FAILURE_MESSAGE,
        )

    @staticmethod
    def _release_worker(
        worker: _InstallWorker,
        *,
        context: str = "after completion",
    ) -> None:
        try:
            worker.deleteLater()
        except Exception:
            logger.exception(
                "Could not release runtime installation worker %s",
                context,
            )

    def _cancel_install(self) -> None:
        self._request_cancellation(close_after=False)

    def _request_cancellation(self, *, close_after: bool) -> None:
        worker = self._worker
        if worker is None or not worker.isRunning():
            return

        if not self._cancel_requested:
            try:
                worker.cancel()
            except Exception:
                logger.exception("Could not request runtime installation cancellation")
                self._operation_status.setText(
                    "Cancellation could not be requested. Check the Automadin log "
                    "and try again."
                )
                self._operation_status.setVisible(True)
                return
            self._cancel_requested = True
            self._log.appendPlainText("Cancellation requested. Waiting for installer to stop…")

        if close_after:
            self._close_requested = True
            self._operation_status.setText(
                "Cancellation requested. Waiting for the installer to stop safely; "
                "this window will close automatically."
            )
            self._close_button.setText("Waiting for installer…")
            self._close_button.setEnabled(False)
        else:
            self._operation_status.setText(
                "Cancellation requested. Waiting for the installer to stop safely."
            )
        self._operation_status.setVisible(True)
        self._cancel_button.setText("Cancelling…")
        self._cancel_button.setEnabled(False)

    def accept(self) -> None:
        if self._worker is not None:
            self.close()
            return
        super().accept()

    def reject(self) -> None:
        # Escape calls QDialog.reject() directly and otherwise bypasses closeEvent.
        if self._worker is not None:
            self.close()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            if self._close_requested:
                event.ignore()
                return
            reply = QMessageBox.question(
                self,
                "Cancel Installation?",
                "A component is still installing. Cancel it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._request_cancellation(close_after=True)
            event.ignore()
            return
        if worker is not None:
            # The native thread has stopped, but its queued ``finished`` cleanup has
            # not reached the GUI thread yet. Keep this dialog alive until it does.
            self._close_requested = True
            event.ignore()
            return
        super().closeEvent(event)
