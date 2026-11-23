"""Shared virtual-folder semantics for named Automadin resources.

Flows, images, and visual rules are still stored under their existing qualified
names. A slash is a portable hierarchy separator, not a filesystem path. The UI
presents those segments as folders and keeps the separator out of normal editing.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


def split_resource_path(value: Any) -> list[str]:
    """Return compact, trimmed parts from a slash- or backslash-delimited value."""
    if value is None:
        return []
    return [
        part.strip()
        for part in str(value).replace("\\", "/").split("/")
        if part.strip()
    ]


def normalize_resource_path(value: Any) -> str:
    """Return the canonical slash-delimited representation used for new writes."""
    return "/".join(split_resource_path(value))


def resource_path_error(value: Any) -> str | None:
    """Return a user-facing validation error for a new virtual resource path."""
    normalized = normalize_resource_path(value)
    if not normalized:
        return "A name is required."
    if len(normalized) > 500:
        return "The complete folder path must be 500 characters or fewer."
    for part in split_resource_path(normalized):
        if part in {".", ".."}:
            return "Folder and item names cannot be '.' or '..'."
        if len(part) > 120:
            return "Each folder and item name must be 120 characters or fewer."
        if any(ord(char) < 32 for char in part):
            return "Names cannot contain control characters."
    return None


def normalize_resource_group(value: Any) -> str:
    """Normalize a virtual folder path that has no leaf item."""
    normalized = normalize_resource_path(value)
    error = resource_path_error(normalized) if normalized else None
    if error:
        raise ValueError(error)
    return normalized


def resource_leaf_name(value: Any) -> str:
    parts = split_resource_path(value)
    return parts[-1] if parts else ""


def resource_group_path(value: Any) -> str:
    parts = split_resource_path(value)
    return "/".join(parts[:-1])


def resource_top_folder(value: Any) -> str:
    parts = split_resource_path(value)
    return parts[0] if len(parts) > 1 else ""


def resource_subfolder(value: Any) -> str:
    parts = split_resource_path(value)
    return "/".join(parts[1:-1]) if len(parts) > 2 else ""


def join_resource_path(group_path: Any, leaf_name: Any) -> str:
    """Join a virtual folder path and leaf, validating the resulting path."""
    leaf_parts = split_resource_path(leaf_name)
    if len(leaf_parts) != 1:
        raise ValueError("The item name cannot contain '/'. Choose its folder separately.")
    result = normalize_resource_path(
        "/".join(part for part in (normalize_resource_path(group_path), leaf_parts[0]) if part)
    )
    error = resource_path_error(result)
    if error:
        raise ValueError(error)
    return result


def resource_top_folders(paths: Iterable[str]) -> list[str]:
    folders = {resource_top_folder(path) for path in paths}
    return sorted((folder for folder in folders if folder), key=str.casefold)


class WorkspaceFolderIndex:
    """One case-insensitive, case-preserving view of workspace folders.

    Run setup folders are explicit user organization metadata, while folders in
    flow, image, and visual-rule names are implicit observations. Explicit
    spellings therefore win; within either source, the most common spelling
    wins with a stable lexical tie-break. Resolving each prefix independently
    keeps nested paths consistent with their canonical parent.
    """

    def __init__(
        self,
        *,
        explicit_groups: Iterable[str] = (),
        resource_paths: Iterable[str] = (),
        candidate_groups: Iterable[str] = (),
    ) -> None:
        explicit_variants: dict[str, Counter[str]] = defaultdict(Counter)
        implicit_variants: dict[str, Counter[str]] = defaultdict(Counter)

        def observe(parts: list[str], target: dict[str, Counter[str]]) -> None:
            folded_parts: list[str] = []
            for part in parts:
                folded_parts.append(part.casefold())
                target["/".join(folded_parts)][part] += 1

        for group in explicit_groups:
            observe(split_resource_path(group), explicit_variants)
        for path in resource_paths:
            observe(split_resource_path(path)[:-1], implicit_variants)
        for group in candidate_groups:
            parts = split_resource_path(group)
            # A newly typed nested folder should inherit established parent
            # casing, while its complete path is an explicit user choice.
            observe(parts, implicit_variants)
            if parts:
                key = "/".join(part.casefold() for part in parts)
                explicit_variants[key][parts[-1]] += 1

        keys = set(explicit_variants) | set(implicit_variants)
        self._groups: dict[str, str] = {}
        for key in sorted(keys, key=lambda value: (value.count("/"), value)):
            variants = explicit_variants.get(key) or implicit_variants.get(key)
            if not variants:
                continue
            # All variants have the same casefolded identity. Exact lexical
            # order makes ties deterministic and naturally favors conventional
            # leading capitals over an all-lowercase legacy spelling.
            segment = min(variants, key=lambda value: (-variants[value], value))
            parent_key, _, _ = key.rpartition("/")
            parent = self._groups.get(parent_key, "")
            self._groups[key] = "/".join(
                part for part in (parent, segment) if part
            )

    @property
    def folders(self) -> list[str]:
        return sorted(
            self._groups.values(),
            key=lambda group: tuple(
                part.casefold() for part in split_resource_path(group)
            ),
        )

    def _canonicalize(self, value: Any, *, validate: bool) -> str:
        normalized = (
            normalize_resource_group(value)
            if validate
            else normalize_resource_path(value)
        )
        if not normalized:
            return ""
        result: list[str] = []
        input_parts = split_resource_path(normalized)
        for index, part in enumerate(input_parts, start=1):
            input_prefix = "/".join(input_parts[:index])
            established = self._groups.get(input_prefix.casefold())
            if established:
                result = split_resource_path(established)
            else:
                result.append(part)
        return "/".join(result)

    def canonicalize(self, value: Any) -> str:
        """Resolve a new/edited folder path and validate its segments."""
        return self._canonicalize(value, validate=True)

    def canonicalize_existing(self, value: Any) -> str:
        """Resolve display casing without rejecting readable legacy segments."""
        return self._canonicalize(value, validate=False)

    def display_path(self, value: Any) -> str:
        """Return a resource path with canonical folder casing and its leaf intact."""
        normalized = normalize_resource_path(value)
        leaf = resource_leaf_name(normalized)
        group = self.canonicalize_existing(resource_group_path(normalized))
        return normalize_resource_path(
            "/".join(part for part in (group, leaf) if part)
        )


def resource_group_paths(paths: Iterable[str]) -> list[str]:
    """Return every materialized folder prefix with deterministic casing."""
    return WorkspaceFolderIndex(resource_paths=paths).folders


def canonicalize_resource_group(
    value: Any,
    known_paths: Iterable[str],
    *,
    preferred_groups: Iterable[str] = (),
) -> str:
    """Reuse one established folder spelling while retaining new segments.

    ``preferred_groups`` represents explicit organization metadata and takes
    precedence over casing inferred from resource identities.
    """
    return WorkspaceFolderIndex(
        explicit_groups=preferred_groups,
        resource_paths=known_paths,
    ).canonicalize(value)


def resource_sort_key(value: Any) -> tuple[tuple[str, ...], str]:
    normalized = normalize_resource_path(value)
    return tuple(part.casefold() for part in split_resource_path(normalized)), normalized


def resource_display_label(value: Any) -> str:
    """Return a compact combo-box label while keeping the full value as item data."""
    normalized = normalize_resource_path(value)
    leaf = resource_leaf_name(normalized)
    group = resource_group_path(normalized)
    return f"{leaf}  —  {group}" if group else leaf


def find_resource_collision(
    candidate: Any,
    existing_paths: Iterable[str],
    *,
    ignore_path: Any = "",
) -> str | None:
    """Find a normalized, case-insensitive collision among existing paths."""
    target = normalize_resource_path(candidate).casefold()
    ignored = normalize_resource_path(ignore_path).casefold()
    for existing in existing_paths:
        normalized = normalize_resource_path(existing)
        folded = normalized.casefold()
        if folded == ignored:
            continue
        if folded == target:
            return str(existing)
    return None


def safe_export_stem(value: Any) -> str:
    """Return a Windows-safe filename stem for a qualified resource name."""
    normalized = normalize_resource_path(value)
    stem = " - ".join(split_resource_path(normalized)) or "automadin-resource"
    for char in '<>:"/\\|?*':
        stem = stem.replace(char, "_")
    stem = stem.rstrip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    if stem.split(".", 1)[0].upper() in reserved:
        stem = f"_{stem}"
    max_length = 120
    if len(stem) > max_length:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
        stem = f"{stem[: max_length - len(digest) - 3].rstrip()} - {digest}"
    return stem or "automadin-resource"


@dataclass(frozen=True)
class ResourcePath:
    """Parsed view of a stored resource path."""

    storage_name: str

    @classmethod
    def parse(cls, value: Any) -> ResourcePath:
        return cls(normalize_resource_path(value))

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(split_resource_path(self.storage_name))

    @property
    def leaf(self) -> str:
        return resource_leaf_name(self.storage_name)

    @property
    def group(self) -> str:
        return resource_group_path(self.storage_name)

    @property
    def top_folder(self) -> str:
        return resource_top_folder(self.storage_name)

    @property
    def subfolder(self) -> str:
        return resource_subfolder(self.storage_name)
