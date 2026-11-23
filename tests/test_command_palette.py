from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from automadin.execution.handlers.registry import load_command_types
from automadin.ui.flow_editor.command_discovery import (
    COMMAND_DISCOVERY,
    validate_command_discovery,
)
from automadin.ui.flow_editor.palette import CommandPaletteDialog


@pytest.fixture
def palette(application: QApplication) -> CommandPaletteDialog:
    dialog = CommandPaletteDialog(load_command_types())
    yield dialog
    dialog.deleteLater()


def _visible_ids(dialog: CommandPaletteDialog) -> set[str]:
    return {definition["id"] for _item, definition in dialog._tiles}


def _select_purpose(dialog: CommandPaletteDialog, purpose: str) -> None:
    for row in range(dialog._purposes.count()):
        item = dialog._purposes.item(row)
        if item.data(256) == purpose:
            dialog._purposes.setCurrentItem(item)
            return
    raise AssertionError(f"Purpose not found: {purpose}")


def test_discovery_metadata_covers_every_runtime_command() -> None:
    command_types = load_command_types()

    validate_command_discovery(command_types)

    assert set(COMMAND_DISCOVERY) == set(command_types)


def test_target_filter_distinguishes_platform_and_general_commands(
    palette: CommandPaletteDialog,
) -> None:
    palette._set_target("windows")
    windows_ids = _visible_ids(palette)
    assert "win_click" in windows_ids
    assert "click_image" in windows_ids
    assert "wait" in windows_ids
    assert "click_element" not in windows_ids

    palette._set_target("general")
    general_ids = _visible_ids(palette)
    assert "wait" in general_ids
    assert "run_flow" in general_ids
    assert "win_click" not in general_ids
    assert "click_image" not in general_ids


def test_purpose_filter_is_independent_from_target_filter(
    palette: CommandPaletteDialog,
) -> None:
    palette._set_target("browser")
    _select_purpose(palette, "navigation")

    visible_ids = _visible_ids(palette)

    assert "navigate_to" in visible_ids
    assert "wait_for_navigation" in visible_ids
    assert "click_element" not in visible_ids
    assert "win_click" not in visible_ids


def test_search_uses_plain_language_aliases_and_multiple_terms(
    palette: CommandPaletteDialog,
) -> None:
    palette._search.setText("open website")

    visible_ids = _visible_ids(palette)

    assert "navigate_to" in visible_ids
    assert "android_launch_app" not in visible_ids


def test_selection_explains_applicability_and_hides_technical_name_from_rows(
    palette: CommandPaletteDialog,
) -> None:
    palette._search.setText("click element")

    assert palette._current_command_id() == "click_element"
    assert palette._detail_target.text() == "BROWSER"
    assert "Browser automation only" in palette._detail_applicability.text()
    assert palette._detail_id.text() == "Technical name: click_element"

    command_item, _definition = palette._tiles[0]
    assert "click_element" not in command_item.text(0)


def test_activating_command_accepts_the_dialog(
    palette: CommandPaletteDialog,
) -> None:
    palette._search.setText("random wait")
    item, _definition = palette._tiles[0]

    palette._activate_item(item, 0)

    assert palette.get_selected() == "random_wait"
    assert palette.result() == QDialog.DialogCode.Accepted
