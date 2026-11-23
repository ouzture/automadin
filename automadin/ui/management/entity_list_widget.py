"""Generic folder tree for managing named Automadin resources."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.models.resource_path import normalize_resource_path
from automadin.ui.resource_path_dialog import ResourcePathDialog
from automadin.ui.resource_tree import ResourceTreeWidget
from automadin.ui.theme import COLORS


def _make_btn(label: str, css_class: str | None = None) -> QPushButton:
    btn = QPushButton(label)
    btn.setMinimumHeight(30)
    if css_class:
        btn.setProperty("cssClass", css_class)
    return btn


class EntityListWidget(QWidget):
    """Reusable folder tree + organize/delete/import/export resource panel."""

    def __init__(
        self,
        title: str,
        list_fn: Callable[[], list[str]],
        delete_fn: Callable[[str], bool],
        rename_fn: Callable[[str, str], None],
        edit_fn: Callable[[str], None] | None = None,
        import_fn: Callable[[], None] | None = None,
        import_label: str = "⬇ Import",
        export_fn: Callable[[str], None] | None = None,
        path_fn: Callable[[str], str] | None = None,
        known_paths_fn: Callable[[], list[str]] | None = None,
        known_folders_fn: Callable[[], list[str]] | None = None,
        detail_fn: Callable[[str], str] | None = None,
        item_label: str | None = None,
        confirm_delete: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._list_fn = list_fn
        self._delete_fn = delete_fn
        self._rename_fn = rename_fn
        self._edit_fn = edit_fn
        self._import_fn = import_fn
        self._import_label = import_label
        self._export_fn = export_fn
        self._path_fn = path_fn or (lambda name: name)
        self._known_paths_fn = known_paths_fn
        self._known_folders_fn = known_folders_fn
        self._detail_fn = detail_fn
        self._item_label = item_label or title.rstrip("s")
        self._confirm_delete = confirm_delete
        self._build_ui(title)
        self.refresh()

    # ── UI construction ───────────────────────────────────────────

    def _build_ui(self, title: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 8)
        layout.setSpacing(6)

        # Header row
        header = QHBoxLayout()
        header.setSpacing(4)
        lbl = QLabel(title)
        lbl.setStyleSheet(f"font-weight: 600; font-size: 11.25pt; color: {COLORS['text_primary']};")
        header.addWidget(lbl)
        header.addStretch()

        self._refresh_btn = _make_btn("Refresh")
        self._refresh_btn.setToolTip("Refresh list")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)

        if self._export_fn:
            self._export_btn = _make_btn("⬆ Export")
            self._export_btn.setEnabled(False)
            self._export_btn.clicked.connect(self._on_export)
            header.addWidget(self._export_btn)

        if self._import_fn:
            self._import_btn = _make_btn(self._import_label)
            self._import_btn.clicked.connect(self._on_import)
            header.addWidget(self._import_btn)

        layout.addLayout(header)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(f"Search {title.lower()}…")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(180)
        self._filter_timer.timeout.connect(self.refresh)
        self._filter_edit.textChanged.connect(
            lambda _text: self._filter_timer.start()
        )
        layout.addWidget(self._filter_edit)

        # Folder tree
        self._list_widget = ResourceTreeWidget()
        self._list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self._list_widget.resource_activated.connect(self._on_resource_activated)
        layout.addWidget(self._list_widget, 1)

        # Action row
        action_row = QHBoxLayout()
        action_row.setSpacing(4)

        self._organize_btn = _make_btn("Move or rename…")
        self._organize_btn.setToolTip("Change folder or name")
        self._organize_btn.setEnabled(False)
        self._organize_btn.clicked.connect(self._on_organize)
        action_row.addWidget(self._organize_btn)

        self._delete_btn = _make_btn("🗑 Delete", "danger")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._on_delete)
        action_row.addWidget(self._delete_btn)

        if self._edit_fn:
            self._edit_btn = _make_btn("⚙ Edit", "primary")
            self._edit_btn.setEnabled(False)
            self._edit_btn.clicked.connect(self._on_edit)
            action_row.addWidget(self._edit_btn)

        action_row.addStretch()
        layout.addLayout(action_row)

    # ── Data ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Reload items from the data source."""
        names = self._list_fn()
        resources = [(name, self._path_fn(name)) for name in names]
        details = (
            {name: self._detail_fn(name) for name in names}
            if self._detail_fn is not None
            else None
        )
        self._list_widget.set_resources(
            resources,
            query=self._filter_edit.text(),
            canonical_folders=(
                self._known_folders_fn()
                if self._known_folders_fn is not None
                else ()
            ),
            details=details,
        )
        self._on_selection_changed()

    def selected_name(self) -> str | None:
        return self._list_widget.selected_resource_id()

    # ── Selection ─────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        has = self.selected_name() is not None
        self._organize_btn.setEnabled(has)
        self._delete_btn.setEnabled(has)
        if self._edit_fn and hasattr(self, "_edit_btn"):
            self._edit_btn.setEnabled(has)
        if self._export_fn and hasattr(self, "_export_btn"):
            self._export_btn.setEnabled(has)

    # ── Actions ───────────────────────────────────────────────────

    def _on_organize(self) -> None:
        old = self.selected_name()
        if old is None:
            return
        current_path = normalize_resource_path(self._path_fn(old))
        known_paths = [self._path_fn(name) for name in self._list_fn()]
        if self._known_paths_fn is not None:
            known_paths.extend(self._known_paths_fn())
        new_path = ResourcePathDialog.get_path(
            title=f"Move or Rename {self._item_label}",
            item_label=self._item_label,
            current_path=current_path,
            known_paths=known_paths,
            known_folders=(
                self._known_folders_fn()
                if self._known_folders_fn is not None
                else ()
            ),
            existing_paths=[self._path_fn(name) for name in self._list_fn()],
            parent=self,
        )
        if new_path is None or new_path == current_path:
            return
        try:
            self._rename_fn(old, new_path)
        except Exception as exc:
            QMessageBox.warning(self, "Move or Rename Failed", str(exc))
            return
        self.refresh()

    def _on_resource_activated(self, _name: str) -> None:
        if self._edit_fn is not None:
            self._on_edit()

    def _on_delete(self) -> None:
        name = self.selected_name()
        if name is None:
            return
        if self._confirm_delete:
            reply = QMessageBox.question(
                self,
                "Confirm Delete",
                f"Delete '{name}'? This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._delete_fn(name)
        self.refresh()

    def _on_edit(self) -> None:
        name = self.selected_name()
        if name is None or self._edit_fn is None:
            return
        self._edit_fn(name)
        self.refresh()

    def _on_export(self) -> None:
        name = self.selected_name()
        if name is None or self._export_fn is None:
            return
        self._export_fn(name)

    def _on_import(self) -> None:
        if self._import_fn is None:
            return
        self._import_fn()
        self.refresh()
