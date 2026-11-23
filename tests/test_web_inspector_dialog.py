from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from automadin.capabilities.browser.interaction import (
    ElementMetadata,
    LocatorCandidate,
    PickedElement,
)
from automadin.models.locator import LocatorSpec
from automadin.ui.flow_editor.web_inspector_dialog import WebInspectorDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_picked_element_prefers_unique_candidate_and_emits_canonical_locator() -> None:
    _app()
    dialog = WebInspectorDialog(object())
    added: list[tuple[str, dict[str, object]]] = []
    dialog.use_requested.connect(
        lambda command_id, locator: added.append((command_id, locator))
    )
    picked = PickedElement(
        metadata=ElementMetadata(
            tag_name="button",
            role="button",
            accessible_name="Sign in",
        ),
        candidates=(
            LocatorCandidate(
                LocatorSpec.parse(
                    {
                        "strategy": "role",
                        "role": "button",
                        "name": "Sign in",
                        "exact": True,
                    }
                ),
                2,
            ),
            LocatorCandidate(
                LocatorSpec.parse({"strategy": "test_id", "value": "sign-in"}),
                1,
            ),
        ),
    )

    dialog._on_picked(picked)
    dialog._add_step()

    assert dialog._candidates.currentRow() == 1
    assert added == [
        (
            "click_element",
            {"platform": "browser", "strategy": "test_id", "value": "sign-in"},
        )
    ]
    dialog.deleteLater()


def test_ambiguous_inspection_is_visible() -> None:
    _app()
    dialog = WebInspectorDialog(object())

    dialog._on_inspected({"count": 3})

    assert "ambiguous" in dialog._status.text().casefold()
    assert "3" in dialog._status.text()
    dialog.deleteLater()


def test_selected_locator_can_create_a_wait_step() -> None:
    _app()
    dialog = WebInspectorDialog(object())
    used: list[tuple[str, dict[str, object]]] = []
    dialog.use_requested.connect(
        lambda command_id, locator: used.append((command_id, locator))
    )
    picked = PickedElement(
        metadata=ElementMetadata(tag_name="section"),
        candidates=(LocatorCandidate(LocatorSpec.parse("#results"), 1),),
    )
    dialog._on_picked(picked)
    action = dialog.findChild(QComboBox, "locatorAction")
    assert action is not None

    action.setCurrentIndex(action.findData("wait_for_selector"))
    dialog._add_step()

    assert used == [
        (
            "wait_for_selector",
            {"platform": "browser", "strategy": "css", "value": "#results"},
        )
    ]
    assert dialog.result() == dialog.DialogCode.Accepted
    dialog.deleteLater()


def test_collection_mode_prefers_ambiguous_locator_and_emits_loop_request() -> None:
    _app()
    dialog = WebInspectorDialog(object())
    added: list[tuple[str, dict[str, object]]] = []
    dialog.use_requested.connect(
        lambda command_id, locator: added.append((command_id, locator))
    )
    picked = PickedElement(
        metadata=ElementMetadata(tag_name="article"),
        candidates=(
            LocatorCandidate(LocatorSpec.parse("#daily-card"), 1),
            LocatorCandidate(LocatorSpec.parse(".daily-card"), 8),
        ),
    )
    dialog._on_picked(picked)
    mode = dialog.findChild(QComboBox, "selectorMode")
    assert mode is not None

    mode.setCurrentIndex(mode.findData("collection"))
    dialog._add_step()

    assert dialog._candidates.currentRow() == 1
    assert "class collection" in dialog._candidates.currentItem().text()
    assert added == [
        (
            "for_each_browser_element",
            {"platform": "browser", "strategy": "css", "value": ".daily-card"},
        )
    ]
    dialog.deleteLater()
