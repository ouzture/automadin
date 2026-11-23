from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from automadin.engine.adb_engine import ADB_PATH_SETTING_KEY, find_adb
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS
from automadin.ui.adb_path_dialog import AdbPathDialog


class _SettingsDb:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def get_app_setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_app_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def delete_app_setting(self, key: str) -> None:
        self.settings.pop(key, None)


@pytest.fixture(scope="module", autouse=True)
def _application():
    return QApplication.instance() or QApplication([])


def _dialog_for(path: Path) -> tuple[AdbPathDialog, _SettingsDb]:
    db = _SettingsDb()
    dialog = AdbPathDialog(db)  # type: ignore[arg-type]
    dialog._path_edit.setText(str(path))
    return dialog, db


@pytest.mark.parametrize("variable", ["ANDROID_HOME", "ANDROID_SDK_ROOT"])
def test_find_adb_uses_standard_android_sdk_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    sdk_root = tmp_path / "android-sdk"
    adb = sdk_root / "platform-tools" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    monkeypatch.delenv("ADB_PATH", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setenv(variable, str(sdk_root))

    with patch("automadin.engine.adb_engine._bundled_adb_candidates", return_value=[]):
        assert find_adb() == str(adb)


def test_custom_adb_path_is_saved_only_after_version_probe(tmp_path: Path) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    dialog, db = _dialog_for(adb)
    completed = subprocess.CompletedProcess(
        [str(adb), "version"],
        0,
        "Android Debug Bridge version 1.0.41\nVersion 37.0.0",
        "",
    )

    with patch(
        "automadin.ui.adb_path_dialog.subprocess.run", return_value=completed
    ) as run:
        dialog._save()

    assert db.settings[ADB_PATH_SETTING_KEY] == str(adb)
    assert dialog.saved
    assert run.call_args.args[0] == [str(adb), "version"]
    assert run.call_args.kwargs["creationflags"] == HIDDEN_PROCESS_CREATION_FLAGS
    dialog.deleteLater()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileNotFoundError(2, "missing"), "not found"),
        (PermissionError(13, "denied"), "denied permission"),
        (
            subprocess.TimeoutExpired(["adb.exe", "version"], 5),
            "timed out",
        ),
        (OSError(193, "not executable"), "could not start"),
    ],
)
def test_adb_version_probe_handles_process_errors(
    tmp_path: Path, error: OSError, expected: str
) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    dialog, _db = _dialog_for(adb)

    with patch("automadin.ui.adb_path_dialog.subprocess.run", side_effect=error):
        assert not dialog._test()

    assert expected.casefold() in dialog._status_label.text().casefold()
    dialog.deleteLater()


def test_custom_path_is_not_saved_when_command_fails_or_is_not_adb(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "not-adb.exe"
    adb.touch()
    outcomes = (
        subprocess.CompletedProcess([str(adb), "version"], 1, "", "failed"),
        subprocess.CompletedProcess([str(adb), "version"], 0, "other tool", ""),
    )

    for outcome in outcomes:
        dialog, db = _dialog_for(adb)
        with (
            patch(
                "automadin.ui.adb_path_dialog.subprocess.run",
                return_value=outcome,
            ),
            patch.object(QMessageBox, "warning") as warning,
        ):
            dialog._save()

        assert ADB_PATH_SETTING_KEY not in db.settings
        assert not dialog.saved
        warning.assert_called_once()
        assert "did not succeed" in str(warning.call_args.args[2])
        dialog.deleteLater()
