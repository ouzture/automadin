from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from automadin.services.telegram import TelegramConfiguration, TelegramCredentials
from automadin.ui import telegram_setup_dialog as setup_dialog


BOT_TOKEN = "123456789:" + "A" * 35
CHAT_ID = "123456789"


class _SettingsDb:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {
            "telegram_default_chat_id": CHAT_ID,
        }

    def get_app_setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_dialog_masks_loaded_token(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(
        setup_dialog,
        "load_telegram_credentials",
        lambda: TelegramCredentials(BOT_TOKEN),
    )

    dialog = setup_dialog.TelegramSetupDialog(_SettingsDb())

    assert dialog._bot_token_edit.text() == BOT_TOKEN
    assert dialog._chat_id_edit.text() == CHAT_ID
    assert dialog._bot_token_edit.echoMode() is QLineEdit.EchoMode.Password
    assert dialog._status_label.text().startswith("Saved configuration loaded")
    dialog.deleteLater()


def test_dialog_saves_only_successfully_tested_values(monkeypatch) -> None:
    _app()
    saved: list[TelegramConfiguration] = []
    monkeypatch.setattr(setup_dialog, "load_telegram_credentials", lambda: None)
    monkeypatch.setattr(
        setup_dialog,
        "save_telegram_configuration",
        lambda _db, configuration: saved.append(configuration),
    )
    dialog = setup_dialog.TelegramSetupDialog(_SettingsDb())
    dialog._bot_token_edit.setText(BOT_TOKEN)
    dialog._chat_id_edit.setText(CHAT_ID)
    dialog._tested_fingerprint = dialog._fingerprint()

    dialog._save()

    assert dialog.saved is True
    assert saved == [TelegramConfiguration(TelegramCredentials(BOT_TOKEN), CHAT_ID)]
    dialog.deleteLater()
