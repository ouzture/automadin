"""Pure editor state rules for the Update Variable command."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from automadin.models.flow_value import FlowValueCatalog
from automadin.models.typed_value import ValueType
from automadin.models.variable_update import (
    VariableUpdateOperation,
    variable_update_allowed_operations,
    variable_update_allowed_source_types,
)

_OPERAND_NAMES = frozenset({"source", "find", "replacement"})
_SOURCE_OPERATIONS = frozenset(
    operation
    for operation in VariableUpdateOperation
    if operation
    not in {
        VariableUpdateOperation.REPLACE,
        VariableUpdateOperation.TOGGLE,
        VariableUpdateOperation.RESET,
    }
)


def _operation(value: object) -> VariableUpdateOperation | None:
    try:
        return VariableUpdateOperation(value)
    except (TypeError, ValueError):
        return None


def variable_update_operand_names(
    operation: VariableUpdateOperation | None,
) -> frozenset[str]:
    if operation is VariableUpdateOperation.REPLACE:
        return frozenset({"find", "replacement"})
    if operation in _SOURCE_OPERATIONS:
        return frozenset({"source"})
    if operation in {VariableUpdateOperation.TOGGLE, VariableUpdateOperation.RESET}:
        return frozenset()
    return _OPERAND_NAMES


def normalize_variable_update_editor_change(
    params: Mapping[str, Any],
    name: str,
    value: Any,
    catalog: FlowValueCatalog,
) -> dict[str, Any]:
    """Apply a target/operation edit without retaining obsolete operands."""
    result = dict(params)
    result[name] = value
    if name == "target_variable_id":
        for operand_name in _OPERAND_NAMES:
            result.pop(operand_name, None)
        target = catalog.variable_by_id(value) if isinstance(value, str) else None
        operation = _operation(result.get("operation"))
        if target is not None and (
            operation is None
            or operation not in variable_update_allowed_operations(target.type)
        ):
            result["operation"] = VariableUpdateOperation.SET.value
        return result
    if name == "operation":
        visible = variable_update_operand_names(_operation(value))
        for operand_name in _OPERAND_NAMES - visible:
            result.pop(operand_name, None)
    return result


def configure_variable_update_param_defs(
    param_defs: Sequence[Mapping[str, Any]],
    params: Mapping[str, Any],
    catalog: FlowValueCatalog,
) -> list[dict[str, Any]]:
    """Filter operands and attach target-aware operation/reference choices."""
    operation = _operation(params.get("operation"))
    visible_operands = variable_update_operand_names(operation)
    target_id = params.get("target_variable_id")
    target = catalog.variable_by_id(target_id) if isinstance(target_id, str) else None
    configured: list[dict[str, Any]] = []
    for raw_definition in param_defs:
        definition = dict(raw_definition)
        name = definition.get("name")
        # Timing is deliberately kept as a simple integer editor for this
        # action.  The typed source picker is only meaningful for the
        # operation operand; turning the shared timing fields into additional
        # value pickers makes the compact update editor needlessly noisy.
        if name in {"pause_ms", "min_wait_time"}:
            definition["type"] = "int"
        if name in _OPERAND_NAMES and name not in visible_operands:
            continue
        if name == "operation" and target is not None:
            allowed = variable_update_allowed_operations(target.type)
            options = [item.value for item in VariableUpdateOperation if item in allowed]
            current = params.get("operation")
            if isinstance(current, str) and current not in options:
                options.append(current)
            definition["options"] = options
        if name in {"find", "replacement"}:
            definition["allowed_value_types"] = [ValueType.TEXT.value]
        elif name == "source" and target is not None and operation is not None:
            definition["allowed_value_types"] = [
                item.value
                for item in ValueType
                if item in variable_update_allowed_source_types(target.type, operation)
            ]
        configured.append(definition)
    return configured


__all__ = [
    "configure_variable_update_param_defs",
    "normalize_variable_update_editor_change",
    "variable_update_operand_names",
]
