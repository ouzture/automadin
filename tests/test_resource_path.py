from __future__ import annotations

import pytest

from automadin.models.resource_path import (
    ResourcePath,
    WorkspaceFolderIndex,
    canonicalize_resource_group,
    find_resource_collision,
    join_resource_path,
    normalize_resource_path,
    resource_group_paths,
    resource_path_error,
    safe_export_stem,
)


def test_resource_path_normalizes_existing_slash_convention() -> None:
    assert normalize_resource_path(r" / Mobile App\\ Shop // Collect Rewards / ") == (
        "Mobile App/Shop/Collect Rewards"
    )
    path = ResourcePath.parse("Mobile App/Shop/Collect Rewards")
    assert path.top_folder == "Mobile App"
    assert path.subfolder == "Shop"
    assert path.group == "Mobile App/Shop"
    assert path.leaf == "Collect Rewards"


def test_resource_path_supports_unfiled_and_top_folder_items() -> None:
    assert ResourcePath.parse("Loose flow").top_folder == ""
    folder_item = ResourcePath.parse("Mobile App/Daily run")
    assert folder_item.top_folder == "Mobile App"
    assert folder_item.subfolder == ""


def test_resource_folders_include_intermediate_folders() -> None:
    assert resource_group_paths(
        ["Mobile App/Shop/Collect", "Mobile App/Accounts/Create", "Loose"]
    ) == ["Mobile App", "Mobile App/Accounts", "Mobile App/Shop"]


def test_folder_suggestions_merge_case_and_reuse_established_casing() -> None:
    paths = ["Games/Shop/Collect", "games/Rewards/Open"]
    assert resource_group_paths(paths) == ["Games", "Games/Rewards", "Games/Shop"]
    assert canonicalize_resource_group("games/daily", paths) == "Games/daily"


def test_workspace_folder_index_prefers_explicit_run_folder_spelling() -> None:
    explicit = ["IOM", "IOM", "IOM", "iom", "RSL"]
    implicit = [
        "iom/click_fire_bomb",
        *(f"iom/images/button-{index}" for index in range(20)),
        *(f"rsl/images/button-{index}" for index in range(20)),
    ]

    index = WorkspaceFolderIndex(
        explicit_groups=explicit,
        resource_paths=implicit,
    )
    reversed_index = WorkspaceFolderIndex(
        explicit_groups=reversed(explicit),
        resource_paths=reversed(implicit),
    )

    assert index.canonicalize_existing("iom/Phone") == "IOM/Phone"
    assert index.canonicalize_existing("rsl/Daily") == "RSL/Daily"
    assert reversed_index.folders == index.folders


def test_typed_nested_run_folder_inherits_canonical_parent_casing() -> None:
    index = WorkspaceFolderIndex(
        resource_paths=["Mobile/Existing/Login"],
        candidate_groups=["mobile/New"],
    )
    acronym = WorkspaceFolderIndex(
        resource_paths=["iom/click_fire_bomb"],
        candidate_groups=["IOM"],
    )

    assert index.canonicalize("mobile/New") == "Mobile/New"
    assert acronym.canonicalize("iom") == "IOM"


def test_join_rejects_path_separators_in_leaf() -> None:
    assert join_resource_path("Mobile App/Shop", "Collect") == (
        "Mobile App/Shop/Collect"
    )
    with pytest.raises(ValueError, match="item name"):
        join_resource_path("Mobile App", "Shop/Collect")


def test_new_resource_paths_reject_navigation_and_control_segments() -> None:
    assert resource_path_error("Folder/../Flow") is not None
    assert resource_path_error("Folder/Bad\x01Name") is not None
    assert resource_path_error("Folder/Flow") is None


def test_collision_check_is_normalized_and_case_insensitive() -> None:
    existing = ["Mobile App/Shop/Collect"]
    assert find_resource_collision("mobile app\\shop//collect", existing) == existing[0]
    assert (
        find_resource_collision(
            "mobile app/shop/collect",
            existing,
            ignore_path="Mobile App/Shop/Collect",
        )
        is None
    )


def test_safe_export_stem_never_treats_virtual_path_as_directory() -> None:
    assert safe_export_stem("Mobile App/Shop/Collect") == (
        "Mobile App - Shop - Collect"
    )
    assert safe_export_stem("CON") == "_CON"
    long_stem = safe_export_stem("Folder/" + "x" * 300)
    assert len(long_stem) <= 120
    assert safe_export_stem("Folder/" + "x" * 300) == long_stem
