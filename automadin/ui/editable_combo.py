"""Small helpers for editable combo boxes with suggested values."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox


def set_editable_combo_text(combo: QComboBox, value: str) -> None:
    """Select an existing value or preserve a non-listed editable value.

    ``QComboBox.setCurrentText`` only changes the line edit when the combo is
    editable.  Its selected index can remain on a different item (commonly the
    blank folder entry), which lets the displayed value revert when the widget
    receives focus.  Keep the model selection and edit text in agreement.
    """
    text = str(value or "")
    index = combo.findText(text, Qt.MatchFlag.MatchFixedString)
    if index >= 0:
        combo.setCurrentIndex(index)
        return
    combo.setCurrentIndex(-1)
    combo.setEditText(text)


__all__ = ["set_editable_combo_text"]
