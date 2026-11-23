"""Shared presentation helpers for application buttons."""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QSizePolicy


def configure_button(
    button: QPushButton,
    *,
    semantic: str | None = None,
    appearance: str | None = None,
    density: str | None = None,
    minimum_width: int | None = None,
) -> QPushButton:
    """Apply a theme-backed button variant without constraining text height.

    Button height must come from Qt's size hint so translated text, user fonts,
    and display scaling cannot be clipped by a fixed pixel height.
    """
    button.setProperty("cssClass", semantic)
    button.setProperty("buttonAppearance", appearance)
    button.setProperty("buttonDensity", density)
    if minimum_width is not None:
        button.setProperty("buttonMinimumWidth", minimum_width)
    button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
    _refresh_button_style(button)
    fit_button_to_contents(button)
    return button


def set_button_semantic(button: QPushButton, semantic: str | None) -> None:
    """Change only a button's semantic color and refresh its Qt style."""
    button.setProperty("cssClass", semantic)
    _refresh_button_style(button)
    fit_button_to_contents(button)


def fit_button_to_contents(button: QPushButton) -> None:
    """Prevent layouts from compressing a text button below its size hint."""
    configured_width = button.property("buttonMinimumWidth")
    baseline = configured_width if isinstance(configured_width, int) else 0
    button.setMinimumWidth(max(baseline, button.sizeHint().width()))


def _refresh_button_style(button: QPushButton) -> None:
    style = button.style()
    style.unpolish(button)
    style.polish(button)
    button.updateGeometry()
