"""A small reusable flow layout for controls that must wrap when space narrows."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget


class WrappingLayout(QLayout):
    """Lay widgets left-to-right and continue on a new row when needed."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        horizontal_spacing: int = 8,
        vertical_spacing: int = 6,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._horizontal_spacing = horizontal_spacing
        self._vertical_spacing = vertical_spacing

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt API
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt API
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt API
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802 - Qt API
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        return self._arrange(QRect(0, 0, width, 0), apply_geometry=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt API
        super().setGeometry(rect)
        self._arrange(rect, apply_geometry=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt API
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _arrange(self, rect: QRect, *, apply_geometry: bool) -> int:
        margins = self.contentsMargins()
        usable = rect.adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )
        x = usable.x()
        y = usable.y()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint().expandedTo(item.minimumSize())
            next_x = x + hint.width() + self._horizontal_spacing
            if line_height and next_x - self._horizontal_spacing > usable.right() + 1:
                x = usable.x()
                y += line_height + self._vertical_spacing
                next_x = x + hint.width() + self._horizontal_spacing
                line_height = 0
            if apply_geometry:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()
