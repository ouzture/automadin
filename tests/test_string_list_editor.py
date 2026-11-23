from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLineEdit, QListWidget

from automadin.ui.flow_editor.param_editor import ParamEditorWidget
from automadin.ui.theme import COLORS


def test_string_list_inline_editor_has_explicit_readable_palette() -> None:
    application = QApplication.instance() or QApplication([])
    widget = ParamEditorWidget(
        {"name": "arguments", "type": "string_list", "label": "Launch Arguments"},
        ["--profile selected-value"],
        lambda _name, _value: None,
    )
    widget.show()
    application.processEvents()
    try:
        values = widget.findChild(QListWidget)
        assert values is not None
        values.setCurrentRow(0)
        values.editItem(values.item(0))
        application.processEvents()

        editor = values.findChild(QLineEdit)
        assert editor is not None
        palette = editor.palette()
        assert palette.color(QPalette.ColorRole.Base).name() == COLORS[
            "bg_elevated"
        ].lower()
        assert palette.color(QPalette.ColorRole.Text).name() == COLORS[
            "text_primary"
        ].lower()
        assert palette.color(QPalette.ColorRole.Highlight).name() == COLORS[
            "primary"
        ].lower()
        assert palette.color(QPalette.ColorRole.HighlightedText).name() == COLORS[
            "text_on_accent"
        ].lower()
    finally:
        widget.close()
        widget.deleteLater()
        application.processEvents()
