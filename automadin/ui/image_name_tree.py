from __future__ import annotations

from automadin.models.resource_path import (
    normalize_resource_path,
    resource_group_path,
    resource_leaf_name,
    resource_sort_key,
    resource_top_folder,
    split_resource_path,
)


def split_image_name(name: str) -> list[str]:
    """Split a template image name into display path parts."""
    return split_resource_path(name)


def image_group_parts(name: str) -> list[str]:
    return split_resource_path(resource_group_path(name))


def image_top_group(name: str) -> str:
    return resource_top_folder(name)


def top_level_image_groups(names: list[str]) -> list[str]:
    groups: dict[str, str] = {}
    for name in names:
        group = image_top_group(name)
        if group:
            groups.setdefault(group.casefold(), group)
    return sorted(groups.values(), key=str.casefold)


def image_matches_top_group(name: str, group: str) -> bool:
    if not group:
        return True
    return image_top_group(name).casefold() == group.casefold()


def image_leaf_name(name: str) -> str:
    return resource_leaf_name(name) or normalize_resource_path(name)


def image_sort_key(name: str) -> tuple[tuple[str, ...], str]:
    return resource_sort_key(name)
