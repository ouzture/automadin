from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from automadin.ui.flow_editor.android_package_picker import (
    AndroidPackagePickerDialog,
)
from automadin.ui.flow_editor.param_editor import ParamEditorWidget


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_package_picker_loads_filters_and_returns_selection() -> None:
    app = _app()
    dialog = AndroidPackagePickerDialog(
        lambda: ["com.example.alpha", "com.example.beta"],
        current_value="com.example.beta",
    )
    assert dialog._worker.wait(2000)
    app.processEvents()

    assert dialog._packages.count() == 2
    assert dialog._packages.currentItem().text() == "com.example.beta"

    dialog._filter.setText("alpha")
    dialog._packages.setCurrentRow(0)
    dialog.accept()

    assert dialog.selected_package == "com.example.alpha"
    dialog.deleteLater()


def test_package_parameter_keeps_editable_field_and_exposes_picker() -> None:
    _app()
    requested: list[str] = []
    widget = ParamEditorWidget(
        {
            "name": "package_name",
            "type": "string",
            "label": "Package Name",
            "picker": "android_package",
        },
        "com.example.app",
        lambda _name, _value: None,
        android_package_launcher=requested.append,
    )

    choose = next(
        button
        for button in widget.findChildren(QPushButton)
        if button.text() == "Choose…"
    )
    choose.click()

    assert requested == ["package_name"]
    widget.deleteLater()
