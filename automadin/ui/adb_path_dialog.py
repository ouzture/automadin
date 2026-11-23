"""Dialog for selecting the ADB executable used by Automadin."""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.adb_engine import ADB_PATH_SETTING_KEY, find_adb
from automadin.services.database import DatabaseService
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS
from automadin.ui.theme import COLORS


class AdbPathDialog(QDialog):
    """Configure and test the persisted ADB executable path."""

    def __init__(self, db: DatabaseService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._saved = False

        self.setWindowTitle("ADB Location")
        self.resize(620, 230)
        self._build_ui()
        self._load_current_setting()

    @property
    def saved(self) -> bool:
        return self._saved

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        note = QLabel(
            "Choose adb.exe if Automadin cannot find Android devices with the "
            "automatic search. The path is saved in the current database."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(note)

        current_auto = find_adb()
        self._auto_label = QLabel(f"Auto-detected: {current_auto}")
        self._auto_label.setWordWrap(True)
        self._auto_label.setStyleSheet(f"color: {COLORS['text_tertiary']};")
        layout.addWidget(self._auto_label)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(r"C:\Android\platform-tools\adb.exe")
        path_row.addWidget(self._path_edit, 1)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(f"color: {COLORS['text_tertiary']};")
        layout.addWidget(self._status_label)

        button_row = QHBoxLayout()
        test_btn = QPushButton("Test")
        test_btn.clicked.connect(self._test)
        button_row.addWidget(test_btn)

        reset_btn = QPushButton("Reset Auto")
        reset_btn.clicked.connect(self._reset_auto)
        button_row.addWidget(reset_btn)

        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setProperty("cssClass", "primary")
        save_btn.clicked.connect(self._save)
        button_row.addWidget(save_btn)
        layout.addLayout(button_row)

    def _load_current_setting(self) -> None:
        value = self._db.get_app_setting(ADB_PATH_SETTING_KEY, "") or ""
        self._path_edit.setText(value)
        if value:
            self._status_label.setText("Using saved ADB path.")
        else:
            self._status_label.setText("Using automatic ADB discovery.")

    def _browse(self) -> None:
        start_dir = str(Path(self._path_edit.text()).parent)
        if not self._path_edit.text().strip() or not Path(start_dir).exists():
            start_dir = str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select adb.exe",
            start_dir,
            "ADB Executable (adb.exe);;All Files (*)",
        )
        if path:
            self._path_edit.setText(path)

    def _candidate_path(self) -> str:
        configured = self._path_edit.text().strip()
        return configured or find_adb()

    def _test(self) -> bool:
        adb_path = self._candidate_path()
        try:
            result = subprocess.run(
                [adb_path, "version"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
            )
        except FileNotFoundError:
            self._status_label.setStyleSheet(f"color: {COLORS['error']};")
            self._status_label.setText(f"ADB not found at: {adb_path}")
            return False
        except PermissionError as exc:
            self._status_label.setStyleSheet(f"color: {COLORS['error']};")
            self._status_label.setText(
                f"Windows denied permission to run ADB at: {adb_path}\n{exc}"
            )
            return False
        except subprocess.TimeoutExpired:
            self._status_label.setStyleSheet(f"color: {COLORS['error']};")
            self._status_label.setText(
                "ADB version command timed out. Check the selected executable or "
                "repair ADB from Tools > Runtime Components."
            )
            return False
        except OSError as exc:
            self._status_label.setStyleSheet(f"color: {COLORS['error']};")
            self._status_label.setText(
                f"Windows could not start ADB at: {adb_path}\n{exc}"
            )
            return False

        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        ).strip()
        if result.returncode != 0:
            self._status_label.setStyleSheet(f"color: {COLORS['error']};")
            self._status_label.setText(
                output or f"ADB version failed with exit code {result.returncode}."
            )
            return False
        if "android debug bridge" not in output.casefold():
            self._status_label.setStyleSheet(f"color: {COLORS['error']};")
            self._status_label.setText(
                "The selected file ran but did not identify itself as Android Debug "
                "Bridge. Choose adb.exe from Android Platform Tools."
            )
            return False

        first_line = output.splitlines()[0] if output else "ADB test passed."
        self._status_label.setStyleSheet(f"color: {COLORS['success']};")
        self._status_label.setText(first_line)
        return True

    def _reset_auto(self) -> None:
        self._path_edit.clear()
        self._db.delete_app_setting(ADB_PATH_SETTING_KEY)
        self._saved = True
        self._status_label.setStyleSheet(f"color: {COLORS['success']};")
        self._status_label.setText("ADB path reset. Automatic discovery will be used.")

    def _save(self) -> None:
        path = self._path_edit.text().strip()
        if path:
            if not Path(path).is_file():
                QMessageBox.warning(
                    self,
                    "ADB Not Found",
                    f"No file exists at:\n{path}",
                )
                return
            if not self._test():
                QMessageBox.warning(
                    self,
                    "ADB Test Failed",
                    "This path was not saved because 'adb version' did not succeed.\n\n"
                    f"{self._status_label.text()}",
                )
                return
            self._db.set_app_setting(ADB_PATH_SETTING_KEY, path)
        else:
            self._db.delete_app_setting(ADB_PATH_SETTING_KEY)
        self._saved = True
        self.accept()
