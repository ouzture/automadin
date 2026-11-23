"""Compact accessible on/off switch for utility controls."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QWidget

from automadin.ui.theme import COLORS


class ToggleSwitch(QAbstractButton):
    """A small painted switch that follows the active Automadin theme."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.sizeHint())

    def sizeHint(self) -> QSize:
        return QSize(42, 24)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        track = QRectF(1.0, 3.0, self.width() - 2.0, self.height() - 6.0)
        if not self.isEnabled():
            track_color = QColor(COLORS["border_light"])
            thumb_color = QColor(COLORS["text_tertiary"])
        elif self.isChecked():
            track_color = QColor(COLORS["primary"])
            thumb_color = QColor(COLORS["text_on_accent"])
        else:
            track_color = QColor(COLORS["bg_elevated"])
            thumb_color = QColor(COLORS["text_secondary"])

        border_color = (
            QColor(COLORS["primary_hover"])
            if self.hasFocus()
            else QColor(COLORS["border"])
        )
        painter.setPen(QPen(border_color, 1.0))
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)

        radius = 7.0
        center_x = track.right() - radius - 2.0 if self.isChecked() else track.left() + radius + 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(thumb_color)
        painter.drawEllipse(
            QPointF(center_x, track.center().y()),
            radius,
            radius,
        )
