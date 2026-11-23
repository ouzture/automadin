"""Configure and test Automadin's default Pushover destination."""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.services.database import DatabaseService
from automadin.services.pushover import (
    PUSHOVER_DEVICE_SETTING,
    PushoverConfiguration,
    PushoverCredentials,
    PushoverError,
    PushoverMessage,
    delete_pushover_configuration,
    load_pushover_credentials,
    save_pushover_configuration,
    send_pushover_notification,
)
from automadin.services.qt_worker_lifecycle import qthread_start_may_have_succeeded
from automadin.ui.theme import COLORS


logger = logging.getLogger(__name__)
_PUSHOVER_DASHBOARD = "https://pushover.net/"
_SAFE_TEST_FAILURE = (
    "Automadin could not send the test notification. Check the keys, optional "
    "device name, and internet connection."
)
_SAFE_START_FAILURE = (
    "The Pushover test could not start. Close this dialog, reopen it, and try again."
)


class _PushoverTestThread(QThread):
    succeeded = Signal()
    failed = Signal(str)

    def __init__(
        self,
        configuration: PushoverConfiguration,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._configuration = configuration

    def run(self) -> None:
        try:
            send_pushover_notification(
                self._configuration,
                PushoverMessage(
                    title="Automadin setup test",
                    message="Pushover is configured correctly for Automadin.",
                ),
            )
        except PushoverError as exc:
            self.failed.emit(str(exc))
        except BaseException:
            logger.exception("Unexpected Pushover test failure")
            self.failed.emit(_SAFE_TEST_FAILURE)
        else:
            self.succeeded.emit()


class PushoverSetupDialog(QDialog):
    """Edit secrets without copying them into Automadin's SQLite database."""

    def __init__(self, db: DatabaseService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._thread: _PushoverTestThread | None = None
        self._tested_fingerprint: tuple[str, str, str] | None = None
        self._active_fingerprint: tuple[str, str, str] | None = None
        self._startup_uncertain = False
        self._saved = False

        self.setWindowTitle("Pushover Notifications")
        self.resize(620, 390)
        self._build_ui()
        self._load_current_configuration()

    @property
    def saved(self) -> bool:
        return self._saved

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        note = QLabel(
            "Send flow notifications to Pushover on your iPhone. Create an "
            "application named Automadin in the Pushover dashboard, then enter "
            "its API token and your user key below."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(note)

        dashboard = QPushButton("Open Pushover dashboard")
        dashboard.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_PUSHOVER_DASHBOARD))
        )
        layout.addWidget(dashboard)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._app_token_edit = QLineEdit()
        self._app_token_edit.setObjectName("pushoverAppToken")
        self._app_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._app_token_edit.setPlaceholderText("30-character application API token")
        form.addRow("Application token", self._app_token_edit)

        self._user_key_edit = QLineEdit()
        self._user_key_edit.setObjectName("pushoverUserKey")
        self._user_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._user_key_edit.setPlaceholderText("30-character user or group key")
        form.addRow("User key", self._user_key_edit)

        self._device_edit = QLineEdit()
        self._device_edit.setObjectName("pushoverDevice")
        self._device_edit.setPlaceholderText("Optional; empty sends to all Pushover devices")
        form.addRow("Device name", self._device_edit)
        layout.addLayout(form)

        self._show_keys = QCheckBox("Show keys")
        self._show_keys.toggled.connect(self._set_keys_visible)
        layout.addWidget(self._show_keys)

        security = QLabel(
            "The token and user key are saved in Windows Credential Manager. "
            "They are not stored in flows, the Automadin database, or portable backups."
        )
        security.setWordWrap(True)
        security.setStyleSheet(f"color: {COLORS['text_tertiary']};")
        layout.addWidget(security)

        self._status_label = QLabel()
        self._status_label.setObjectName("pushoverStatus")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        buttons = QHBoxLayout()
        self._test_button = QPushButton("Send Test")
        self._test_button.setObjectName("pushoverTestButton")
        self._test_button.clicked.connect(self._test)
        buttons.addWidget(self._test_button)

        self._remove_button = QPushButton("Remove Configuration")
        self._remove_button.clicked.connect(self._remove)
        buttons.addWidget(self._remove_button)
        buttons.addStretch()

        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self._cancel_button)

        self._save_button = QPushButton("Save")
        self._save_button.setObjectName("pushoverSaveButton")
        self._save_button.setProperty("cssClass", "primary")
        self._save_button.clicked.connect(self._save)
        buttons.addWidget(self._save_button)
        layout.addLayout(buttons)

        for edit in (self._app_token_edit, self._user_key_edit, self._device_edit):
            edit.textChanged.connect(self._configuration_changed)

    def _load_current_configuration(self) -> None:
        try:
            credentials = load_pushover_credentials()
        except PushoverError as exc:
            self._set_status(str(exc), COLORS["error"])
            self._remove_button.setEnabled(True)
            return
        device = self._db.get_app_setting(PUSHOVER_DEVICE_SETTING, "") or ""
        for edit in (self._app_token_edit, self._user_key_edit, self._device_edit):
            edit.blockSignals(True)
        if credentials is not None:
            self._app_token_edit.setText(credentials.app_token)
            self._user_key_edit.setText(credentials.user_key)
        self._device_edit.setText(device)
        for edit in (self._app_token_edit, self._user_key_edit, self._device_edit):
            edit.blockSignals(False)
        self._remove_button.setEnabled(credentials is not None)
        self._set_status(
            (
                "Saved credentials loaded. Send a test after making changes."
                if credentials is not None
                else "Enter both keys, send a test, then save the configuration."
            ),
            COLORS["text_tertiary"],
        )

    def _set_keys_visible(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self._app_token_edit.setEchoMode(mode)
        self._user_key_edit.setEchoMode(mode)

    def _configuration_changed(self) -> None:
        if self._tested_fingerprint is not None:
            self._tested_fingerprint = None
            self._set_status(
                "Configuration changed. Send another test before saving.",
                COLORS["warning"],
            )

    def _fingerprint(self) -> tuple[str, str, str]:
        return (
            self._app_token_edit.text().strip(),
            self._user_key_edit.text().strip(),
            self._device_edit.text().strip(),
        )

    def _configuration(self) -> PushoverConfiguration:
        app_token, user_key, device = self._fingerprint()
        return PushoverConfiguration(
            PushoverCredentials(app_token, user_key),
            device,
        )

    def _test(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        try:
            configuration = self._configuration()
        except PushoverError as exc:
            self._set_status(str(exc), COLORS["error"])
            return

        self._set_busy(True)
        self._set_status("Sending a test notification…", COLORS["warning"])
        self._active_fingerprint = self._fingerprint()
        self._startup_uncertain = False
        thread: _PushoverTestThread | None = None
        try:
            thread = _PushoverTestThread(configuration, self)
            thread.succeeded.connect(self._test_succeeded)
            thread.failed.connect(self._test_failed)
            thread.finished.connect(
                lambda current=thread: self._thread_finished(current)
            )
            self._thread = thread
            thread.start()
        except BaseException:
            logger.exception("Pushover test worker could not be started")
            if qthread_start_may_have_succeeded(thread):
                self._startup_uncertain = True
                self._set_status(
                    "The Pushover test reported a startup problem. Wait for the "
                    "background operation to finish before closing this dialog.",
                    COLORS["error"],
                )
                return
            if self._thread is thread:
                self._thread = None
            if thread is not None:
                thread.deleteLater()
            self._active_fingerprint = None
            self._set_busy(False)
            self._set_status(_SAFE_START_FAILURE, COLORS["error"])

    def _test_succeeded(self) -> None:
        if self._active_fingerprint == self._fingerprint():
            self._tested_fingerprint = self._active_fingerprint
            self._set_status(
                "Test sent successfully. Confirm it arrived on your iPhone, then save.",
                COLORS["success"],
            )

    def _test_failed(self, message: str) -> None:
        safe_message = message if message else _SAFE_TEST_FAILURE
        self._tested_fingerprint = None
        self._set_status(safe_message, COLORS["error"])

    def _thread_finished(self, thread: _PushoverTestThread) -> None:
        if self._thread is thread:
            self._thread = None
            self._active_fingerprint = None
            self._startup_uncertain = False
            self._set_busy(False)
        thread.deleteLater()

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self._app_token_edit,
            self._user_key_edit,
            self._device_edit,
            self._show_keys,
            self._test_button,
            self._remove_button,
            self._cancel_button,
            self._save_button,
        ):
            widget.setEnabled(not busy)
        if busy:
            self._test_button.setText("Sending…")
        else:
            self._test_button.setText("Send Test")
            self._remove_button.setEnabled(bool(self._app_token_edit.text().strip()))

    def _save(self) -> None:
        try:
            configuration = self._configuration()
        except PushoverError as exc:
            self._set_status(str(exc), COLORS["error"])
            return
        if self._tested_fingerprint != self._fingerprint():
            QMessageBox.warning(
                self,
                "Test Required",
                "Send a successful test with the current values before saving.",
            )
            return
        try:
            save_pushover_configuration(self._db, configuration)
        except PushoverError as exc:
            self._set_status(str(exc), COLORS["error"])
            return
        self._saved = True
        self.accept()

    def _remove(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Remove Pushover Configuration",
                "Remove Automadin's saved Pushover credentials? Existing Send "
                "Notification commands will fail until Pushover is configured again.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            delete_pushover_configuration(self._db)
        except PushoverError as exc:
            self._set_status(str(exc), COLORS["error"])
            return
        self._app_token_edit.clear()
        self._user_key_edit.clear()
        self._device_edit.clear()
        self._tested_fingerprint = None
        self._saved = True
        self._remove_button.setEnabled(False)
        self._set_status("Pushover configuration removed.", COLORS["success"])

    def _set_status(self, text: str, color: str) -> None:
        self._status_label.setStyleSheet(f"color: {color};")
        self._status_label.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None and self._thread.isRunning():
            event.ignore()
            return
        super().closeEvent(event)
