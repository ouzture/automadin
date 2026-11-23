"""Reusable scalable top-level folder filter for image libraries."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QSizePolicy, QWidget

from automadin.ui.theme import COLORS


class ImageGroupFilterBar(QWidget):
    """Compact folder selector that remains usable with many top-level groups."""

    group_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._groups: list[str] = []
        self._active_group = ""

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel("Top folder")
        label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10pt;")
        layout.addWidget(label)

        self._combo = QComboBox()
        self._combo.setMinimumWidth(180)
        self._combo.setMaximumWidth(300)
        self._combo.currentIndexChanged.connect(self._on_selection_changed)
        layout.addWidget(self._combo)
        layout.addStretch()
        self.set_groups([])

    @property
    def active_group(self) -> str:
        return self._active_group

    def set_groups(self, groups: list[str]) -> None:
        normalized = sorted(
            {group.strip() for group in groups if group.strip()},
            key=str.casefold,
        )
        if normalized == self._groups and self._combo.count():
            return
        current = self._active_group
        self._groups = normalized
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("All folders", "")
        for group in normalized:
            self._combo.addItem(group, group)
        index = self._combo.findData(current)
        self._combo.setCurrentIndex(index if index >= 0 else 0)
        self._active_group = str(self._combo.currentData() or "")
        self._combo.blockSignals(False)

    def _on_selection_changed(self, _index: int) -> None:
        group = str(self._combo.currentData() or "")
        if group == self._active_group:
            return
        self._active_group = group
        self.group_changed.emit(group)
