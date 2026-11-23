"""Read-only Android file browser."""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.session import DeviceSessionService
from automadin.models.android_file import AndroidFileEntry, AndroidFilePreview
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.services.android_file_preview import preview_local_file, preview_text
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import (
    DeviceOperationConflictError,
    DeviceOperationLease,
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    default_device_operation_registry,
    device_operation_key,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)

MAX_GENERAL_PULL_BYTES = 20 * 1024 * 1024
TEMP_PULL_PREFIX = "automadin-android-file-"
def _android_browser_tab_style() -> str:
    return (
        "QTabWidget#androidDetailTabs QTabBar::tab {"
        f"background-color: {COLORS['bg_surface']};"
        f"color: {COLORS['text_secondary']};"
        "padding: 8px 18px;"
        "border: none;"
        "border-bottom: 2px solid transparent;"
        "}"
        "QTabWidget#androidDetailTabs QTabBar::tab:selected {"
        f"background-color: {COLORS['selection_bg']};"
        f"color: {COLORS['selection_text']};"
        f"border-bottom: 2px solid {COLORS['selection_border']};"
        "}"
        "QTabWidget#androidDetailTabs QTabBar::tab:hover {"
        f"background-color: {COLORS['bg_hover']};"
        "}"
    )


def _android_browser_table_style() -> str:
    return (
        "QTableWidget#androidFileTable::item:selected {"
        f"background-color: {COLORS['selection_bg']};"
        f"color: {COLORS['selection_text']};"
        f"border: 1px solid {COLORS['selection_border']};"
        "}"
    )


