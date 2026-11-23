"""Workspace-wide management for virtual resource folders."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.models.resource_path import split_resource_path
from automadin.services.database import DatabaseService
from automadin.ui.theme import COLORS


class FolderManagementDialog(QDialog):
    """Move, rename, or remove inferred folders without deleting resources."""

    def __init__(
        self,
        database: DatabaseService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self.changes: list[dict[str, list[tuple[str, str]]]] = []
        self.setWindowTitle("Manage Folders")
        self.setMinimumSize(520, 440)

        layout = QVBoxLayout(self)
        title = QLabel("Workspace folders")
        title.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 14pt; font-weight: 700;"
        )
        layout.addWidget(title)

        explanation = QLabel(
            "Folders organize flows, run setups, visual rules, and images together. "
            "They are created by their contents, so removing a folder safely moves "
            "its contents to the parent instead of deleting them."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(explanation)

        self._folders = QListWidget()
        self._folders.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self._folders, 1)

        actions = QHBoxLayout()
        self._move_button = QPushButton("Move or rename…")
        self._move_button.clicked.connect(self._move_folder)
        actions.addWidget(self._move_button)

        self._delete_button = QPushButton("Remove folder")
        self._delete_button.setProperty("cssClass", "danger")
        self._delete_button.clicked.connect(self._remove_folder)
        actions.addWidget(self._delete_button)
        actions.addStretch()
        layout.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _selected_folder(self) -> str | None:
        item = self._folders.currentItem()
        return item.text() if item is not None else None

    def _update_buttons(self) -> None:
        enabled = self._selected_folder() is not None
        self._move_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)

    def _refresh(self, preferred: str = "") -> None:
        self._folders.clear()
        folders = self._database.list_resource_folders()
        self._folders.addItems(folders)
        selected = None
        for index in range(self._folders.count()):
            item = self._folders.item(index)
            if item.text().casefold() == preferred.casefold():
                selected = item
                break
        if selected is not None:
            self._folders.setCurrentItem(selected)
        elif self._folders.count():
            self._folders.setCurrentRow(0)
        self._update_buttons()

    def _apply_move(self, source: str, destination: str) -> None:
        try:
            changes = self._database.move_resource_folder(source, destination)
        except ValueError as exc:
            QMessageBox.warning(self, "Folder Move Failed", str(exc))
            return
        self.changes.append(changes)
        self._refresh(destination)

    def _move_folder(self) -> None:
        source = self._selected_folder()
        if source is None:
            return
        destination, accepted = QInputDialog.getText(
            self,
            "Move or Rename Folder",
            "New full folder path:",
            text=source,
        )
        if not accepted:
            return
        if not destination.strip():
            QMessageBox.information(
                self,
                "Use Remove Folder",
                "Use Remove folder to move these resources to the parent folder.",
            )
            return
        self._apply_move(source, destination)

    def _remove_folder(self) -> None:
        source = self._selected_folder()
        if source is None:
            return
        parts = split_resource_path(source)
        parent = "/".join(parts[:-1])
        destination_label = f"'{parent}'" if parent else "No folder"
        reply = QMessageBox.question(
            self,
            "Remove Folder",
            f"Remove folder '{source}'?\n\n"
            f"All resources and subfolders inside it will move to {destination_label}. "
            "No flows, images, visual rules, or run setups will be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._apply_move(source, parent)


__all__ = ["FolderManagementDialog"]
