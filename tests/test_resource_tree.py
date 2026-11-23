from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.ui.resource_tree import GROUP_PATH_ROLE, ResourceTreeWidget


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_resource_tree_builds_collapsible_categories_and_uncategorized() -> None:
    _app()
    tree = ResourceTreeWidget()
    tree.set_resources(
        [
            ("flow:collect", "Mobile App/Shop/Collect Rewards"),
            ("flow:create", "Mobile App/Accounts/Create"),
            ("flow:loose", "Loose Flow"),
        ]
    )

    assert tree.topLevelItemCount() == 2
    assert tree.topLevelItem(0).text(0) == "No folder"
    assert tree.topLevelItem(0).data(0, GROUP_PATH_ROLE) == ""
    assert tree.topLevelItem(1).text(0) == "Mobile App"
    assert tree.topLevelItem(1).childCount() == 2
    assert tree.selected_resource_id() == "flow:loose"


def test_resource_tree_search_keeps_identity_separate_from_display_path() -> None:
    _app()
    tree = ResourceTreeWidget()
    tree.set_resources(
        [
            ("stable-1", "Mobile App/Shop/Collect Rewards"),
            ("stable-2", "Browser/Auth/Login"),
        ],
        query="collect",
    )

    assert tree.topLevelItemCount() == 1
    assert tree.topLevelItem(0).text(0) == "Mobile App"
    assert tree.selected_resource_id() == "stable-1"
    assert tree.selected_resource_path() == "Mobile App/Shop/Collect Rewards"


def test_resource_tree_merges_folder_casing_without_merging_resources() -> None:
    _app()
    tree = ResourceTreeWidget()
    tree.set_resources(
        [("one", "Games/Shop/One"), ("two", "games/Rewards/Two")]
    )

    assert tree.topLevelItemCount() == 1
    assert tree.topLevelItem(0).text(0) == "Games"
    assert tree.topLevelItem(0).childCount() == 2


def test_resource_tree_uses_workspace_folder_spelling_for_legacy_variants() -> None:
    _app()
    tree = ResourceTreeWidget()
    tree.set_resources(
        [("phone", "iom/Phone"), ("fire", "IOM/Fire Bomb")],
        canonical_folders=["IOM"],
    )

    assert tree.topLevelItemCount() == 1
    assert tree.topLevelItem(0).text(0) == "IOM"
    assert tree.topLevelItem(0).childCount() == 2


def test_resource_tree_displays_legacy_names_without_new_write_validation() -> None:
    _app()
    tree = ResourceTreeWidget()

    tree.set_resources([("legacy", "Old/../Very old flow")])

    assert tree.topLevelItemCount() == 1
    assert tree.selected_resource_id() == "legacy"


def test_resource_tree_shows_dependency_details_under_resource_name() -> None:
    _app()
    tree = ResourceTreeWidget()

    tree.set_resources(
        [("child-id", "Flows/Child")],
        details={"child-id": "Used by flows: rewards_complete"},
    )

    item = tree.topLevelItem(0).child(0)
    assert item.text(0) == "Child\nUsed by flows: rewards_complete"
    assert "rewards_complete" in item.toolTip(0)
