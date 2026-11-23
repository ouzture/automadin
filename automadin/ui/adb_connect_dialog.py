"""Dialog for running ``adb connect`` against Android TCP targets."""

from __future__ import annotations

import json
import logging

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.adb_engine import AdbDeviceEngine
from automadin.services.database import DatabaseService
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 5555
_HISTORY_LIMIT = 5
_SETTING_HOST = "adb_connect_host"
_SETTING_PORT = "adb_connect_port"
_SETTING_HISTORY = "adb_connect_history"
_SAFE_CONNECT_FAILURE = (
    "ADB could not connect to this target. Check the host, port, and device "
    "network settings, then try again."
)
_SAFE_START_FAILURE = (
    "ADB Connect could not start. Close this dialog, reopen it, and try again."
)
_SAFE_START_UNCERTAIN = (
    "ADB Connect reported a startup problem. Automadin is waiting for the "
    "background operation to finish safely; review Diagnostics before trying again."
)


class _AdbConnectThread(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        host: str,
        port: int,
        adb_path: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._port = port
        self._adb_path = adb_path

    def run(self) -> None:
        try:
            output = AdbDeviceEngine(adb_path=self._adb_path).connect_tcp(
                self._host,
                self._port,
            )
            self.succeeded.emit(output)
        except BaseException:
            logger.exception(
                "ADB connect worker failed for %s:%s",
                self._host,
                self._port,
            )
            self.failed.emit(_SAFE_CONNECT_FAILURE)


class AdbConnectDialog(QDialog):
    """Run ``adb connect`` and remember recently used targets."""

    def __init__(self, db: DatabaseService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._thread: _AdbConnectThread | None = None
        self._startup_uncertain = False
        self._connected = False

        self.setWindowTitle("ADB Connect")
        self.resize(430, 250)
        self._build_ui()
        self._load_defaults()
        self._populate_history()

    @property
    def connected(self) -> bool:
        return self._connected

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        note = QLabel(
            "Connect an Android emulator or device exposed over TCP, then refresh "
            "the Android device list."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(note)

        self._history_combo = QComboBox()
        self._history_combo.setPlaceholderText("Recent ADB targets")
        self._history_combo.currentIndexChanged.connect(self._on_history_selected)
        layout.addWidget(self._history_combo)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("localhost")
        form.addRow("Host", self._host_edit)

        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(_DEFAULT_PORT)
        form.addRow("Port", self._port_spin)
        layout.addLayout(form)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(f"color: {COLORS['text_tertiary']};")
        layout.addWidget(self._status_label)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setProperty("cssClass", "primary")
        self._connect_btn.clicked.connect(self._connect)
        button_row.addWidget(self._connect_btn)

        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.accept)
        button_row.addWidget(self._close_btn)
        layout.addLayout(button_row)

    def _load_defaults(self) -> None:
        host = self._db.get_app_setting(_SETTING_HOST, _DEFAULT_HOST) or _DEFAULT_HOST
        port_text = self._db.get_app_setting(_SETTING_PORT, str(_DEFAULT_PORT))
        try:
            port = int(port_text or _DEFAULT_PORT)
        except ValueError:
            port = _DEFAULT_PORT

        self._host_edit.setText(host)
        self._port_spin.setValue(max(1, min(65535, port)))

    def _load_history(self) -> list[dict[str, object]]:
        raw = self._db.get_app_setting(_SETTING_HISTORY, "[]") or "[]"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []

        history: list[dict[str, object]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host", "")).strip()
            try:
                port = int(item.get("port", _DEFAULT_PORT))
            except (TypeError, ValueError):
                continue
            if host and 1 <= port <= 65535:
                history.append({"host": host, "port": port})
        return history[:_HISTORY_LIMIT]

    def _populate_history(self) -> None:
        self._history_combo.blockSignals(True)
        self._history_combo.clear()
        seen: set[str] = set()
        for item in self._load_history():
            host = str(item["host"])
            port = int(item["port"])
            key = f"{host}:{port}"
            if key in seen:
                continue
            seen.add(key)
            self._history_combo.addItem(key, (host, port))
        self._history_combo.setCurrentIndex(-1)
        self._history_combo.blockSignals(False)

    def _on_history_selected(self, index: int) -> None:
        target = self._history_combo.itemData(index)
        if not target:
            return
        host, port = target
        self._host_edit.setText(str(host))
        self._port_spin.setValue(int(port))

    def _save_successful_target(self, host: str, port: int) -> None:
        self._db.set_app_setting(_SETTING_HOST, host)
        self._db.set_app_setting(_SETTING_PORT, str(port))

        new_history = [{"host": host, "port": port}]
        for item in self._load_history():
            if str(item["host"]).casefold() == host.casefold() and int(item["port"]) == port:
                continue
            new_history.append(item)
            if len(new_history) >= _HISTORY_LIMIT:
                break

        self._db.set_app_setting(_SETTING_HISTORY, json.dumps(new_history))
        self._populate_history()

    def _connect(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        host = self._host_edit.text().strip() or _DEFAULT_HOST
        port = int(self._port_spin.value())
        self._host_edit.setText(host)

        self._connect_btn.setEnabled(False)
        self._connect_btn.setText("Connecting...")
        self._close_btn.setEnabled(False)
        self._status_label.setStyleSheet(f"color: {COLORS['warning']};")
        self._status_label.setText(f"Running: adb connect {host}:{port}")
        self._startup_uncertain = False

        thread: _AdbConnectThread | None = None
        try:
            adb_path = AdbDeviceEngine(db=self._db).adb_path
            thread = _AdbConnectThread(host, port, adb_path, self)
            thread.succeeded.connect(
                lambda output, h=host, p=port: self._on_connect_succeeded(
                    h,
                    p,
                    output,
                )
            )
            thread.failed.connect(self._on_connect_failed)
            thread.finished.connect(
                lambda current=thread: self._on_thread_finished(current)
            )
            self._thread = thread
            thread.start()
        except BaseException:
            logger.exception("ADB connect worker could not be started")
            if qthread_start_may_have_succeeded(thread):
                # QThread.start() can fail after the native thread has already
                # begun.  Retain the object until its exact finished signal so
                # Qt cannot destroy a running thread.
                self._startup_uncertain = True
                thread.requestInterruption()
                self._show_connect_error(_SAFE_START_UNCERTAIN)
                return
            if self._thread is thread:
                self._thread = None
            if thread is not None:
                try:
                    thread.deleteLater()
                except BaseException:
                    logger.exception("Could not dispose failed ADB connect worker")
            self._restore_controls()
            self._show_connect_error(_SAFE_START_FAILURE)

    def _on_connect_succeeded(self, host: str, port: int, output: str) -> None:
        self._connected = True
        self._save_successful_target(host, port)
        self._status_label.setStyleSheet(f"color: {COLORS['success']};")
        self._status_label.setText(output)
        if not self._startup_uncertain:
            QMessageBox.information(
                self,
                "ADB Connected",
                f"{output}\n\nRefresh Android devices to discover the instance.",
            )

    def _on_connect_failed(self, error_msg: str) -> None:
        self._show_connect_error(
            error_msg,
            show_dialog=not self._startup_uncertain,
        )

    def _show_connect_error(
        self,
        error_msg: str,
        *,
        show_dialog: bool = True,
    ) -> None:
        """Display only an approved, non-sensitive operation failure."""
        safe_message = (
            error_msg
            if error_msg
            in {_SAFE_CONNECT_FAILURE, _SAFE_START_FAILURE, _SAFE_START_UNCERTAIN}
            else _SAFE_CONNECT_FAILURE
        )
        self._status_label.setStyleSheet(f"color: {COLORS['error']};")
        self._status_label.setText(safe_message)
        if show_dialog:
            QMessageBox.warning(self, "ADB Connect Failed", safe_message)

    def _restore_controls(self) -> None:
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("Connect")
        self._close_btn.setEnabled(True)

    def _on_thread_finished(self, thread: _AdbConnectThread) -> None:
        if self._thread is thread:
            self._thread = None
            self._startup_uncertain = False
            self._restore_controls()
        try:
            thread.deleteLater()
        except BaseException:
            logger.exception("Could not dispose finished ADB connect worker")

    def closeEvent(self, event) -> None:
        if self._thread is not None and self._thread.isRunning():
            event.ignore()
            return
        super().closeEvent(event)
