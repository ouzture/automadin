"""Configure and test Automadin's default Telegram destination."""

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
from automadin.services.qt_worker_lifecycle import qthread_start_may_have_succeeded
from automadin.services.telegram import (
    TELEGRAM_CHAT_ID_SETTING,
    TelegramConfiguration,
    TelegramCredentials,
    TelegramError,
    TelegramMessage,
    delete_telegram_configuration,
    load_telegram_credentials,
    save_telegram_configuration,
    send_telegram_message,
)
from automadin.ui.theme import COLORS


logger = logging.getLogger(__name__)
_BOTFATHER_URL = "https://t.me/BotFather"
_SAFE_TEST_FAILURE = (
    "Automadin could not send the test message. Check the bot token, chat ID, "
    "and internet connection."
)
_SAFE_START_FAILURE = (
    "The Telegram test could not start. Close this dialog, reopen it, and try again."
)


class _TelegramTestThread(QThread):
    succeeded = Signal()
    failed = Signal(str)

    def __init__(
        self,
        configuration: TelegramConfiguration,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._configuration = configuration

    def run(self) -> None:
        try:
            send_telegram_message(
                self._configuration,
                TelegramMessage(
                    title="Automadin setup test",
                    message="Telegram is configured correctly for Automadin.",
                ),
            )
        except TelegramError as exc:
            self.failed.emit(str(exc))
        except BaseException:
            logger.exception("Unexpected Telegram test failure")
            self.failed.emit(_SAFE_TEST_FAILURE)
        else:
            self.succeeded.emit()


class TelegramSetupDialog(QDialog):
    """Edit the Telegram token without copying it into SQLite or flows."""

    def __init__(self, db: DatabaseService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._thread: _TelegramTestThread | None = None
        self._tested_fingerprint: tuple[str, str] | None = None
        self._active_fingerprint: tuple[str, str] | None = None
        self._startup_uncertain = False
        self._saved = False

        self.setWindowTitle("Telegram Notifications")
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
            "Create a Telegram bot with BotFather and send that bot a message from "
            "the destination chat. Use Telegram's getUpdates Bot API method to find "
            "message.chat.id, then enter the bot token and that numeric chat ID below."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(note)

        botfather = QPushButton("Open Telegram BotFather")
        botfather.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_BOTFATHER_URL))
        )
        layout.addWidget(botfather)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._bot_token_edit = QLineEdit()
        self._bot_token_edit.setObjectName("telegramBotToken")
        self._bot_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._bot_token_edit.setPlaceholderText("Bot token supplied by BotFather")
        form.addRow("Bot token", self._bot_token_edit)

        self._chat_id_edit = QLineEdit()
        self._chat_id_edit.setObjectName("telegramChatId")
        self._chat_id_edit.setPlaceholderText("Numeric user or group chat ID")
        form.addRow("Chat ID", self._chat_id_edit)
        layout.addLayout(form)

        self._show_token = QCheckBox("Show bot token")
        self._show_token.toggled.connect(self._set_token_visible)
        layout.addWidget(self._show_token)

        security = QLabel(
            "The bot token is saved in Windows Credential Manager. The non-secret "
            "chat ID is saved in Automadin settings."
        )
        security.setWordWrap(True)
        security.setStyleSheet(f"color: {COLORS['text_tertiary']};")
        layout.addWidget(security)

        self._status_label = QLabel()
        self._status_label.setObjectName("telegramStatus")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        buttons = QHBoxLayout()
        self._test_button = QPushButton("Send Test")
        self._test_button.setObjectName("telegramTestButton")
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
        self._save_button.setObjectName("telegramSaveButton")
        self._save_button.setProperty("cssClass", "primary")
        self._save_button.clicked.connect(self._save)
        buttons.addWidget(self._save_button)
        layout.addLayout(buttons)

        self._bot_token_edit.textChanged.connect(self._configuration_changed)
        self._chat_id_edit.textChanged.connect(self._configuration_changed)

    def _load_current_configuration(self) -> None:
        try:
            credentials = load_telegram_credentials()
        except TelegramError as exc:
            self._set_status(str(exc), COLORS["error"])
            self._remove_button.setEnabled(True)
            return
        chat_id = self._db.get_app_setting(TELEGRAM_CHAT_ID_SETTING, "") or ""
        self._bot_token_edit.blockSignals(True)
        self._chat_id_edit.blockSignals(True)
        if credentials is not None:
            self._bot_token_edit.setText(credentials.bot_token)
        self._chat_id_edit.setText(chat_id)
        self._bot_token_edit.blockSignals(False)
        self._chat_id_edit.blockSignals(False)
        self._remove_button.setEnabled(credentials is not None)
        self._set_status(
            (
                "Saved configuration loaded. Send a test after making changes."
                if credentials is not None
                else "Enter the bot token and chat ID, send a test, then save."
            ),
            COLORS["text_tertiary"],
        )

    def _set_token_visible(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self._bot_token_edit.setEchoMode(mode)

    def _configuration_changed(self) -> None:
        if self._tested_fingerprint is not None:
            self._tested_fingerprint = None
            self._set_status(
                "Configuration changed. Send another test before saving.",
                COLORS["warning"],
            )

    def _fingerprint(self) -> tuple[str, str]:
        return (
            self._bot_token_edit.text().strip(),
            self._chat_id_edit.text().strip(),
        )

    def _configuration(self) -> TelegramConfiguration:
        bot_token, chat_id = self._fingerprint()
        return TelegramConfiguration(TelegramCredentials(bot_token), chat_id)

    def _test(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        try:
            configuration = self._configuration()
        except TelegramError as exc:
            self._set_status(str(exc), COLORS["error"])
            return

        self._set_busy(True)
        self._set_status("Sending a test message…", COLORS["warning"])
        self._active_fingerprint = self._fingerprint()
        self._startup_uncertain = False
        thread: _TelegramTestThread | None = None
        try:
            thread = _TelegramTestThread(configuration, self)
            thread.succeeded.connect(self._test_succeeded)
            thread.failed.connect(self._test_failed)
            thread.finished.connect(
                lambda current=thread: self._thread_finished(current)
            )
            self._thread = thread
            thread.start()
        except BaseException:
            logger.exception("Telegram test worker could not be started")
            if qthread_start_may_have_succeeded(thread):
                self._startup_uncertain = True
                self._set_status(
                    "The Telegram test reported a startup problem. Wait for the "
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
                "Test sent successfully. Confirm it arrived in Telegram, then save.",
                COLORS["success"],
            )

    def _test_failed(self, message: str) -> None:
        self._tested_fingerprint = None
        self._set_status(message or _SAFE_TEST_FAILURE, COLORS["error"])

    def _thread_finished(self, thread: _TelegramTestThread) -> None:
        if self._thread is thread:
            self._thread = None
            self._active_fingerprint = None
            self._startup_uncertain = False
            self._set_busy(False)
        thread.deleteLater()

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self._bot_token_edit,
            self._chat_id_edit,
            self._show_token,
            self._test_button,
            self._remove_button,
            self._cancel_button,
            self._save_button,
        ):
            widget.setEnabled(not busy)
        self._test_button.setText("Sending…" if busy else "Send Test")
        if not busy:
            self._remove_button.setEnabled(bool(self._bot_token_edit.text().strip()))

    def _save(self) -> None:
        try:
            configuration = self._configuration()
        except TelegramError as exc:
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
            save_telegram_configuration(self._db, configuration)
        except TelegramError as exc:
            self._set_status(str(exc), COLORS["error"])
            return
        self._saved = True
        self.accept()

    def _remove(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Remove Telegram Configuration",
                "Remove Automadin's saved Telegram bot token and chat ID? Telegram "
                "notification commands will fail until it is configured again.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            delete_telegram_configuration(self._db)
        except TelegramError as exc:
            self._set_status(str(exc), COLORS["error"])
            return
        self._bot_token_edit.clear()
        self._chat_id_edit.clear()
        self._tested_fingerprint = None
        self._saved = True
        self._remove_button.setEnabled(False)
        self._set_status("Telegram configuration removed.", COLORS["success"])

    def _set_status(self, text: str, color: str) -> None:
        self._status_label.setStyleSheet(f"color: {color};")
        self._status_label.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None and self._thread.isRunning():
            event.ignore()
            return
        super().closeEvent(event)