class _AndroidListThread(QThread):
    succeeded = Signal(str, list)
    failed = Signal(str)

    def __init__(
        self,
        session_service: DeviceSessionService,
        session: DeviceSession,
        path: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_service = session_service
        self._session = session
        self._path = path

    def run(self) -> None:
        try:
            adb_data = self._session.get_data("adb_data")
            if adb_data is None:
                self.failed.emit("No ADB session data")
                return
            entries = self._session_service._adb_engine.list_directory(
                adb_data, self._path
            )
            self.succeeded.emit(self._path, entries)
        except Exception:
            logger.exception("Android directory listing failed")
            self.failed.emit(
                "The directory could not be loaded. Check the ADB connection "
                "and try again."
            )


class _AndroidPreviewThread(QThread):
    succeeded = Signal(str, object, str)
    failed = Signal(str)

    def __init__(
        self,
        session_service: DeviceSessionService,
        session: DeviceSession,
        path: str,
        force_text: bool = False,
        allow_pull: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_service = session_service
        self._session = session
        self._path = path
        self._force_text = force_text
        self._allow_pull = allow_pull

    def run(self) -> None:
        temp_dir: str | None = None
        try:
            adb_data = self._session.get_data("adb_data")
            if adb_data is None:
                self.failed.emit("No ADB session data")
                return

            if self._force_text or not self._allow_pull:
                text, truncated = self._session_service._adb_engine.read_file_text(
                    adb_data, self._path
                )
                self.succeeded.emit(
                    self._path,
                    preview_text(self._path, text, truncated),
                    text,
                )
                return

            temp_dir = tempfile.mkdtemp(prefix=TEMP_PULL_PREFIX)
            pulled = self._session_service._adb_engine.pull_file_to_temp(
                adb_data, self._path, temp_dir=temp_dir
            )
            preview = preview_local_file(self._path, pulled.local_path)
            self.succeeded.emit(self._path, preview, "")
        except Exception:
            logger.exception("Android file preview failed")
            self.failed.emit(
                "The file could not be previewed. Check its permissions and the "
                "ADB connection, then try again."
            )
        finally:
            if temp_dir is not None:
                _cleanup_pull_temp_dir(temp_dir)


class _AndroidRootThread(QThread):
    succeeded = Signal(str, str)
    failed = Signal(str)

    def __init__(
        self,
        session_service: DeviceSessionService,
        session: DeviceSession,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_service = session_service
        self._session = session

    def run(self) -> None:
        try:
            adb_data = self._session.get_data("adb_data")
            if adb_data is None:
                self.failed.emit("No ADB session data")
                return
            output = self._session_service._adb_engine.restart_adbd_as_root(adb_data)
            state = self._session_service._adb_engine.root_state(adb_data)
            self.succeeded.emit(output, state.label)
        except Exception:
            logger.exception("ADB root failed")
            self.failed.emit(
                "ADB root did not complete. This target may not support root; "
                "review Diagnostics for details."
            )


def _cleanup_pull_temp_dir(temp_dir: str) -> None:
    path = Path(temp_dir)
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved = path.resolve()
    except OSError:
        return

    if resolved.parent != temp_root or not resolved.name.startswith(TEMP_PULL_PREFIX):
        return

    shutil.rmtree(resolved, ignore_errors=True)


class AndroidFileBrowserView(QWidget):
    def __init__(
        self,
        db: DatabaseService,
        session_service: DeviceSessionService,
        device_selector=None,
        *,
        device_operation_registry: DeviceOperationLeaseRegistry | None = None,
    ) -> None:
        super().__init__()
        self._db = db
        self._session_service = session_service
        self._device_selector = device_selector
        self._device_operation_registry = (
            device_operation_registry or default_device_operation_registry()
        )
        self._operation_leases: dict[QThread, DeviceOperationLease] = {}
        self._pending_operation_leases: list[DeviceOperationLease] = []
        self._current_path = "/sdcard"
        self._entries: list[AndroidFileEntry] = []
        self._list_thread: _AndroidListThread | None = None
        self._read_thread: _AndroidPreviewThread | None = None
        self._root_thread: _AndroidRootThread | None = None
        self._refresh_after_root = False
        self._shutting_down = False
        self._shutdown_abort_requested = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self._status_label = QLabel("Connect an Android device to browse files")
        self._status_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        status_row.addWidget(self._status_label, 1)

        self._root_btn = QPushButton("ADB Root")
        self._root_btn.setProperty("cssClass", "warning")
        self._root_btn.clicked.connect(self._on_root_clicked)
        status_row.addWidget(self._root_btn)
        layout.addLayout(status_row)

        self._detail_tabs = QTabWidget()
        self._detail_tabs.setObjectName("androidDetailTabs")
        self._detail_tabs.setStyleSheet(_android_browser_tab_style())
        layout.addWidget(self._detail_tabs, 1)

        self._files_tab = QWidget()
        self._detail_tabs.addTab(self._files_tab, "Files")
        self._build_files_tab()

    def _build_files_tab(self) -> None:
        layout = QVBoxLayout(self._files_tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self._path_edit = QLineEdit(self._current_path)
        self._path_edit.returnPressed.connect(self._open_path_from_edit)
        path_row.addWidget(self._path_edit, 1)

        self._up_btn = QPushButton("Up")
        self._up_btn.clicked.connect(self._go_up)
        path_row.addWidget(self._up_btn)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh_current_path)
        path_row.addWidget(self._refresh_btn)
        layout.addLayout(path_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._file_table = QTableWidget(0, 5)
        self._file_table.setObjectName("androidFileTable")
        self._file_table.setHorizontalHeaderLabels(
            ["Name", "Size", "Mode", "Owner", "Modified"]
        )
        self._file_table.setAlternatingRowColors(True)
        self._file_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._file_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._file_table.setStyleSheet(_android_browser_table_style())
        self._file_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._file_table.verticalHeader().setVisible(False)
        self._file_table.horizontalHeader().setStretchLastSection(True)
        self._file_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._file_table.itemDoubleClicked.connect(self._on_item_activated)
        splitter.addWidget(self._file_table)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)

        self._preview_title = QLabel("Preview")
        self._preview_title.setStyleSheet(f"color: {COLORS['text_secondary']};")
        right_layout.addWidget(self._preview_title)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        right_layout.addWidget(self._preview, 1)

        splitter.addWidget(right)
        splitter.setSizes([700, 450])
        layout.addWidget(splitter, 1)

    def shutdown(self, wait_ms: int = 0) -> bool:
        """Request worker interruption without blocking past one shared deadline."""
        self._shutting_down = True
        self._shutdown_abort_requested = False
        self._set_controls_enabled(False)

        threads = tuple(
            thread
            for thread in (self._list_thread, self._read_thread, self._root_thread)
            if thread is not None
        )
        for thread in threads:
            thread.requestInterruption()

        if wait_ms > 0:
            deadline = time.monotonic() + (wait_ms / 1000)
            for thread in threads:
                if not thread.isRunning():
                    continue
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    break
                thread.wait(remaining_ms)

        self._retry_operation_lease_cleanup()
        finished = (
            not any(thread.isRunning() for thread in threads)
            and not self._operation_leases
            and not self._pending_operation_leases
        )
        if not finished:
            # MainWindow keeps the application open when this returns false.
            # Restore this view after the retained workers really finish.
            self._shutdown_abort_requested = True
        return finished

    def abort_shutdown(self) -> bool:
        """Keep this view usable when its owner aborts or refuses application close."""
        self._shutdown_abort_requested = True
        return self._restore_after_aborted_shutdown()

    def _restore_after_aborted_shutdown(self) -> bool:
        if not self._shutdown_abort_requested:
            return False
        threads = (self._list_thread, self._read_thread, self._root_thread)
        if any(thread is not None and thread.isRunning() for thread in threads):
            return False
        self._shutdown_abort_requested = False
        self._shutting_down = False
        self._set_controls_enabled(True)
        self._set_status("Android file operations are ready")
        return True

    def _selected_android_session(self) -> DeviceSession | None:
        if self._device_selector is None:
            return None
        session = self._device_selector.get_selected_session()
        if session is None or not isinstance(session.device, AndroidDevice):
            return None
        return session

    def _set_status(self, text: str, error: bool = False) -> None:
        color = COLORS["error"] if error else COLORS["text_secondary"]
        self._status_label.setStyleSheet(f"color: {color};")
        self._status_label.setText(text)

    def _is_list_running(self) -> bool:
        return self._list_thread is not None and self._list_thread.isRunning()

    def _is_read_running(self) -> bool:
        return self._read_thread is not None and self._read_thread.isRunning()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self._root_btn,
            self._refresh_btn,
            self._path_edit,
            self._up_btn,
            self._file_table,
        ):
            widget.setEnabled(enabled)

    def _acquire_operation_lease(
        self,
        session: DeviceSession,
        *,
        kind: str,
        label: str,
    ) -> DeviceOperationLease | None:
        self._retry_operation_lease_cleanup()
        device = session.device
        if not isinstance(device, AndroidDevice):
            self._set_status("Select and connect an Android device first", error=True)
            return None
        try:
            return self._device_operation_registry.acquire(
                device_operation_key(device),
                DeviceOperationUse(
                    kind=kind,
                    label=label,
                    operation_id=str(uuid4()),
                ),
            )
        except DeviceOperationConflictError as exc:
            self._set_status(
                f"Device busy with {exc.current.label}. Stop that operation and "
                "try again.",
                error=True,
            )
        except Exception:
            logger.exception("Could not reserve Android device operation")
            self._set_status(
                "The Android device could not be reserved. Stop other operations "
                "and try again.",
                error=True,
            )
        return None

    def _retain_operation_lease(
        self,
        thread: QThread,
        lease: DeviceOperationLease,
    ) -> None:
        self._operation_leases[thread] = lease

    def _release_operation_lease(self, thread: QThread) -> None:
        lease = self._operation_leases.get(thread)
        if lease is None:
            return
        if self._try_release_lease(lease):
            if self._operation_leases.get(thread) is lease:
                self._operation_leases.pop(thread, None)

    @staticmethod
    def _try_release_lease(lease: DeviceOperationLease) -> bool:
        try:
            lease.release()
        except BaseException:
            logger.exception("Could not release Android file operation lease")
            return False
        return True

    def _release_unretained_lease(self, lease: DeviceOperationLease) -> None:
        if self._try_release_lease(lease):
            return
        if lease not in self._pending_operation_leases:
            self._pending_operation_leases.append(lease)

    def _retry_operation_lease_cleanup(self) -> None:
        for thread in list(self._operation_leases):
            if not thread.isRunning():
                self._release_operation_lease(thread)
        for lease in list(self._pending_operation_leases):
            if self._try_release_lease(lease):
                self._pending_operation_leases.remove(lease)

    def _refresh_current_path(self) -> None:
        self._open_path(self._path_edit.text().strip() or self._current_path)

    def _open_path_from_edit(self) -> None:
        self._open_path(self._path_edit.text().strip() or "/")

    def _open_path(self, path: str) -> None:
        if self._shutting_down:
            return
        session = self._selected_android_session()
        if session is None:
            self._set_status("Select and connect an Android device first", error=True)
            return
        if self._is_list_running():
            self._set_status("Directory listing already in progress")
            return
        lease = self._acquire_operation_lease(
            session,
            kind="android-file-list",
            label="Android file listing",
        )
        if lease is None:
            return

        thread: _AndroidListThread | None = None
        try:
            self._current_path = path
            self._path_edit.setText(path)
            self._file_table.setRowCount(0)
            self._entries = []
            self._set_status(f"Loading {path}...")
            thread = _AndroidListThread(
                self._session_service, session, path, self
            )
            self._list_thread = thread
            self._retain_operation_lease(thread, lease)
            thread.succeeded.connect(self._on_list_succeeded)
            thread.failed.connect(self._on_list_failed)
            thread.finished.connect(self._on_list_thread_finished)
            thread.start()
        except BaseException:
            logger.exception("Android directory-listing worker could not be started")
            if qthread_start_may_have_succeeded(thread):
                thread.requestInterruption()
                self._set_status(
                    "The directory listing reported a startup problem. Waiting "
                    "for it to finish safely; review Diagnostics before retrying.",
                    error=True,
                )
                return
            if (
                thread is not None
                and self._operation_leases.get(thread) is lease
            ):
                self._release_operation_lease(thread)
            else:
                self._release_unretained_lease(lease)
            if self._list_thread is thread:
                self._list_thread = None
            if thread is not None:
                try:
                    thread.deleteLater()
                except BaseException:
                    logger.exception(
                        "Could not dispose failed directory-listing worker"
                    )
            self._set_status(
                "The directory listing could not be started. Try again.",
                error=True,
            )

    def _go_up(self) -> None:
        path = self._current_path.rstrip("/")
        if not path or path == "/":
            self._open_path("/")
            return
        self._open_path(path.rsplit("/", 1)[0] or "/")

    def _on_list_succeeded(
        self, path: str, entries: list[AndroidFileEntry]
    ) -> None:
        if self._shutting_down:
            return
        if path != self._current_path:
            return
        self._entries = entries
        self._file_table.setRowCount(len(entries))

        for row, entry in enumerate(entries):
            values = [
                entry.name,
                entry.display_size,
                entry.mode,
                entry.owner,
                entry.modified,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row)
                if entry.is_dir:
                    item.setForeground(QColor(COLORS["accent"]))
                self._file_table.setItem(row, column, item)

        self._file_table.resizeColumnsToContents()
        self._set_status(f"Loaded {len(entries)} item(s) from {path}")

    def _on_list_failed(self, error: str) -> None:
        if self._shutting_down:
            return
        self._set_status(error, error=True)
        self._file_table.setRowCount(0)
        self._entries = []

    def _clear_list_thread(self, thread: _AndroidListThread) -> None:
        self._release_operation_lease(thread)
        if self._list_thread is thread:
            self._list_thread = None
        self._restore_after_aborted_shutdown()

    def _on_list_thread_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, _AndroidListThread):
            self._clear_list_thread(thread)

    def _on_item_activated(self, item: QTableWidgetItem) -> None:
        row = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, int) or row < 0 or row >= len(self._entries):
            return

        entry = self._entries[row]
        if entry.is_dir:
            self._open_path(entry.path)
            return

        allow_pull = entry.is_regular_file and entry.size <= MAX_GENERAL_PULL_BYTES
        self._preview_file(
            entry.path,
            force_text=not entry.is_regular_file,
            allow_pull=allow_pull,
        )

    def _preview_file(
        self,
        path: str,
        force_text: bool = False,
        allow_pull: bool = True,
    ) -> None:
        if self._shutting_down:
            return
        session = self._selected_android_session()
        if session is None:
            self._set_status("Select and connect an Android device first", error=True)
            return
        if self._is_read_running():
            self._set_status("File read already in progress")
            return
        lease = self._acquire_operation_lease(
            session,
            kind="android-file-preview",
            label="Android file preview",
        )
        if lease is None:
            return

        thread: _AndroidPreviewThread | None = None
        try:
            self._preview_title.setText(path)
            self._preview.setPlainText("Loading...")
            self._set_status(f"Reading {path}...")
            thread = _AndroidPreviewThread(
                self._session_service,
                session,
                path,
                force_text=force_text,
                allow_pull=allow_pull,
                parent=self,
            )
            self._read_thread = thread
            self._retain_operation_lease(thread, lease)
            thread.succeeded.connect(self._on_preview_succeeded)
            thread.failed.connect(self._on_preview_failed)
            thread.finished.connect(self._on_read_thread_finished)
            thread.start()
        except BaseException:
            logger.exception("Android file-preview worker could not be started")
            if qthread_start_may_have_succeeded(thread):
                thread.requestInterruption()
                self._set_status(
                    "The file preview reported a startup problem. Waiting for "
                    "it to finish safely; review Diagnostics before retrying.",
                    error=True,
                )
                return
            if (
                thread is not None
                and self._operation_leases.get(thread) is lease
            ):
                self._release_operation_lease(thread)
            else:
                self._release_unretained_lease(lease)
            if self._read_thread is thread:
                self._read_thread = None
            if thread is not None:
                try:
                    thread.deleteLater()
                except BaseException:
                    logger.exception("Could not dispose failed file-preview worker")
            self._set_status(
                "The file preview could not be started. Try again.",
                error=True,
            )

    def _on_preview_succeeded(
        self, path: str, preview: AndroidFilePreview, _raw_text: str
    ) -> None:
        if self._shutting_down:
            return
        self._preview_title.setText(f"{preview.title}: {path}")
        self._preview.setPlainText(preview.text)
        self._set_status(f"Previewed {path}")

    def _on_preview_failed(self, error: str) -> None:
        if self._shutting_down:
            return
        self._set_status(error, error=True)
        self._preview.setPlainText(error)

    def _clear_read_thread(self, thread: _AndroidPreviewThread) -> None:
        self._release_operation_lease(thread)
        if self._read_thread is thread:
            self._read_thread = None
        self._restore_after_aborted_shutdown()

    def _on_read_thread_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, _AndroidPreviewThread):
            self._clear_read_thread(thread)

    def _on_root_clicked(self) -> None:
        if self._shutting_down:
            return
        session = self._selected_android_session()
        if session is None:
            self._set_status("Select and connect an Android device first", error=True)
            return
        if session.get_data("adb_data") is None:
            self._set_status("No ADB session data", error=True)
            return
        if self._root_thread is not None and self._root_thread.isRunning():
            self._set_status("ADB root already in progress")
            return

        reply = QMessageBox.question(
            self,
            "Restart ADB As Root",
            "Run adb root for the selected Android device? The device may briefly reconnect.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        lease = self._acquire_operation_lease(
            session,
            kind="android-adb-root",
            label="ADB root",
        )
        if lease is None:
            return

        thread: _AndroidRootThread | None = None
        try:
            self._set_status("Restarting ADB as root...")
            self._root_btn.setEnabled(False)
            self._refresh_after_root = False
            thread = _AndroidRootThread(self._session_service, session, self)
            self._root_thread = thread
            self._retain_operation_lease(thread, lease)
            thread.succeeded.connect(self._on_root_succeeded)
            thread.failed.connect(self._on_root_failed)
            thread.finished.connect(self._on_root_thread_finished)
            thread.start()
        except BaseException:
            logger.exception("ADB-root worker could not be started")
            if qthread_start_may_have_succeeded(thread):
                thread.requestInterruption()
                self._set_status(
                    "ADB root reported a startup problem. Waiting for it to "
                    "finish safely; review Diagnostics before retrying.",
                    error=True,
                )
                return
            if (
                thread is not None
                and self._operation_leases.get(thread) is lease
            ):
                self._release_operation_lease(thread)
            else:
                self._release_unretained_lease(lease)
            if self._root_thread is thread:
                self._root_thread = None
            if thread is not None:
                try:
                    thread.deleteLater()
                except BaseException:
                    logger.exception("Could not dispose failed ADB-root worker")
            self._root_btn.setEnabled(True)
            self._set_status(
                "ADB root could not be started. Try again.",
                error=True,
            )

    def _on_root_succeeded(self, output: str, state_label: str) -> None:
        if self._shutting_down:
            return
        message = output or "adb root completed"
        self._set_status(f"{message} | current shell: {state_label}")
        # Refresh only after QThread.finished releases the root operation lease;
        # otherwise the listing conflicts with the root operation itself.
        self._refresh_after_root = True

    def _on_root_failed(self, error: str) -> None:
        if self._shutting_down:
            return
        self._refresh_after_root = False
        self._set_status(error, error=True)

    def _clear_root_thread(self, thread: _AndroidRootThread) -> None:
        self._release_operation_lease(thread)
        if self._root_thread is thread:
            if not self._shutting_down:
                self._root_btn.setEnabled(True)
            self._root_thread = None
        self._restore_after_aborted_shutdown()

    def _on_root_thread_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, _AndroidRootThread):
            refresh = self._refresh_after_root
            self._refresh_after_root = False
            self._clear_root_thread(thread)
            if refresh and not self._shutting_down:
                self._refresh_current_path()
