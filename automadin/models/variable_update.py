"""Strict persistence contract for typed Update Variable commands."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from automadin.models.typed_value import ValueType
from automadin.models.value_reference import (
    BuiltinValueReference,
    InputValueReference,
    LiteralValueReference,
    ValueReference,
    ValueReferenceError,
    VariableValueReference,
    value_reference_from_dict,
)

_REFERENCE_TYPES = (
    LiteralValueReference,
    InputValueReference,
    VariableValueReference,
    BuiltinValueReference,
)


class VariableUpdateOperation(str, Enum):
    SET = "set"
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"
    APPEND = "append"
    PREPEND = "prepend"
    REPLACE = "replace"
    TOGGLE = "toggle"
    RESET = "reset"


class VariableUpdateError(ValueError):
    """An Update Variable command contract is malformed."""


def variable_update_allowed_operations(
    value_type: ValueType,
) -> frozenset[VariableUpdateOperation]:
    numeric = frozenset(
        {
            VariableUpdateOperation.SET,
            VariableUpdateOperation.ADD,
            VariableUpdateOperation.SUBTRACT,
            VariableUpdateOperation.MULTIPLY,
            VariableUpdateOperation.DIVIDE,
            VariableUpdateOperation.RESET,
        }
    )
    return {
        ValueType.INTEGER: numeric,
        ValueType.NUMBER: numeric,
        ValueType.DURATION_MS: numeric,
        ValueType.TEXT: frozenset(
            {
                VariableUpdateOperation.SET,
                VariableUpdateOperation.APPEND,
                VariableUpdateOperation.PREPEND,
                VariableUpdateOperation.REPLACE,
                VariableUpdateOperation.RESET,
            }
        ),
        ValueType.BOOLEAN: frozenset(
            {
                VariableUpdateOperation.SET,
                VariableUpdateOperation.TOGGLE,
                VariableUpdateOperation.RESET,
            }
        ),
    }[value_type]


def variable_update_allowed_source_types(
    target_type: ValueType,
    operation: VariableUpdateOperation,
) -> frozenset[ValueType]:
    if operation in {
        VariableUpdateOperation.MULTIPLY,
        VariableUpdateOperation.DIVIDE,
    }:
        return frozenset({ValueType.INTEGER, ValueType.NUMBER})
    if target_type is ValueType.NUMBER:
        return frozenset({ValueType.INTEGER, ValueType.NUMBER})
    return frozenset({target_type})


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise VariableUpdateError("Update Variable target ID must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise VariableUpdateError(
            "Update Variable target ID must be a UUID string."
        ) from exc


def _reference(value: object, *, label: str) -> ValueReference:
    try:
        return (
            value
            if isinstance(value, _REFERENCE_TYPES)
            else value_reference_from_dict(value)
        )
    except ValueReferenceError as exc:
        raise VariableUpdateError(f"Update Variable {label} is invalid: {exc}") from exc


@dataclass(frozen=True, slots=True)
class VariableUpdate:
    target_variable_id: str
    operation: VariableUpdateOperation
    source: ValueReference | None = None
    find: ValueReference | None = None
    replacement: ValueReference | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_variable_id",
            _canonical_uuid(self.target_variable_id),
        )
        try:
            operation = (
                self.operation
                if isinstance(self.operation, VariableUpdateOperation)
                else VariableUpdateOperation(self.operation)
            )
        except (TypeError, ValueError) as exc:
            raise VariableUpdateError(
                f"Unsupported Update Variable operation '{self.operation}'."
            ) from exc
        object.__setattr__(self, "operation", operation)
        if self.source is not None:
            object.__setattr__(self, "source", _reference(self.source, label="source"))
        if self.find is not None:
            object.__setattr__(self, "find", _reference(self.find, label="find value"))
        if self.replacement is not None:
            object.__setattr__(
                self,
                "replacement",
                _reference(self.replacement, label="replacement value"),
            )
        self._validate_operands()

    def _validate_operands(self) -> None:
        if self.operation in {
            VariableUpdateOperation.RESET,
            VariableUpdateOperation.TOGGLE,
        }:
            valid = self.source is None and self.find is None and self.replacement is None
        elif self.operation is VariableUpdateOperation.REPLACE:
            valid = (
                self.source is None
                and self.find is not None
                and self.replacement is not None
            )
        else:
            valid = (
                self.source is not None
                and self.find is None
                and self.replacement is None
            )
        if not valid:
            raise VariableUpdateError(
                f"Operation '{self.operation.value}' has invalid operands."
            )

    @classmethod
    def from_params(cls, params: object) -> VariableUpdate:
        if not isinstance(params, Mapping):
            raise VariableUpdateError("Update Variable params must be an object.")
        raw_operation = params.get("operation")
        try:
            operation = VariableUpdateOperation(raw_operation)
        except (TypeError, ValueError) as exc:
            raise VariableUpdateError(
                f"Unsupported Update Variable operation '{raw_operation}'."
            ) from exc
        expected = {"target_variable_id", "operation"}
        if operation is VariableUpdateOperation.REPLACE:
            expected.update({"find", "replacement"})
        elif operation not in {
            VariableUpdateOperation.RESET,
            VariableUpdateOperation.TOGGLE,
        }:
            expected.add("source")
        actual = set(params)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(str(key) for key in actual - expected)
            details: list[str] = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unexpected " + ", ".join(extra))
            raise VariableUpdateError(
                "Update Variable params have invalid fields: "
                + "; ".join(details)
                + "."
            )
        return cls(
            target_variable_id=params["target_variable_id"],  # type: ignore[arg-type]
            operation=operation,
            source=params.get("source"),  # type: ignore[arg-type]
            find=params.get("find"),  # type: ignore[arg-type]
            replacement=params.get("replacement"),  # type: ignore[arg-type]
        )

    def to_params(self) -> dict[str, object]:
        params: dict[str, object] = {
            "target_variable_id": self.target_variable_id,
            "operation": self.operation.value,
        }
        if self.source is not None:
            params["source"] = self.source.to_dict()
        if self.find is not None:
            params["find"] = self.find.to_dict()
        if self.replacement is not None:
            params["replacement"] = self.replacement.to_dict()
        return params


__all__ = [
    "VariableUpdate",
    "VariableUpdateError",
    "VariableUpdateOperation",
    "variable_update_allowed_operations",
    "variable_update_allowed_source_types",
]
