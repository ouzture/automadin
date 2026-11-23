"""Typed Built-in scopes shared by preflight, runtime metadata, and editors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from automadin.models.typed_value import ValueType

if TYPE_CHECKING:
    from automadin.models.flow import FlowCommand

BUILTIN_VALUE_TYPES: Mapping[str, ValueType] = MappingProxyType(
    {
        "item": ValueType.TEXT,
        "index": ValueType.INTEGER,
        "element_index": ValueType.INTEGER,
        "element_count": ValueType.INTEGER,
        "_match_x": ValueType.INTEGER,
        "_match_y": ValueType.INTEGER,
        "_match_confidence": ValueType.NUMBER,
        "_matched_image": ValueType.TEXT,
    }
)

IMAGE_MATCH_BUILTIN_TYPES: Mapping[str, ValueType] = MappingProxyType(
    {
        name: BUILTIN_VALUE_TYPES[name]
        for name in (
            "_match_x",
            "_match_y",
            "_match_confidence",
            "_matched_image",
        )
    }
)


def extend_builtin_types(
    inherited: Mapping[str, ValueType],
    names: Sequence[str],
) -> dict[str, ValueType]:
    result = dict(inherited)
    for name in names:
        value_type = BUILTIN_VALUE_TYPES.get(name)
        if value_type is not None:
            result[name] = value_type
    return result


def builtin_types_for_step(
    commands: Sequence[FlowCommand],
    target_step_id: str,
    command_types: Mapping[str, Mapping[str, Any]],
    inherited: Mapping[str, ValueType] | None = None,
) -> dict[str, ValueType]:
    """Return the typed Built-ins visible before one command executes."""
    def _find(
        nested_commands: Sequence[FlowCommand],
        current: Mapping[str, ValueType],
    ) -> dict[str, ValueType] | None:
        for command in nested_commands:
            if command.step_id == target_step_id:
                return dict(current)
            definition = command_types.get(command.id, {})
            child_scope = extend_builtin_types(
                current,
                definition.get("childBuiltins", ()),
            )
            found = _find(command.children, child_scope)
            if found is not None:
                return found
            found = _find(command.else_children, current)
            if found is not None:
                return found
        return None

    return _find(commands, inherited or {}) or {}


__all__ = [
    "BUILTIN_VALUE_TYPES",
    "IMAGE_MATCH_BUILTIN_TYPES",
    "builtin_types_for_step",
    "extend_builtin_types",
]
