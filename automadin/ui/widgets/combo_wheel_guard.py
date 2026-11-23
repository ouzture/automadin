"""Application-wide interaction and readability behavior for combo boxes."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QComboBox

_POPUP_HORIZONTAL_PADDING = 48
_MAX_SCREEN_WIDTH_FRACTION = 0.75
_READABILITY_OPT_OUT_PROPERTY = "automadinReadableComboPopup"


def _readability_enabled(combo: QComboBox) -> bool:
    return combo.property(_READABILITY_OPT_OUT_PROPERTY) is not False


def _sync_current_text_tooltip(combo: QComboBox) -> None:
    """Expose elided selected text without replacing an explicit tooltip."""
    generated = getattr(combo, "_automadin_generated_tooltip", "")
    if combo.toolTip() and combo.toolTip() != generated:
        return

    text = combo.currentText()
    available_width = max(0, combo.width() - _POPUP_HORIZONTAL_PADDING)
    tooltip = text if combo.fontMetrics().horizontalAdvance(text) > available_width else ""
    combo.setToolTip(tooltip)
    combo._automadin_generated_tooltip = tooltip  # type: ignore[attr-defined]


def prepare_combo_box_readability(combo: QComboBox) -> None:
    """Size a combo popup for its labels and provide full-text hover help."""
    if not _readability_enabled(combo):
        return

    popup_view = combo.view()
    metrics = popup_view.fontMetrics()
    content_width = combo.width()
    for index in range(combo.count()):
        text = combo.itemText(index)
        content_width = max(
            content_width,
            metrics.horizontalAdvance(text) + _POPUP_HORIZONTAL_PADDING,
        )
        if not combo.itemData(index, Qt.ItemDataRole.ToolTipRole):
            combo.setItemData(index, text, Qt.ItemDataRole.ToolTipRole)

    screen = combo.screen() or QApplication.primaryScreen()
    if screen is not None:
        screen_limit = int(
            screen.availableGeometry().width() * _MAX_SCREEN_WIDTH_FRACTION
        )
        content_width = min(content_width, max(combo.width(), screen_limit))
    popup_view.setMinimumWidth(content_width)
    _sync_current_text_tooltip(combo)


class ComboBoxBehaviorGuard(QObject):
    """Apply consistent wheel safety and long-label readability to combos."""

    def _configure_readability(self, combo: QComboBox) -> None:
        if not _readability_enabled(combo):
            return
        if not getattr(combo, "_automadin_readability_configured", False):
            combo._automadin_readability_configured = True  # type: ignore[attr-defined]
            combo.currentTextChanged.connect(
                lambda _text, target=combo: _sync_current_text_tooltip(target)
            )
            _sync_current_text_tooltip(combo)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if isinstance(watched, QComboBox):
            self._configure_readability(watched)
            if event.type() in {
                QEvent.Type.Show,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.KeyPress,
            }:
                prepare_combo_box_readability(watched)
            if (
                event.type() == QEvent.Type.Wheel
                and not watched.view().isVisible()
            ):
                return True
        return super().eventFilter(watched, event)


ComboWheelGuard = ComboBoxBehaviorGuard


def install_combo_box_behavior(
    application: QApplication,
) -> ComboBoxBehaviorGuard | None:
    """Install and retain application-wide combo-box behavior."""
    if not isinstance(application, QObject):
        # Startup integration tests use a plain application double rather than
        # a Qt object. Real QApplication instances always take the path below.
        return None
    guard = ComboBoxBehaviorGuard(application)
    application.installEventFilter(guard)
    application._automadin_combo_box_behavior = guard  # type: ignore[attr-defined]
    return guard


def install_combo_wheel_guard(
    application: QApplication,
) -> ComboBoxBehaviorGuard | None:
    """Compatibility wrapper for the original installer name."""
    guard = install_combo_box_behavior(application)
    application._automadin_combo_wheel_guard = guard  # type: ignore[attr-defined]
    return guard
