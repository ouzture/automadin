"""InteractiveScreenshotWidget — reusable zoomable screenshot widget.

Features:
- Scroll wheel zoom (centered on cursor)
- Middle-mouse-drag pan
- Double-click to reset to fit-view
- Shift+drag crop selection (disable with enable_crop=False)
- Ctrl+click to copy coordinates to clipboard
- Mouse tracking for coordinate display
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QLabel, QWidget

from automadin.services.find_image import MatchResult
from automadin.ui.theme import COLORS


class InteractiveScreenshotWidget(QLabel):
    """QLabel that displays a screenshot with zoom/pan support.

    Args:
        enable_crop: When True (default), shift+drag draws a crop rectangle
            and emits ``crop_selected``. Set to False in contexts where
            cropping is not needed (e.g. the flow runner screenshot panel).
    """

    coords_changed = Signal(int, int, float, float)  # px_x, px_y, pct_x, pct_y
    coords_cleared = Signal()
    crop_selected = Signal(QRect)  # crop rect in original image coords
    coord_clicked = Signal(int, int, float, float)  # for ctrl+click
    image_clicked = Signal(int, int, float, float)  # normal click in image coords

    def __init__(
        self,
        parent: QWidget | None = None,
        enable_crop: bool = True,
    ) -> None:
        super().__init__(parent)
        self._enable_crop = enable_crop
        self._crop_requires_shift = True
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMouseTracking(True)
        self.setMinimumSize(480, 270)
        self.setStyleSheet(
            f"background-color: {COLORS['bg_surface']}; "
            f"border: 1px solid {COLORS['border']}; "
            f"border-radius: 8px; color: {COLORS['text_tertiary']};"
        )

        self._original_pixmap: QPixmap | None = None
        self._clean_pixmap: QPixmap | None = None  # pre-overlay snapshot
        self._original_size: QSize | None = None  # actual image dimensions
        self._display_pixmap: QPixmap | None = None
        self._display_offset = QPoint(0, 0)  # where scaled image starts within widget

        # Zoom / pan state
        self._zoom: float = 1.0  # 1.0 = fit-to-view
        self._pan_offset = QPoint(0, 0)
        self._panning: bool = False
        self._pan_start: QPoint | None = None
        self._pan_start_offset: QPoint | None = None

        # Crop selection state
        self._is_cropping = False
        self._crop_start: QPoint | None = None
        self._crop_current: QPoint | None = None
        self._crop_rect_display: QRect | None = None  # in widget coords
        self._crop_rect_original: QRect | None = None

    def set_screenshot_pixmap(self, pixmap: QPixmap) -> None:
        """Set the screenshot to display."""
        self._original_pixmap = pixmap
        self._clean_pixmap = pixmap.copy()
        self._original_size = pixmap.size()
        self._crop_rect_display = None
        self._crop_rect_original = None
        self._is_cropping = False
        self._zoom = 1.0
        self._pan_offset = QPoint(0, 0)
        self._update_display()

    @property
    def has_overlays(self) -> bool:
        """True if match overlays have been drawn on top of the clean image."""
        return self._clean_pixmap is not None and self._original_pixmap is not self._clean_pixmap

    def clear_overlays(self) -> None:
        """Restore the original screenshot without match overlays (preserves zoom/pan)."""
        if self._clean_pixmap is None:
            return
        self._original_pixmap = self._clean_pixmap.copy()
        self._update_display()

    def clear_screenshot(self) -> None:
        """Reset every image-derived interaction and display state."""
        self._original_pixmap = None
        self._clean_pixmap = None
        self._original_size = None
        self._display_pixmap = None
        self._display_offset = QPoint(0, 0)
        self._zoom = 1.0
        self._pan_offset = QPoint(0, 0)
        self._panning = False
        self._pan_start = None
        self._pan_start_offset = None
        self._is_cropping = False
        self._crop_start = None
        self._crop_current = None
        self._crop_rect_display = None
        self._crop_rect_original = None
        self.clear()
        self.coords_cleared.emit()
        self.update()

    def set_crop_requires_shift(self, required: bool) -> None:
        self._crop_requires_shift = required

    def clear_crop_selection(self) -> None:
        """Clear the active crop rectangle without resetting zoom or pan."""
        self._is_cropping = False
        self._crop_start = None
        self._crop_current = None
        self._crop_rect_display = None
        self._crop_rect_original = None
        self._repaint()

    def get_crop_rect_original(self) -> QRect | None:
        """Return the crop rectangle in original image coordinates."""
        if self._crop_rect_original is not None:
            return QRect(self._crop_rect_original)
        if self._crop_rect_display is None or self._original_pixmap is None:
            return None
        return self._widget_to_image_rect(self._crop_rect_display)

    def set_crop_selection_original(self, rect: QRect | None) -> None:
        """Replace the visible selection using exact source-image coordinates."""
        if (
            rect is None
            or self._original_size is None
            or self._display_pixmap is None
        ):
            self.clear_crop_selection()
            return
        image_width = self._original_size.width()
        image_height = self._original_size.height()
        x = max(0, min(rect.x(), image_width))
        y = max(0, min(rect.y(), image_height))
        width = max(0, min(rect.width(), image_width - x))
        height = max(0, min(rect.height(), image_height - y))
        if width <= 0 or height <= 0:
            self.clear_crop_selection()
            return
        self._crop_rect_original = QRect(x, y, width, height)
        self._crop_rect_display = self._image_to_widget_rect(
            self._crop_rect_original
        )
        self._repaint()

    def _update_display(self) -> None:
        """Scale and position the original pixmap according to current zoom/pan."""
        if self._original_pixmap is None:
            return
        ww = self.width()
        wh = self.height()
        ow = self._original_pixmap.width()
        oh = self._original_pixmap.height()
        fit_scale = min(ww / ow, wh / oh)
        dw = max(1, int(ow * fit_scale * self._zoom))
        dh = max(1, int(oh * fit_scale * self._zoom))

        scaled = self._original_pixmap.scaled(
            QSize(dw, dh),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._display_pixmap = scaled
        # Use actual dimensions (KeepAspectRatio may return 1px smaller than requested)
        adw = scaled.width()
        adh = scaled.height()

        if self._zoom <= 1.0:
            self._display_offset = QPoint((ww - adw) // 2, (wh - adh) // 2)
        else:
            self._display_offset = self._clamp_offset(self._pan_offset, adw, adh)

        if self._crop_rect_original is not None:
            self._crop_rect_display = self._image_to_widget_rect(
                self._crop_rect_original
            )
        self._repaint()

    def _clamp_offset(self, offset: QPoint, dw: int, dh: int) -> QPoint:
        """Clamp pan offset so image stays within widget bounds."""
        ww = self.width()
        wh = self.height()
        x = (ww - dw) // 2 if dw <= ww else max(ww - dw, min(0, offset.x()))
        y = (wh - dh) // 2 if dh <= wh else max(wh - dh, min(0, offset.y()))
        return QPoint(x, y)

    def _repaint(self) -> None:
        """Repaint with any overlays (crop rect, match rects)."""
        if self._display_pixmap is None:
            return
        canvas = self._display_pixmap.copy()
        painter = QPainter(canvas)

        # Draw crop rectangle if active (and crop is enabled)
        if self._enable_crop and self._crop_rect_display is not None:
            pen = QPen(QColor(COLORS["accent"]), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            # Adjust rect relative to display offset
            adjusted = self._crop_rect_display.translated(-self._display_offset)
            painter.drawRect(adjusted)

            # Semi-transparent fill
            fill_color = QColor(COLORS["accent"])
            fill_color.setAlpha(30)
            painter.fillRect(adjusted, fill_color)

        painter.end()

        # Set the pixmap with overlays
        result = QPixmap(self.size())
        result.fill(QColor(COLORS["bg_surface"]))
        result_painter = QPainter(result)
        result_painter.drawPixmap(self._display_offset, canvas)
        result_painter.end()

        super().setPixmap(result)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_display()

    def _widget_to_image_coords(self, pos: QPoint) -> tuple[int, int] | None:
        """Convert widget coordinates to original image pixel coordinates."""
        if self._original_pixmap is None or self._display_pixmap is None:
            return None

        # Position relative to displayed image
        rx = pos.x() - self._display_offset.x()
        ry = pos.y() - self._display_offset.y()

        # Check if within displayed image bounds
        dw = self._display_pixmap.width()
        dh = self._display_pixmap.height()
        if rx < 0 or ry < 0 or rx >= dw or ry >= dh:
            return None

        # Scale to original image coordinates
        ow = self._original_size.width()
        oh = self._original_size.height()
        img_x = int(rx * ow / dw)
        img_y = int(ry * oh / dh)
        return (min(img_x, ow - 1), min(img_y, oh - 1))

    def _widget_to_image_rect(self, widget_rect: QRect) -> QRect:
        """Convert an inclusive widget-pixel rect to a half-open image crop.

        ``QRect.right()`` and ``bottom()`` are inclusive.  Image slicing uses
        an exclusive lower-right edge, so convert ``right + 1``/``bottom + 1``
        and round those edges up.  This keeps the final source row and column
        when a selection reaches an image boundary, including at fractional
        display scales.
        """
        if self._original_pixmap is None or self._display_pixmap is None:
            return QRect()

        dw = self._display_pixmap.width()
        dh = self._display_pixmap.height()
        ow = self._original_size.width()
        oh = self._original_size.height()
        ox = self._display_offset.x()
        oy = self._display_offset.y()

        display_left = max(0, min(dw, widget_rect.left() - ox))
        display_top = max(0, min(dh, widget_rect.top() - oy))
        display_right = max(0, min(dw, widget_rect.right() - ox + 1))
        display_bottom = max(0, min(dh, widget_rect.bottom() - oy + 1))

        x1 = display_left * ow // dw
        y1 = display_top * oh // dh
        if display_right <= display_left or display_bottom <= display_top:
            return QRect(x1, y1, 0, 0)
        x2 = min(ow, (display_right * ow + dw - 1) // dw)
        y2 = min(oh, (display_bottom * oh + dh - 1) // dh)

        return QRect(x1, y1, max(0, x2 - x1), max(0, y2 - y1))

    def _image_to_widget_rect(self, image_rect: QRect) -> QRect:
        """Project one half-open source-image crop onto displayed pixels."""
        if self._original_size is None or self._display_pixmap is None:
            return QRect()
        image_width = self._original_size.width()
        image_height = self._original_size.height()
        display_width = self._display_pixmap.width()
        display_height = self._display_pixmap.height()
        left = (
            self._display_offset.x()
            + image_rect.x() * display_width // image_width
        )
        top = (
            self._display_offset.y()
            + image_rect.y() * display_height // image_height
        )
        right = self._display_offset.x() + (
            (image_rect.x() + image_rect.width()) * display_width
            + image_width
            - 1
        ) // image_width
        bottom = self._display_offset.y() + (
            (image_rect.y() + image_rect.height()) * display_height
            + image_height
            - 1
        ) // image_height
        return QRect(
            left,
            top,
            max(1, right - left),
            max(1, bottom - top),
        )

    def _clamp_to_displayed_image(self, pos: QPoint) -> QPoint:
        if self._display_pixmap is None:
            return QPoint(pos)
        left = self._display_offset.x()
        top = self._display_offset.y()
        right = left + self._display_pixmap.width() - 1
        bottom = top + self._display_pixmap.height() - 1
        return QPoint(
            max(left, min(right, pos.x())),
            max(top, min(bottom, pos.y())),
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if self._panning and self._pan_start is not None:
            delta = pos - self._pan_start
            self._pan_offset = self._pan_start_offset + delta
            self._update_display()
            return

        if self._enable_crop and self._is_cropping and self._crop_start is not None:
            self._crop_current = self._clamp_to_displayed_image(pos)
            self._crop_rect_display = QRect(
                self._crop_start, self._crop_current
            ).normalized()
            self._repaint()

        coords = self._widget_to_image_coords(pos)
        if coords is None:
            self.coords_cleared.emit()
            return

        px_x, px_y = coords
        ow = self._original_size.width()
        oh = self._original_size.height()
        pct_x = px_x / ow * 100.0
        pct_y = px_y / oh * 100.0
        self.coords_changed.emit(px_x, px_y, pct_x, pct_y)

        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if event.button() == Qt.MouseButton.MiddleButton and self._zoom > 1.0:
            self._panning = True
            self._pan_start = pos
            self._pan_start_offset = QPoint(self._display_offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        coords = self._widget_to_image_coords(pos)
        if coords is None:
            super().mousePressEvent(event)
            return

        modifiers = event.modifiers()

        # Ctrl+click → copy coords to clipboard
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            px_x, px_y = coords
            ow = self._original_size.width()
            oh = self._original_size.height()
            pct_x = px_x / ow * 100.0
            pct_y = px_y / oh * 100.0
            self.coord_clicked.emit(px_x, px_y, pct_x, pct_y)
            return

        should_crop = self._enable_crop and (
            not self._crop_requires_shift
            or bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        )
        if should_crop:
            self._is_cropping = True
            self._crop_start = pos
            self._crop_current = pos
            self._crop_rect_display = None
            self._crop_rect_original = None
            return

        px_x, px_y = coords
        ow = self._original_size.width()
        oh = self._original_size.height()
        self.image_clicked.emit(px_x, px_y, px_x / ow * 100.0, px_y / oh * 100.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._is_cropping:
            self._is_cropping = False
            if self._crop_start is not None and self._crop_current is not None:
                self._crop_current = self._clamp_to_displayed_image(
                    event.position().toPoint()
                )
                self._crop_rect_display = QRect(
                    self._crop_start, self._crop_current
                ).normalized()
                # Only emit if the rect is meaningful (> 5x5 pixels in widget space)
                if self._crop_rect_display.width() > 5 and self._crop_rect_display.height() > 5:
                    img_rect = self._widget_to_image_rect(self._crop_rect_display)
                    self._crop_rect_original = QRect(img_rect)
                    self.crop_selected.emit(img_rect)
                else:
                    self._crop_rect_display = None
                    self._crop_rect_original = None
                self._repaint()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self.coords_cleared.emit()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        if self._original_pixmap is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        pos = event.position().toPoint()
        img_coords = self._widget_to_image_coords(pos)
        factor = 1.15 if delta > 0 else 1 / 1.15
        new_zoom = max(1.0, min(8.0, self._zoom * factor))
        if new_zoom == self._zoom:
            return
        self._zoom = new_zoom
        if new_zoom <= 1.0:
            self._zoom = 1.0
            self._pan_offset = QPoint(0, 0)
        elif img_coords is not None:
            ow = self._original_pixmap.width()
            oh = self._original_pixmap.height()
            fit_scale = min(self.width() / ow, self.height() / oh)
            new_x = pos.x() - int(img_coords[0] * fit_scale * new_zoom)
            new_y = pos.y() - int(img_coords[1] * fit_scale * new_zoom)
            self._pan_offset = QPoint(new_x, new_y)
        self._update_display()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._zoom = 1.0
            self._pan_offset = QPoint(0, 0)
            self._update_display()
            event.accept()

    def draw_match_overlays(self, matches: list[MatchResult]) -> None:
        """Draw green rectangles on the screenshot for template matches."""
        if self._original_pixmap is None:
            return

        # Work on a copy of the original
        annotated = self._original_pixmap.copy()
        painter = QPainter(annotated)
        pen = QPen(QColor(COLORS["overlay_match"]), 3)
        painter.setPen(pen)

        for match in matches:
            painter.drawRect(match.x, match.y, match.width, match.height)
            # Draw label
            label_color = QColor(COLORS["overlay_match"])
            label_color.setAlpha(180)
            label_rect = QRect(match.x, match.y - 20, match.width, 20)
            painter.fillRect(label_rect, label_color)
            painter.setPen(QColor(COLORS["text_on_accent"]))
            painter.drawText(
                label_rect, Qt.AlignmentFlag.AlignCenter,
                f"{match.confidence:.0%}"
            )
            painter.setPen(pen)

        painter.end()

        # Store annotated as display source and re-render
        self._original_pixmap = annotated
        self._update_display()

    def draw_rect_overlays(
        self,
        rects: list[tuple[int, int, int, int]],
        labels: list[str] | None = None,
        color: str | None = None,
    ) -> None:
        """Draw labeled rectangles on the screenshot."""
        if self._original_pixmap is None:
            return

        color = color or COLORS["overlay_match"]
        annotated = self._original_pixmap.copy()
        painter = QPainter(annotated)
        pen = QPen(QColor(color), 3)
        painter.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(180)

        for idx, (x, y, w, h) in enumerate(rects):
            painter.setPen(pen)
            painter.drawRect(x, y, w, h)
            if labels and idx < len(labels):
                label_rect = QRect(x, max(0, y - 22), max(44, w), 22)
                painter.fillRect(label_rect, fill)
                painter.setPen(QColor(COLORS["text_on_accent"]))
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, labels[idx])

        painter.end()
        self._original_pixmap = annotated
        self._update_display()
