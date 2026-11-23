"""Stable child-flow bindings for version-1 Run Flow commands."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from automadin.models.value_reference import (
    BuiltinValueReference,
    InputValueReference,
    LiteralValueReference,
    ValueReference,
    ValueReferenceError,
    VariableValueReference,
    value_reference_from_dict,
)

_BINDING_FIELDS = frozenset({"flow_id", "input_mappings", "output_mappings"})
_REFERENCE_TYPES = (
    LiteralValueReference,
    InputValueReference,
    VariableValueReference,
    BuiltinValueReference,
)


class RunFlowBindingError(ValueError):
    """A version-1 Run Flow binding is malformed."""


def _canonical_uuid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise RunFlowBindingError(f"{label} must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise RunFlowBindingError(f"{label} must be a UUID string.") from exc


def _require_exact_fields(data: Mapping[object, object]) -> None:
    actual = set(data)
    if actual == _BINDING_FIELDS:
        return
    missing = sorted(_BINDING_FIELDS - actual)
    extra = sorted(str(key) for key in actual - _BINDING_FIELDS)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("unexpected " + ", ".join(extra))
    raise RunFlowBindingError(
        "Run Flow binding has invalid fields: " + "; ".join(details) + "."
    )


def _normalize_input_mappings(raw: object) -> Mapping[str, ValueReference]:
    if not isinstance(raw, Mapping):
        raise RunFlowBindingError("Run Flow input mappings must be an object.")
    normalized: dict[str, ValueReference] = {}
    for raw_id, raw_reference in raw.items():
        input_id = _canonical_uuid(raw_id, label="Child Input mapping ID")
        if input_id in normalized:
            raise RunFlowBindingError(
                f"Run Flow input mappings contain duplicate ID '{input_id}'."
            )
        try:
            normalized[input_id] = (
                raw_reference
                if isinstance(raw_reference, _REFERENCE_TYPES)
                else value_reference_from_dict(raw_reference)
            )
        except ValueReferenceError as exc:
            raise RunFlowBindingError(
                f"Child Input mapping '{input_id}' is invalid: {exc}"
            ) from exc
    return MappingProxyType(normalized)


def _normalize_output_mappings(raw: object) -> Mapping[str, str]:
    if not isinstance(raw, Mapping):
        raise RunFlowBindingError("Run Flow output mappings must be an object.")
    normalized: dict[str, str] = {}
    for raw_output_id, raw_parent_variable_id in raw.items():
        output_id = _canonical_uuid(raw_output_id, label="Child Output mapping ID")
        if output_id in normalized:
            raise RunFlowBindingError(
                f"Run Flow output mappings contain duplicate ID '{output_id}'."
            )
        normalized[output_id] = _canonical_uuid(
            raw_parent_variable_id,
            label=f"Parent Variable ID for child Output '{output_id}'",
        )
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class RunFlowBinding:
    """Rename-safe child identity plus stable Input and Output mappings."""

    flow_id: str
    input_mappings: Mapping[str, ValueReference]
    output_mappings: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "flow_id",
            _canonical_uuid(self.flow_id, label="Child Flow ID"),
        )
        object.__setattr__(
            self,
            "input_mappings",
            _normalize_input_mappings(self.input_mappings),
        )
        object.__setattr__(
            self,
            "output_mappings",
            _normalize_output_mappings(self.output_mappings),
        )

    @classmethod
    def from_params(cls, params: object) -> RunFlowBinding:
        if not isinstance(params, Mapping):
            raise RunFlowBindingError("Run Flow binding must be an object.")
        _require_exact_fields(params)
        return cls(
            flow_id=params["flow_id"],  # type: ignore[arg-type]
            input_mappings=params["input_mappings"],  # type: ignore[arg-type]
            output_mappings=params["output_mappings"],  # type: ignore[arg-type]
        )

    def to_params(self) -> dict[str, object]:
        return {
            "flow_id": self.flow_id,
            "input_mappings": {
                input_id: self.input_mappings[input_id].to_dict()
                for input_id in sorted(self.input_mappings)
            },
            "output_mappings": {
                output_id: self.output_mappings[output_id]
                for output_id in sorted(self.output_mappings)
            },
        }


__all__ = ["RunFlowBinding", "RunFlowBindingError"]
