"""Folder + name editor for slash-qualified Automadin resources."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from automadin.models.resource_path import (
    WorkspaceFolderIndex,
    find_resource_collision,
    join_resource_path,
    normalize_resource_path,
    resource_group_path,
    resource_leaf_name,
)
from automadin.ui.theme import COLORS
from automadin.ui.editable_combo import set_editable_combo_text


class ResourcePathDialog(QDialog):
    """Edit a virtual folder and leaf name without making users compose raw paths."""

    def __init__(
        self,
        *,
        title: str,
        item_label: str,
        current_path: str = "",
        ignore_path: str | None = None,
        allow_existing: bool = False,
        known_paths: Iterable[str] = (),
        known_folders: Iterable[str] = (),
        existing_paths: Iterable[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._current_path = normalize_resource_path(current_path)
        self._ignore_path = (
            self._current_path
            if ignore_path is None
            else normalize_resource_path(ignore_path)
        )
        self._allow_existing = allow_existing
        self._known_paths = list(known_paths)
        self._known_folders = list(known_folders)
        self._existing_paths = (
            list(existing_paths) if existing_paths is not None else list(self._known_paths)
        )
        self._folder_index = WorkspaceFolderIndex(
            explicit_groups=self._known_folders,
            resource_paths=self._known_paths,
        )
        self._result_path = ""

        self.setWindowTitle(title)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        intro = QLabel(
            "Choose an existing folder or type a new one; it is created when "
            "this item is saved. Use / for subfolders, for example "
            "Mobile/Daily tasks."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10pt;")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._folder_combo = QComboBox()
        self._folder_combo.setEditable(True)
        self._folder_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._folder_combo.addItem("")
        self._folder_combo.addItems(self._folder_index.folders)
        set_editable_combo_text(
            self._folder_combo,
            self._folder_index.canonicalize_existing(
                resource_group_path(self._current_path)
            ),
        )
        self._folder_combo.setToolTip(
            "Leave blank for No folder. Use / to create nested folders."
        )
        form.addRow("Folder", self._folder_combo)

        self._name_edit = QLineEdit(resource_leaf_name(self._current_path))
        self._name_edit.setPlaceholderText(f"{item_label} name")
        self._name_edit.setClearButtonEnabled(True)
        form.addRow("Name", self._name_edit)
        layout.addLayout(form)

        self._preview_label = QLabel()
        self._preview_label.setStyleSheet(
            f"color: {COLORS['text_primary']}; background-color: {COLORS['input_bg']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 5px; "
            "padding: 8px 10px; font-family: 'Cascadia Code', 'Consolas', monospace; "
            "font-size: 10pt;"
        )
        layout.addWidget(self._preview_label)

        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {COLORS['red']}; font-size: 10pt;")
        layout.addWidget(self._error_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_button = self._buttons.button(QDialogButtonBox.StandardButton.Save)
        if self._save_button is not None:
            self._save_button.setText("Apply")
            self._save_button.setProperty("cssClass", "primary")
        self._buttons.accepted.connect(self._accept_if_valid)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._folder_combo.currentTextChanged.connect(self._refresh_validation)
        self._name_edit.textChanged.connect(self._refresh_validation)
        self._refresh_validation()
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _candidate_path(self) -> tuple[str, str | None]:
        name = self._name_edit.text().strip()
        if not name:
            return "", "Enter a name."
        if "/" in name or "\\" in name:
            return "", "Name cannot contain '/'. Choose its folder separately."
        try:
            folder = self._folder_index.canonicalize(
                self._folder_combo.currentText()
            )
            candidate = join_resource_path(folder, name)
        except ValueError as exc:
            return "", str(exc)
        collision = find_resource_collision(
            candidate,
            self._existing_paths,
            ignore_path=self._ignore_path,
        )
        if collision is not None and not self._allow_existing:
            return candidate, f"'{collision}' already exists."
        return candidate, None

    def _refresh_validation(self) -> None:
        candidate, error = self._candidate_path()
        self._preview_label.setText(
            f"Location: {candidate or 'No folder / …'}"
        )
        self._error_label.setText(error or "")
        if self._save_button is not None:
            self._save_button.setEnabled(error is None)

    def _accept_if_valid(self) -> None:
        candidate, error = self._candidate_path()
        if error is not None:
            self._refresh_validation()
            return
        self._result_path = candidate
        self.accept()

    def selected_path(self) -> str:
        return self._result_path

    @classmethod
    def get_path(
        cls,
        *,
        title: str,
        item_label: str,
        current_path: str = "",
        ignore_path: str | None = None,
        allow_existing: bool = False,
        known_paths: Iterable[str] = (),
        known_folders: Iterable[str] = (),
        existing_paths: Iterable[str] | None = None,
        parent: QWidget | None = None,
    ) -> str | None:
        dialog = cls(
            title=title,
            item_label=item_label,
            current_path=current_path,
            ignore_path=ignore_path,
            allow_existing=allow_existing,
            known_paths=known_paths,
            known_folders=known_folders,
            existing_paths=existing_paths,
            parent=parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_path()


class ResourceFolderDialog(QDialog):
    """Choose one existing or new virtual folder for a batch operation."""

    def __init__(
        self,
        *,
        title: str,
        known_paths: Iterable[str] = (),
        known_folders: Iterable[str] = (),
        current_folder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._known_paths = list(known_paths)
        self._known_folders = list(known_folders)
        self._folder_index = WorkspaceFolderIndex(
            explicit_groups=self._known_folders,
            resource_paths=self._known_paths,
        )
        self._selected_folder = ""
        self.setWindowTitle(title)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        intro = QLabel(
            "Choose an existing folder or type a new one; it is created when "
            "the items are saved. Use / for subfolders; leave blank for No folder."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10pt;")
        layout.addWidget(intro)

        self._folder_combo = QComboBox()
        self._folder_combo.setEditable(True)
        self._folder_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._folder_combo.addItem("")
        self._folder_combo.addItems(self._folder_index.folders)
        set_editable_combo_text(
            self._folder_combo,
            self._folder_index.canonicalize_existing(current_folder),
        )
        form = QFormLayout()
        form.addRow("Folder", self._folder_combo)
        layout.addLayout(form)

        self._preview = QLabel()
        self._preview.setStyleSheet(
            f"color: {COLORS['text_primary']}; background-color: {COLORS['input_bg']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 5px; "
            "padding: 8px 10px; font-size: 10pt;"
        )
        layout.addWidget(self._preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setText("Continue")
            ok_button.setProperty("cssClass", "primary")
        buttons.accepted.connect(self._accept_folder)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._folder_combo.currentTextChanged.connect(self._refresh_preview)
        self._refresh_preview()

    def _normalized_folder(self) -> str:
        return self._folder_index.canonicalize(
            self._folder_combo.currentText()
        )

    def _refresh_preview(self) -> None:
        try:
            folder = self._normalized_folder()
            self._preview.setText(f"Destination: {folder or 'No folder'}")
        except ValueError as exc:
            self._preview.setText(str(exc))

    def _accept_folder(self) -> None:
        try:
            self._selected_folder = self._normalized_folder()
        except ValueError:
            self._refresh_preview()
            return
        self.accept()

    @classmethod
    def get_folder(
        cls,
        *,
        title: str,
        known_paths: Iterable[str] = (),
        known_folders: Iterable[str] = (),
        current_folder: str = "",
        parent: QWidget | None = None,
    ) -> str | None:
        dialog = cls(
            title=title,
            known_paths=known_paths,
            known_folders=known_folders,
            current_folder=current_folder,
            parent=parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog._selected_folder
