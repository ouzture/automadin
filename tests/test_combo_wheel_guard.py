from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox

from automadin.ui.widgets.combo_wheel_guard import (
    install_combo_box_behavior,
    install_combo_wheel_guard,
)


def _wheel_event() -> QWheelEvent:
    return QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def test_closed_combo_ignores_wheel_but_keeps_keyboard_selection() -> None:
    application = QApplication.instance() or QApplication([])
    guard = install_combo_wheel_guard(application)
    combo = QComboBox()
    combo.addItems(["First", "Second", "Third"])
    combo.setCurrentIndex(1)
    try:
        QApplication.sendEvent(combo, _wheel_event())
        assert combo.currentIndex() == 1

        QApplication.sendEvent(
            combo,
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Down,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        assert combo.currentIndex() == 2
    finally:
        application.removeEventFilter(guard)
        combo.deleteLater()
        guard.deleteLater()
        application.processEvents()


def test_open_combo_is_not_blocked_by_wheel_guard() -> None:
    application = QApplication.instance() or QApplication([])
    guard = install_combo_wheel_guard(application)
    combo = QComboBox()
    combo.addItems(["First", "Second", "Third"])
    combo.show()
    combo.showPopup()
    application.processEvents()
    try:
        assert combo.view().isVisible()
        assert not guard.eventFilter(combo, _wheel_event())
    finally:
        combo.hidePopup()
        application.removeEventFilter(guard)
        combo.deleteLater()
        guard.deleteLater()
        application.processEvents()


def test_plain_combo_gets_readable_popup_and_elided_text_tooltip() -> None:
    application = QApplication.instance() or QApplication([])
    guard = install_combo_box_behavior(application)
    combo = QComboBox()
    combo.resize(90, 30)
    long_label = "Built-in — _match_x (integer coordinate from matched image)"
    combo.addItems(["Select value", long_label])
    combo.setCurrentIndex(1)
    combo.show()
    application.processEvents()
    try:
        assert combo.toolTip() == long_label
        assert combo.itemData(1, Qt.ItemDataRole.ToolTipRole) == long_label
        assert combo.view().minimumWidth() > combo.width()
    finally:
        application.removeEventFilter(guard)
        combo.deleteLater()
        guard.deleteLater()
        application.processEvents()


def test_combo_readability_preserves_explicit_tooltip_and_supports_opt_out() -> None:
    application = QApplication.instance() or QApplication([])
    guard = install_combo_box_behavior(application)
    explicit = QComboBox()
    explicit.setToolTip("Choose a launch target")
    explicit.addItem("A very long launch target label that will not fit")
    opted_out = QComboBox()
    opted_out.setProperty("automadinReadableComboPopup", False)
    opted_out.addItem("Another very long label that will not fit")
    explicit.show()
    opted_out.show()
    application.processEvents()
    try:
        assert explicit.toolTip() == "Choose a launch target"
        assert opted_out.toolTip() == ""
        assert opted_out.itemData(0, Qt.ItemDataRole.ToolTipRole) is None
    finally:
        application.removeEventFilter(guard)
        explicit.deleteLater()
        opted_out.deleteLater()
        guard.deleteLater()
        application.processEvents()
