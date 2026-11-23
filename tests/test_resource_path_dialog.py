from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.ui.resource_path_dialog import ResourcePathDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_suggested_import_name_does_not_bypass_collision_check() -> None:
    _app()
    dialog = ResourcePathDialog(
        title="Import Image",
        item_label="Image",
        current_path="Shared/Button",
        ignore_path="",
        existing_paths=["Shared/Button"],
    )

    candidate, error = dialog._candidate_path()

    assert candidate == "Shared/Button"
    assert error == "'Shared/Button' already exists."


def test_organizing_existing_item_ignores_its_own_path() -> None:
    _app()
    dialog = ResourcePathDialog(
        title="Organize Image",
        item_label="Image",
        current_path="Shared/Button",
        existing_paths=["Shared/Button"],
    )

    candidate, error = dialog._candidate_path()

    assert candidate == "Shared/Button"
    assert error is None


def test_explicit_overwrite_mode_can_return_an_existing_path() -> None:
    _app()
    dialog = ResourcePathDialog(
        title="Import Flow",
        item_label="Flow",
        current_path="Shared/Login",
        ignore_path="",
        allow_existing=True,
        existing_paths=["Shared/Login"],
    )

    candidate, error = dialog._candidate_path()

    assert candidate == "Shared/Login"
    assert error is None


def test_dialog_prefills_and_saves_with_workspace_folder_spelling() -> None:
    _app()
    dialog = ResourcePathDialog(
        title="Organize Image",
        item_label="Image",
        current_path="iom/Phone",
        known_paths=["iom/click_fire_bomb"],
        known_folders=["IOM"],
        existing_paths=["iom/Phone"],
    )

    candidate, error = dialog._candidate_path()

    assert dialog._folder_combo.currentText() == "IOM"
    assert dialog._folder_combo.currentIndex() == dialog._folder_combo.findText("IOM")
    assert candidate == "IOM/Phone"
    assert error is None
