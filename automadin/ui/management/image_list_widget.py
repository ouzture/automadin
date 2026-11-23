"""Image list panel with thumbnail previews, flow references, and a filter field."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from automadin.models.resource_path import resource_group_path, split_resource_path
from automadin.services.database import DatabaseService
from automadin.ui.image_group_filter import ImageGroupFilterBar
from automadin.ui.image_name_tree import (
    image_group_parts,
    image_leaf_name,
    image_matches_top_group,
    image_sort_key,
    top_level_image_groups,
)
from automadin.ui.management.entity_list_widget import EntityListWidget
from automadin.ui.resource_tree import (
    GROUP_PATH_ROLE,
    RESOURCE_ID_ROLE,
    RESOURCE_PATH_ROLE,
)
from automadin.ui.theme import COLORS


class ImageListWidget(EntityListWidget):
    """EntityListWidget variant that shows thumbnails, dimensions, and flow references."""

    image_renamed = Signal(str, str)
    image_deleted = Signal(str)

    def __init__(
        self,
        db: DatabaseService,
        import_fn: Callable[[], None] | None = None,
        export_fn: Callable[[str], None] | None = None,
        known_paths_fn: Callable[[], list[str]] | None = None,
        known_folders_fn: Callable[[], list[str]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        self._db = db
        self._image_meta: list[dict] = []
        self._flow_map: dict[str, list[str]] = {}
        self._display_paths: dict[str, str] = {}
        self._group_filter: ImageGroupFilterBar | None = None
        self._image_tree_initialized = False

        def list_fn() -> list[str]:
            self._image_meta = db.list_images()
            return [m["name"] for m in self._image_meta]

        super().__init__(
            title="Images",
            list_fn=list_fn,
            delete_fn=db.delete_image,
            rename_fn=self._rename_image,
            import_fn=import_fn,
            export_fn=export_fn,
            known_paths_fn=known_paths_fn,
            known_folders_fn=known_folders_fn,
            item_label="Image",
            parent=parent,
        )

        self._filter_edit.setPlaceholderText("Search images…")

        self._group_filter = ImageGroupFilterBar()
        self._group_filter.group_changed.connect(lambda _group: self.refresh())
        self.layout().insertWidget(2, self._group_filter)
        self.refresh()

    # ── Data ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Rebuild flow map, apply filter, then repopulate list with thumbnail rows."""
        # Rebuild flow-usage map
        self._flow_map = {}
        for image in self._db.list_images():
            name = str(image["name"])
            references = self._db.image_references(name)
            if references:
                self._flow_map[name] = references

        filter_q = getattr(self, "_filter_edit", None)
        filter_text = filter_q.text().strip().lower() if filter_q else ""

        selected_name = self.selected_name()
        expanded_groups = {
            path.casefold() for path in self._list_widget.expanded_group_paths()
        }
        self._list_widget.blockSignals(True)
        self._list_widget.clear()
        names = self._list_fn()  # also refreshes self._image_meta
        folder_index = self._db.workspace_folder_index()
        self._display_paths = {
            name: folder_index.display_path(name) for name in names
        }
        display_names = list(self._display_paths.values())
        if self._group_filter is not None:
            self._group_filter.set_groups(top_level_image_groups(display_names))
        active_group = self._group_filter.active_group if self._group_filter else ""
        group_items: dict[str, QTreeWidgetItem] = {}
        group_font = QFont(self._list_widget.font())
        group_font.setBold(True)

        def ensure_group(
            parent: QTreeWidgetItem | None,
            group_path: str,
            label: str,
        ) -> QTreeWidgetItem:
            key = group_path.casefold() if group_path else "\x00uncategorized"
            existing = group_items.get(key)
            if existing is not None:
                return existing
            item = QTreeWidgetItem([label])
            item.setData(0, GROUP_PATH_ROLE, group_path)
            item.setFont(0, group_font)
            item.setForeground(0, QBrush(QColor(COLORS["text_secondary"])))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            if parent is None:
                self._list_widget.addTopLevelItem(item)
            else:
                parent.addChild(item)
            group_items[key] = item
            return item

        visible_names = [
            name
            for name in sorted(names, key=image_sort_key)
            if (not active_group or image_matches_top_group(name, active_group))
            and (not filter_text or filter_text in name.lower())
        ]
        if any(not resource_group_path(name) for name in visible_names):
            ensure_group(None, "", "No folder")

        selected_item: QTreeWidgetItem | None = None
        for name in visible_names:
            parent: QTreeWidgetItem | None = None
            path_parts: list[str] = []
            display_group = folder_index.canonicalize_existing(
                resource_group_path(name)
            )
            for part in split_resource_path(display_group):
                path_parts.append(part)
                parent = ensure_group(parent, "/".join(path_parts), part)
            if parent is None:
                parent = group_items["\x00uncategorized"]

            item = QTreeWidgetItem()
            item.setData(0, RESOURCE_ID_ROLE, name)
            display_path = self._display_paths.get(name, name)
            item.setData(0, RESOURCE_PATH_ROLE, display_path)
            item.setToolTip(0, display_path)
            row_widget = self._build_row(name)
            hint = row_widget.sizeHint()
            item.setSizeHint(0, QSize(hint.width(), max(hint.height(), 60)))
            parent.addChild(item)
            self._list_widget.setItemWidget(item, 0, row_widget)
            if name == selected_name:
                selected_item = item

        if filter_text or active_group:
            self._list_widget.expandAll()
        elif self._image_tree_initialized:
            for key, item in group_items.items():
                path = "" if key == "\x00uncategorized" else key
                item.setExpanded(path.casefold() in expanded_groups)
        else:
            self._list_widget.expandToDepth(0)
        self._image_tree_initialized = True
        if selected_item is not None:
            self._list_widget.setCurrentItem(selected_item)
        self._list_widget.blockSignals(False)
        self._on_selection_changed()

    def _rename_image(self, old: str, new: str) -> None:
        ok = self._db.rename_image(old, new)
        if not ok:
            raise ValueError(f"Could not rename '{old}' to '{new}'.")
        self.image_renamed.emit(old, new)

    def _on_delete(self) -> None:
        name = self.selected_name()
        if name is None:
            return
        affected = self._flow_map.get(name, [])
        if affected:
            flow_list = "\n".join(f"  • {flow}" for flow in affected)
            message = (
                f"'{name}' is used by {len(affected)} flow(s):\n\n{flow_list}\n\n"
                "Deleting it will leave broken image references. Delete anyway?"
            )
        else:
            message = f"Delete image '{name}'? This cannot be undone."
        reply = QMessageBox.question(
            self,
            "Delete Image",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self._db.delete_image(name):
                self.image_deleted.emit(name)
            self.refresh()

    # ── Row rendering ─────────────────────────────────────────────

    def _build_group_row(self, label: str, depth: int) -> QWidget:
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        row = QHBoxLayout(container)
        row.setContentsMargins(4 + depth * 18, 3, 4, 3)
        row.setSpacing(6)

        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; font-weight: 700;"
        )
        row.addWidget(lbl)
        row.addStretch()
        return container

    def _build_row(self, name: str) -> QWidget:
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        row = QHBoxLayout(container)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(8)

        # Thumbnail
        thumb_lbl = QLabel()
        thumb_lbl.setFixedSize(40, 40)
        thumb_lbl.setStyleSheet(
            f"background-color: {COLORS['bg_elevated']}; border-radius: 4px;"
        )
        thumb_bytes = self._db.get_image_thumbnail(name, size=40)
        if thumb_bytes:
            pixmap = QPixmap()
            pixmap.loadFromData(thumb_bytes)
            thumb_lbl.setPixmap(
                pixmap.scaled(
                    40, 40,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        row.addWidget(thumb_lbl)

        # Name + dimensions + flow references
        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        display_name = self._display_paths.get(name, name)
        leaf = image_leaf_name(display_name)
        name_lbl = QLabel(leaf)
        name_lbl.setToolTip(display_name)
        name_lbl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 10.5pt;")
        text_col.addWidget(name_lbl)

        meta = next((m for m in self._image_meta if m["name"] == name), None)
        if meta:
            path = "/".join(image_group_parts(display_name))
            dim_text = f"{meta['width']} × {meta['height']} px"
            if path:
                dim_text = f"{path} / {dim_text}"
            dim_lbl = QLabel(dim_text)
            dim_lbl.setStyleSheet(f"color: {COLORS['text_tertiary']}; font-size: 10pt;")
            text_col.addWidget(dim_lbl)

        flows = self._flow_map.get(name, [])
        if flows:
            flow_lbl = QLabel("Used by flows: " + ", ".join(flows))
            flow_lbl.setStyleSheet(f"color: {COLORS['accent']}; font-size: 10pt;")
            text_col.addWidget(flow_lbl)

        row.addLayout(text_col)
        row.addStretch()
        return container
