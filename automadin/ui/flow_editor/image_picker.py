"""Image picker dialog — browse, search, and select template images with thumbnails."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from automadin.ui.image_group_filter import ImageGroupFilterBar
from automadin.ui.image_name_tree import (
    image_group_parts,
    image_leaf_name,
    image_matches_top_group,
    image_sort_key,
    top_level_image_groups,
)
from automadin.ui.theme import COLORS


class ImagePickerDialog(QDialog):
    """Modal dialog to pick one or more template images from the database.

    Shows a searchable list with thumbnails on the left and a larger preview
    on the right. Supports both single-select and multi-select modes.
    """

    def __init__(
        self,
        db: Any,
        selected: list[str] | None = None,
        multi_select: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._all_images: list[dict] = []
        self._selected: list[str] = list(selected or [])
        self._multi_select = multi_select
        self._thumbnail_cache: dict[str, QPixmap] = {}
        self._display_paths: dict[str, str] = {}
        self._populating = False
        self._active_group = ""

        self.setWindowTitle("Select Template Images")
        self.setMinimumSize(700, 500)
        self.resize(800, 550)
        self.setStyleSheet(
            f"QDialog {{ background-color: {COLORS['bg_darkest']}; "
            f"color: {COLORS['text_primary']}; }}"
        )
        self._build_ui()
        self._load_images()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Search bar
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filter images...")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._filter_images)
        search_row.addWidget(self._search_edit, 1)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt;"
        )
        search_row.addWidget(self._count_label)
        layout.addLayout(search_row)

        self._group_filter = ImageGroupFilterBar()
        self._group_filter.group_changed.connect(self._on_group_filter_changed)
        layout.addWidget(self._group_filter, 0)

        # Splitter: image list | preview
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: image list with thumbnails
        self._image_list = QTreeWidget()
        self._image_list.setHeaderHidden(True)
        self._image_list.setRootIsDecorated(True)
        self._image_list.setIconSize(QSize(48, 48))
        self._image_list.setIndentation(18)
        if self._multi_select:
            self._image_list.setSelectionMode(
                QAbstractItemView.SelectionMode.MultiSelection
            )
        self._image_list.currentItemChanged.connect(self._on_item_focused)
        self._image_list.itemSelectionChanged.connect(self._on_selection_changed)
        self._image_list.setStyleSheet(
            f"QTreeWidget {{ background-color: {COLORS['bg_surface']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; }} "
            f"QTreeWidget::item {{ padding: 4px; border-radius: 4px; }} "
            f"QTreeWidget::item:selected {{ "
            f"background-color: {COLORS['accent']}; "
            f"color: {COLORS['text_on_accent']}; }} "
            f"QTreeWidget::item:hover {{ "
            f"background-color: {COLORS['bg_hover']}; }}"
        )
        splitter.addWidget(self._image_list)

        # Right: preview panel
        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(8, 0, 0, 0)

        self._preview_label = QLabel("Select an image to preview")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(200, 200)
        self._preview_label.setStyleSheet(
            f"background-color: {COLORS['bg_surface']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 8px; "
            f"color: {COLORS['text_tertiary']};"
        )
        preview_layout.addWidget(self._preview_label, 1)

        self._info_label = QLabel("")
        self._info_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; padding: 4px;"
        )
        self._info_label.setWordWrap(True)
        preview_layout.addWidget(self._info_label)

        splitter.addWidget(preview_panel)
        splitter.setSizes([450, 250])
        layout.addWidget(splitter, 1)

        # Bottom buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        ok_btn = QPushButton("OK")
        ok_btn.setProperty("cssClass", "primary")
        ok_btn.clicked.connect(self._accept_selection)
        btn_row.addWidget(ok_btn)

        layout.addLayout(btn_row)

    def _load_images(self) -> None:
        """Load all images from DB and populate the list."""
        self._all_images = self._db.list_images()
        folder_index = self._db.workspace_folder_index()
        self._display_paths = {
            str(img["name"]): folder_index.display_path(img["name"])
            for img in self._all_images
        }
        self._group_filter.set_groups(
            top_level_image_groups(list(self._display_paths.values()))
        )
        self._apply_filters()

    def _get_thumbnail(self, name: str) -> QPixmap | None:
        """Get or create a cached thumbnail for an image."""
        if name in self._thumbnail_cache:
            return self._thumbnail_cache[name]
        data = self._db.load_image(name)
        if data is None:
            return None
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        thumb = pixmap.scaled(
            QSize(48, 48),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumbnail_cache[name] = thumb
        return thumb

    def _populate_list(self, images: list[dict], expand_groups: bool = False) -> None:
        """Populate the list widget with image entries."""
        self._populating = True
        self._image_list.blockSignals(True)
        self._image_list.clear()
        group_items: dict[str, QTreeWidgetItem] = {}
        group_font = QFont()
        group_font.setBold(True)

        def ensure_group(
            parent: QTreeWidgetItem | None,
            path: str,
            label: str,
        ) -> QTreeWidgetItem:
            key = path.casefold()
            existing = group_items.get(key)
            if existing is not None:
                return existing
            item = QTreeWidgetItem([label])
            item.setFont(0, group_font)
            item.setForeground(0, QBrush(QColor(COLORS["text_secondary"])))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item.setExpanded(expand_groups)
            if parent is None:
                self._image_list.addTopLevelItem(item)
            else:
                parent.addChild(item)
            group_items[key] = item
            return item

        for info in sorted(
            images,
            key=lambda img: image_sort_key(
                self._display_paths.get(str(img["name"]), str(img["name"]))
            ),
        ):
            name = info["name"]
            display_path = self._display_paths.get(str(name), str(name))
            dims = f"{info['width']}x{info['height']}"
            parent = None
            path_parts: list[str] = []
            for part in image_group_parts(display_path):
                path_parts.append(part)
                parent = ensure_group(parent, "/".join(path_parts), part)

            item = QTreeWidgetItem(
                [f"{image_leaf_name(display_path)}  ({dims})"]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            item.setToolTip(0, display_path)

            thumb = self._get_thumbnail(name)
            if thumb:
                item.setIcon(0, QIcon(thumb))

            # Pre-select if in current selection
            if name in self._selected:
                item.setSelected(True)

            if parent is None:
                self._image_list.addTopLevelItem(item)
            else:
                parent.addChild(item)
        if expand_groups:
            self._image_list.expandAll()
        else:
            self._image_list.collapseAll()
        self._image_list.blockSignals(False)
        self._populating = False

    def _filter_images(self, _text: str) -> None:
        """Filter the image list by search text."""
        self._apply_filters()

    def _on_group_filter_changed(self, group: str) -> None:
        self._active_group = group
        self._apply_filters()

    def _apply_filters(self) -> None:
        text = self._search_edit.text().strip().lower()
        filtered = []
        for img in self._all_images:
            name = str(img["name"])
            display_path = self._display_paths.get(name, name)
            if self._active_group and not image_matches_top_group(
                display_path, self._active_group
            ):
                continue
            if text and text not in display_path.lower():
                continue
            filtered.append(img)
        self._populate_list(filtered, expand_groups=bool(text) or bool(self._active_group))
        self._count_label.setText(f"{len(filtered)}/{len(self._all_images)} images")

    def _on_item_focused(self, current: QTreeWidgetItem | None, _prev=None) -> None:
        """Show preview of the focused image."""
        if current is None:
            return
        name = current.data(0, Qt.ItemDataRole.UserRole)
        if not name:
            return
        data = self._db.load_image(name)
        if data is None:
            return

        pixmap = QPixmap()
        pixmap.loadFromData(data)
        scaled = pixmap.scaled(
            self._preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview_label.setPixmap(scaled)

        # Show info
        info = next((i for i in self._all_images if i["name"] == name), None)
        if info:
            display_path = self._display_paths.get(str(name), str(name))
            self._info_label.setText(
                f"{display_path}\n{info['width']}x{info['height']} px"
            )

    def _visible_image_items(self) -> list[QTreeWidgetItem]:
        items: list[QTreeWidgetItem] = []

        def visit(item: QTreeWidgetItem) -> None:
            name = item.data(0, Qt.ItemDataRole.UserRole)
            if name:
                items.append(item)
            for child_index in range(item.childCount()):
                visit(item.child(child_index))

        for top_index in range(self._image_list.topLevelItemCount()):
            visit(self._image_list.topLevelItem(top_index))
        return items

    def _on_selection_changed(self) -> None:
        """Keep selected image names in a stable, existing-first order."""
        if self._populating:
            return
        visible_items = self._visible_image_items()
        visible_names = {
            str(item.data(0, Qt.ItemDataRole.UserRole))
            for item in visible_items
            if item.data(0, Qt.ItemDataRole.UserRole)
        }
        selected_visible = [
            str(item.data(0, Qt.ItemDataRole.UserRole))
            for item in visible_items
            if item.isSelected() and item.data(0, Qt.ItemDataRole.UserRole)
        ]
        if self._multi_select:
            selected_set = set(selected_visible)
            self._selected = [
                name
                for name in self._selected
                if name not in visible_names or name in selected_set
            ]
            for name in selected_visible:
                if name not in self._selected:
                    self._selected.append(name)
        else:
            self._selected = selected_visible[:1]

    def _accept_selection(self) -> None:
        """Collect selected items and accept the dialog."""
        self._on_selection_changed()
        self.accept()

    def selected_images(self) -> list[str]:
        """Return the list of selected image names."""
        return list(self._selected)
