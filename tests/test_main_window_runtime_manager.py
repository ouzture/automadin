from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from automadin.engine.adb_engine import ADB_PATH_SETTING_KEY
from automadin.ui import main_window


class _FakeDatabase:
    def __init__(self, db_path: Path, adb_path: Path) -> None:
        self.db_path = str(db_path)
        self._adb_path = adb_path

    def get_app_setting(self, key: str, default=None):
        assert key == ADB_PATH_SETTING_KEY
        return str(self._adb_path)


class _FakeStatusBar:
    def __init__(self) -> None:
        self.messages: list[tuple[str, int]] = []

    def showMessage(self, message: str, timeout: int) -> None:
        self.messages.append((message, timeout))


def test_show_runtime_manager_passes_database_configured_adb(
    monkeypatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    database = _FakeDatabase(data_dir / "automadin.db", tmp_path / "tools" / "adb.exe")
    status_bar = _FakeStatusBar()
    window = SimpleNamespace(_db=database, statusBar=lambda: status_bar)
    launched: list[tuple[str, str | None]] = []
    monkeypatch.delenv("AUTOMADIN_DATA_DIR", raising=False)
    monkeypatch.setattr(
        main_window,
        "launch_runtime_manager",
        lambda launched_data_dir, adb_path=None: launched.append(
            (launched_data_dir, adb_path)
        ),
    )

    main_window.MainWindow._show_runtime_manager(window)

    assert launched == [(str(data_dir), str(database._adb_path))]
    assert status_bar.messages == [
        ("Runtime Manager opened in a separate process.", 5000)
    ]
