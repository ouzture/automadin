"""Shared presentation helpers for application combo boxes."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QSizePolicy


def configure_combo_box(
    combo: QComboBox,
    *,
    density: str | None = None,
    minimum_contents_length: int | None = None,
) -> QComboBox:
    """Apply a theme-backed combo variant and keep its size content-aware."""
    combo.setProperty("controlDensity", density)
    if minimum_contents_length is not None:
        combo.setMinimumContentsLength(minimum_contents_length)
    combo.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
    style = combo.style()
    style.unpolish(combo)
    style.polish(combo)
    combo.updateGeometry()
    return combo


__all__ = ["configure_combo_box"]
