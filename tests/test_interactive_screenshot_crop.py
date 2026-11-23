from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from automadin.ui.widgets.interactive_screenshot import InteractiveScreenshotWidget


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _widget(
    application: QApplication,
    *,
    image_size: QSize,
    widget_size: QSize,
) -> InteractiveScreenshotWidget:
    widget = InteractiveScreenshotWidget()
    widget.setMinimumSize(0, 0)
    widget.resize(widget_size)
    pixmap = QPixmap(image_size)
    pixmap.fill(QColor("#2468ac"))
    widget.set_screenshot_pixmap(pixmap)
    application.processEvents()
    return widget


def _drag_crop(
    widget: InteractiveScreenshotWidget,
    start: QPoint,
    end: QPoint,
) -> list[QRect]:
    emitted: list[QRect] = []
    widget.crop_selected.connect(emitted.append)
    modifiers = Qt.KeyboardModifier.ShiftModifier
    widget.mousePressEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            start,
            start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            modifiers,
        )
    )
    widget.mouseMoveEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            end,
            end,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            modifiers,
        )
    )
    widget.mouseReleaseEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            end,
            end,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            modifiers,
        )
    )
    return emitted


def test_fit_scale_full_boundary_crop_keeps_last_row_and_column(
    application: QApplication,
) -> None:
    widget = _widget(
        application,
        image_size=QSize(100, 50),
        widget_size=QSize(200, 200),
    )
    try:
        display = widget._display_pixmap
        assert display is not None
        offset = widget._display_offset
        full_display = QRect(offset, display.size())

        assert widget._widget_to_image_rect(full_display) == QRect(0, 0, 100, 50)
    finally:
        widget.deleteLater()


def test_fractional_fit_scale_rounds_outer_edge_up(
    application: QApplication,
) -> None:
    widget = _widget(
        application,
        image_size=QSize(7, 5),
        widget_size=QSize(10, 10),
    )
    try:
        display = widget._display_pixmap
        assert display is not None
        assert display.size() == QSize(9, 7)
        offset = widget._display_offset
        selection = QRect(offset + QPoint(2, 2), offset + QPoint(6, 4))

        assert widget._widget_to_image_rect(selection) == QRect(1, 1, 5, 3)
    finally:
        widget.deleteLater()


def test_reverse_drag_after_zoom_and_pan_emits_exact_source_crop(
    application: QApplication,
) -> None:
    widget = _widget(
        application,
        image_size=QSize(100, 50),
        widget_size=QSize(200, 100),
    )
    try:
        widget._zoom = 2.0
        widget._pan_offset = QPoint(-75, -40)
        widget._update_display()
        assert widget._display_pixmap is not None
        assert widget._display_pixmap.size() == QSize(400, 200)
        offset = widget._display_offset

        top_left = offset + QPoint(40, 60)
        bottom_right = offset + QPoint(79, 99)
        emitted = _drag_crop(widget, bottom_right, top_left)

        assert emitted == [QRect(10, 15, 10, 10)]
        assert widget.get_crop_rect_original() == QRect(10, 15, 10, 10)
    finally:
        widget.deleteLater()


def test_crop_conversion_clamps_each_image_boundary(
    application: QApplication,
) -> None:
    widget = _widget(
        application,
        image_size=QSize(10, 10),
        widget_size=QSize(100, 100),
    )
    try:
        display = widget._display_pixmap
        assert display is not None
        offset = widget._display_offset
        left = offset.x()
        top = offset.y()
        right = left + display.width() - 1
        bottom = top + display.height() - 1

        assert widget._widget_to_image_rect(
            QRect(QPoint(left - 20, top - 20), QPoint(left + 9, top + 9))
        ) == QRect(0, 0, 1, 1)
        assert widget._widget_to_image_rect(
            QRect(QPoint(right - 9, bottom - 9), QPoint(right + 20, bottom + 20))
        ) == QRect(9, 9, 1, 1)
        assert widget._widget_to_image_rect(
            QRect(QPoint(left - 20, top - 20), QPoint(right + 20, bottom + 20))
        ) == QRect(0, 0, 10, 10)
    finally:
        widget.deleteLater()


def test_crop_conversion_returns_empty_rect_for_selection_outside_image(
    application: QApplication,
) -> None:
    widget = _widget(
        application,
        image_size=QSize(10, 10),
        widget_size=QSize(100, 140),
    )
    try:
        offset = widget._display_offset
        above_image = QRect(0, 0, 20, max(1, offset.y() - 1))

        crop = widget._widget_to_image_rect(above_image)

        assert crop.width() == 0
        assert crop.height() == 0
    finally:
        widget.deleteLater()


def test_release_position_reaches_image_edge_without_final_move_event(
    application: QApplication,
) -> None:
    widget = _widget(
        application,
        image_size=QSize(10, 10),
        widget_size=QSize(100, 100),
    )
    emitted: list[QRect] = []
    widget.crop_selected.connect(emitted.append)
    modifiers = Qt.KeyboardModifier.ShiftModifier
    try:
        start = QPoint(20, 20)
        release_outside = QPoint(140, 130)
        widget.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                start,
                start,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                modifiers,
            )
        )
        widget.mouseReleaseEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                release_outside,
                release_outside,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                modifiers,
            )
        )

        assert emitted == [QRect(2, 2, 8, 8)]
    finally:
        widget.deleteLater()


def test_source_coordinate_setter_keeps_exact_crop_across_resize(
    application: QApplication,
) -> None:
    widget = _widget(
        application,
        image_size=QSize(100, 50),
        widget_size=QSize(200, 100),
    )
    try:
        expected = QRect(17, 11, 29, 23)
        widget.set_crop_selection_original(expected)
        first_display = QRect(widget._crop_rect_display)

        widget.resize(317, 223)
        widget._update_display()

        assert widget.get_crop_rect_original() == expected
        assert widget._crop_rect_display is not None
        assert widget._crop_rect_display != first_display
    finally:
        widget.deleteLater()
