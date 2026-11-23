from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from automadin.ui.flow_editor.param_editor import ParamEditorWidget


def test_key_list_actions_have_clear_labels_and_tooltips() -> None:
    application = QApplication.instance() or QApplication([])
    widget = ParamEditorWidget(
        {"name": "keys", "type": "key_list", "label": "Keys"},
        ["Control", "Enter"],
        lambda _name, _value: None,
    )
    try:
        buttons = {
            button.text(): button for button in widget.findChildren(QPushButton)
        }

        assert buttons["Add key"].toolTip() == "Add selected key"
        assert buttons["Remove"].toolTip() == "Remove selected key"
        assert "+" not in buttons
        assert "-" not in buttons
    finally:
        widget.deleteLater()
        application.processEvents()
