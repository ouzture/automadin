"""Configure the separately installed Tesseract OCR engine."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
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

from automadin.services.database import DatabaseService
from automadin.services.ocr import (
    TesseractConfiguration,
    inspect_tesseract,
    load_tesseract_configuration,
    normalise_tesseract_languages,
    save_tesseract_configuration,
)
from automadin.ui.theme import COLORS


_TESSERACT_INSTALL_GUIDE = "https://tesseract-ocr.github.io/tessdoc/Installation.html"


class OcrSetupDialog(QDialog):
    """Select and validate an externally installed Tesseract executable."""

    def __init__(self, db: DatabaseService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._saved = False
        self.setWindowTitle("Screen text recognition")
        self.resize(680, 300)
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
            "Automadin uses a separately installed Tesseract OCR engine. "
            "English is always enabled; add installed Tesseract language codes "
            "such as deu, fra, or spa when needed."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(note)

        guide = QPushButton("Open Tesseract installation guide")
        guide.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_TESSERACT_INSTALL_GUIDE))
        )
        layout.addWidget(guide)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(
            r"Leave empty to use tesseract.exe found on PATH"
        )
        path_row.addWidget(self._path_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        layout.addWidget(QLabel("Tesseract executable (optional)"))
        layout.addLayout(path_row)

        layout.addWidget(QLabel("Additional language codes (comma-separated)"))
        self._languages_edit = QLineEdit()
        self._languages_edit.setPlaceholderText("deu, fra, spa")
        layout.addWidget(self._languages_edit)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(
            self._status.textInteractionFlags()
        )
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        test = QPushButton("Test")
        test.clicked.connect(self._test)
        buttons.addWidget(test)
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save")
        save.setProperty("cssClass", "primary")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _load_current_setting(self) -> None:
        configuration = load_tesseract_configuration(self._db)
        self._path_edit.setText(configuration.executable or "")
        self._languages_edit.setText(
            ", ".join(language for language in configuration.languages if language != "eng")
        )
        self._set_status(
            "Choose tesseract.exe after installing Tesseract, then test the "
            "language data you want Automadin to use.",
            COLORS["text_tertiary"],
        )

    def _browse(self) -> None:
        current = self._path_edit.text().strip()
        start_dir = str(Path(current).parent) if current else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select tesseract.exe",
            start_dir,
            "Tesseract executable (tesseract.exe);;All Files (*)",
        )
        if path:
            self._path_edit.setText(path)

    def _configuration(self) -> TesseractConfiguration:
        languages = ["eng"]
        languages.extend(
            part.strip() for part in self._languages_edit.text().split(",") if part.strip()
        )
        return TesseractConfiguration(
            self._path_edit.text().strip() or None,
            normalise_tesseract_languages(languages),
        )

    def _test(self) -> bool:
        try:
            status = inspect_tesseract(self._configuration())
        except ValueError as exc:
            self._set_status(str(exc), COLORS["error"])
            return False
        if not status.usable:
            self._set_status(status.error or "Tesseract is not ready.", COLORS["error"])
            return False
        self._set_status(
            f"Ready: {status.version} ({status.source})\n"
            f"Installed language data: {', '.join(status.languages)}",
            COLORS["success"],
        )
        return True

    def _save(self) -> None:
        if not self._test():
            QMessageBox.warning(
                self,
                "Tesseract Test Failed",
                "The OCR configuration was not saved because it could not be validated.\n\n"
                + self._status.text(),
            )
            return
        try:
            configuration = self._configuration()
            save_tesseract_configuration(
                self._db,
                configuration.executable,
                configuration.languages,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid OCR Languages", str(exc))
            return
        self._saved = True
        self.accept()

    def _set_status(self, text: str, color: str) -> None:
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(text)
