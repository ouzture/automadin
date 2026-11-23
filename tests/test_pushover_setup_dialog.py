from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from automadin.services.pushover import PushoverConfiguration, PushoverCredentials
from automadin.ui import pushover_setup_dialog as setup_dialog


APP_TOKEN = "A" * 30
USER_KEY = "u" * 30


class _SettingsDb:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def get_app_setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_dialog_masks_loaded_credentials(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(
        setup_dialog,
        "load_pushover_credentials",
        lambda: PushoverCredentials(APP_TOKEN, USER_KEY),
    )

    dialog = setup_dialog.PushoverSetupDialog(_SettingsDb())

    assert dialog._app_token_edit.text() == APP_TOKEN
    assert dialog._user_key_edit.text() == USER_KEY
    assert dialog._app_token_edit.echoMode() is QLineEdit.EchoMode.Password
    assert dialog._user_key_edit.echoMode() is QLineEdit.EchoMode.Password
    assert dialog._status_label.text().startswith("Saved credentials loaded")
    dialog.deleteLater()


def test_dialog_saves_only_the_successfully_tested_values(monkeypatch) -> None:
    _app()
    saved: list[PushoverConfiguration] = []
    monkeypatch.setattr(setup_dialog, "load_pushover_credentials", lambda: None)
    monkeypatch.setattr(
        setup_dialog,
        "save_pushover_configuration",
        lambda _db, configuration: saved.append(configuration),
    )
    dialog = setup_dialog.PushoverSetupDialog(_SettingsDb())
    dialog._app_token_edit.setText(APP_TOKEN)
    dialog._user_key_edit.setText(USER_KEY)
    dialog._device_edit.setText("iphone")
    dialog._tested_fingerprint = dialog._fingerprint()

    dialog._save()

    assert dialog.saved is True
    assert saved == [
        PushoverConfiguration(PushoverCredentials(APP_TOKEN, USER_KEY), "iphone")
    ]
    dialog.deleteLater()
