"""Readable, bounded values for execution diagnostics."""

from __future__ import annotations

import json

from automadin.models.flow_value import FlowValueCatalog
from automadin.models.typed_value import TypedValue, canonical_typed_value_text
from automadin.models.value_reference import (
    BuiltinValueReference,
    InputValueReference,
    LiteralValueReference,
    ValueReference,
    VariableValueReference,
)

_MAX_LOG_TEXT_CHARS = 160


def format_runtime_value(value: TypedValue) -> str:
    """Render one value visibly while keeping execution logs bounded."""
    if not isinstance(value, str):
        return canonical_typed_value_text(value)
    visible = value
    if len(visible) > _MAX_LOG_TEXT_CHARS:
        visible = visible[: _MAX_LOG_TEXT_CHARS - 1] + "…"
    return json.dumps(visible, ensure_ascii=False)


def value_reference_label(
    reference: ValueReference,
    catalog: FlowValueCatalog,
) -> str:
    """Return a human-readable source label for a resolved value reference."""
    if isinstance(reference, LiteralValueReference):
        return "Literal"
    if isinstance(reference, InputValueReference):
        definition = catalog.input_by_id(reference.id)
        name = definition.name if definition is not None else reference.id
        return f"Input '{name}'"
    if isinstance(reference, VariableValueReference):
        definition = catalog.variable_by_id(reference.id)
        name = definition.name if definition is not None else reference.id
        return f"Variable '{name}'"
    if isinstance(reference, BuiltinValueReference):
        return f"Built-in '{reference.name}'"
    return "Value"


def variable_display_name(
    variable_id: object,
    catalog: FlowValueCatalog | None,
) -> str:
    """Resolve a stored variable ID to its declaration name when possible."""
    if isinstance(variable_id, str) and catalog is not None:
        definition = catalog.variable_by_id(variable_id)
        if definition is not None:
            return definition.name
    return str(variable_id)


__all__ = [
    "format_runtime_value",
    "value_reference_label",
    "variable_display_name",
]
