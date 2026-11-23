"""Package picker for the connected Android device."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


class _PackageLoadWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        loader: Callable[[], list[str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._loader = loader

    def run(self) -> None:
        try:
            self.loaded.emit(self._loader())
        except Exception as exc:
            self.failed.emit(str(exc) or "Could not list Android packages")


class AndroidPackagePickerDialog(QDialog):
    """Load and choose one user-installed Android package."""

    def __init__(
        self,
        loader: Callable[[], list[str]],
        current_value: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose Android Package")
        self.resize(540, 480)
        self._loading = True
        self._selected_package = ""

        layout = QVBoxLayout(self)
        self._status = QLabel("Loading user-installed packages…")
        layout.addWidget(self._status)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter packages…")
        self._filter.setEnabled(False)
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._packages = QListWidget()
        self._packages.setObjectName("androidPackageList")
        self._packages.setEnabled(False)
        self._packages.itemDoubleClicked.connect(lambda _item: self.accept())
        self._packages.currentItemChanged.connect(
            lambda _current, _previous: self._refresh_accept_enabled()
        )
        layout.addWidget(self._packages, 1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setEnabled(False)
        layout.addWidget(self._buttons)

        self._current_value = current_value
        self._worker = _PackageLoadWorker(loader, self)
        self._worker.loaded.connect(self._on_loaded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    @property
    def selected_package(self) -> str:
        return self._selected_package

    def _on_loaded(self, packages: object) -> None:
        values = packages if isinstance(packages, list) else []
        self._packages.clear()
        for package in values:
            if isinstance(package, str):
                self._packages.addItem(QListWidgetItem(package))
        self._filter.setEnabled(True)
        self._packages.setEnabled(True)
        self._status.setText(f"{self._packages.count()} user-installed packages")
        matches = self._packages.findItems(
            self._current_value,
            Qt.MatchFlag.MatchExactly,
        )
        if matches:
            self._packages.setCurrentItem(matches[0])
        elif self._packages.count():
            self._packages.setCurrentRow(0)

    def _on_failed(self, message: str) -> None:
        self._status.setText(message)

    def _on_finished(self) -> None:
        self._loading = False
        self._refresh_accept_enabled()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for row in range(self._packages.count()):
            item = self._packages.item(row)
            item.setHidden(bool(needle and needle not in item.text().casefold()))
        current = self._packages.currentItem()
        if current is not None and current.isHidden():
            self._packages.setCurrentItem(None)
        self._refresh_accept_enabled()

    def _refresh_accept_enabled(self) -> None:
        item = self._packages.currentItem()
        self._ok_button.setEnabled(
            not self._loading and item is not None and not item.isHidden()
        )

    def accept(self) -> None:
        item = self._packages.currentItem()
        if self._loading or item is None or item.isHidden():
            return
        self._selected_package = item.text()
        super().accept()

    def reject(self) -> None:
        if self._loading:
            self._status.setText("Finishing package discovery before closing…")
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._loading:
            self._status.setText("Finishing package discovery before closing…")
            event.ignore()
            return
        super().closeEvent(event)
