"""Reusable hierarchy for slash-qualified Automadin resources."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem, QWidget

from automadin.models.resource_path import (
    WorkspaceFolderIndex,
    normalize_resource_path,
    resource_group_path,
    resource_leaf_name,
    resource_sort_key,
    split_resource_path,
)
from automadin.ui.theme import COLORS

RESOURCE_ID_ROLE = int(Qt.ItemDataRole.UserRole)
RESOURCE_PATH_ROLE = RESOURCE_ID_ROLE + 1
GROUP_PATH_ROLE = RESOURCE_ID_ROLE + 2
_UNCATEGORIZED_KEY = "\x00uncategorized"


class ResourceTreeWidget(QTreeWidget):
    """Collapsible folder tree that keeps stored resource identity in item data."""

    resource_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._initialized = False
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setIndentation(18)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformRowHeights(False)
        self.setStyleSheet(
            f"QTreeWidget {{ background-color: {COLORS['bg_darkest']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; "
            "font-size: 10.5pt; }"
            "QTreeWidget::item { padding: 6px 8px; border-radius: 4px; }"
            f"QTreeWidget::item:selected {{ background-color: {COLORS['bg_elevated']}; "
            f"color: {COLORS['text_primary']}; }}"
            f"QTreeWidget::item:hover {{ background-color: {COLORS['bg_hover']}; }}"
        )
        self.itemDoubleClicked.connect(self._on_item_double_clicked)

    def selected_resource_id(self) -> str | None:
        items = self.selectedItems()
        if not items:
            return None
        value = items[0].data(0, RESOURCE_ID_ROLE)
        return str(value) if value else None

    def selected_resource_path(self) -> str | None:
        items = self.selectedItems()
        if not items:
            return None
        value = items[0].data(0, RESOURCE_PATH_ROLE)
        return str(value) if value else None

    def expanded_group_paths(self) -> set[str]:
        expanded: set[str] = set()

        def visit(item: QTreeWidgetItem) -> None:
            group = item.data(0, GROUP_PATH_ROLE)
            if group is not None and item.isExpanded():
                expanded.add(str(group))
            for index in range(item.childCount()):
                visit(item.child(index))

        for index in range(self.topLevelItemCount()):
            visit(self.topLevelItem(index))
        return expanded

    def set_resources(
        self,
        resources: Iterable[tuple[str, str]],
        *,
        query: str = "",
        canonical_folders: Iterable[str] = (),
        details: dict[str, str] | None = None,
    ) -> None:
        """Populate from ``(stable identity, display path)`` pairs."""
        selected_id = self.selected_resource_id()
        expanded = {path.casefold() for path in self.expanded_group_paths()}
        normalized_query = query.strip().casefold()
        raw_entries: list[tuple[str, str]] = []
        for identity, path in resources:
            normalized_path = normalize_resource_path(path)
            raw_entries.append((str(identity), normalized_path))
        known_paths = [path for _, path in raw_entries]
        preferred_groups = list(canonical_folders)
        folder_index = WorkspaceFolderIndex(
            explicit_groups=preferred_groups,
            resource_paths=known_paths,
        )
        entries = []
        for identity, normalized_path in raw_entries:
            if normalized_query and normalized_query not in normalized_path.casefold():
                continue
            raw_group = resource_group_path(normalized_path)
            group = folder_index.canonicalize_existing(raw_group)
            display_path = normalize_resource_path(
                f"{group}/{resource_leaf_name(normalized_path)}"
                if group
                else resource_leaf_name(normalized_path)
            )
            entries.append((identity, display_path))
        entries.sort(key=lambda entry: resource_sort_key(entry[1]))

        self.blockSignals(True)
        self.clear()
        group_items: dict[str, QTreeWidgetItem] = {}
        group_font = QFont(self.font())
        group_font.setBold(True)

        def ensure_group(path: str, label: str, parent: QTreeWidgetItem | None) -> QTreeWidgetItem:
            key = path.casefold() if path else _UNCATEGORIZED_KEY
            existing = group_items.get(key)
            if existing is not None:
                return existing
            item = QTreeWidgetItem([label])
            item.setData(0, GROUP_PATH_ROLE, path)
            item.setFont(0, group_font)
            item.setForeground(0, QBrush(QColor(COLORS["text_secondary"])))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            if parent is None:
                self.addTopLevelItem(item)
            else:
                parent.addChild(item)
            group_items[key] = item
            return item

        uncategorized_entries = [entry for entry in entries if not resource_group_path(entry[1])]
        if uncategorized_entries:
            ensure_group("", "No folder", None)

        selected_item: QTreeWidgetItem | None = None
        first_item: QTreeWidgetItem | None = None
        for identity, path in entries:
            group = resource_group_path(path)
            parent: QTreeWidgetItem | None = None
            if group:
                path_parts: list[str] = []
                for part in split_resource_path(group):
                    path_parts.append(part)
                    group_path = "/".join(path_parts)
                    parent = ensure_group(group_path, part, parent)
            else:
                parent = group_items[_UNCATEGORIZED_KEY]

            detail = (details or {}).get(identity, "").strip()
            label = resource_leaf_name(path)
            if detail:
                label = f"{label}\n{detail}"
            item = QTreeWidgetItem([label])
            item.setData(0, RESOURCE_ID_ROLE, identity)
            item.setData(0, RESOURCE_PATH_ROLE, path)
            item.setToolTip(0, f"{path}\n{detail}" if detail else path)
            parent.addChild(item)
            if first_item is None:
                first_item = item
            if identity == selected_id:
                selected_item = item

        if normalized_query:
            self.expandAll()
        elif self._initialized:
            for key, item in group_items.items():
                group_path = "" if key == _UNCATEGORIZED_KEY else key
                item.setExpanded(group_path.casefold() in expanded)
        else:
            self.expandToDepth(0)
        self._initialized = True

        if selected_item is not None or first_item is not None:
            self.setCurrentItem(selected_item or first_item)
        self.blockSignals(False)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        value = item.data(0, RESOURCE_ID_ROLE)
        if value:
            self.resource_activated.emit(str(value))
