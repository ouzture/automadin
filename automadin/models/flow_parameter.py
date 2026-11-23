"""Typed immutable input parameters for flow execution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from automadin.models.typed_value import (
    TypedValue,
    ValueCoercionError,
    ValueType,
    canonical_typed_value_text,
    coerce_typed_value,
)

_PARAMETER_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
ParameterValue = TypedValue
FlowParameterType = ValueType


class FlowParameterError(ValueError):
    """A declaration or override cannot produce a valid parameter snapshot."""


def coerce_parameter_value(
    parameter_type: FlowParameterType,
    value: object,
    *,
    name: str,
) -> ParameterValue:
    try:
        return coerce_typed_value(
            parameter_type,
            value,
            subject=f"Flow parameter '{name}'",
        )
    except ValueCoercionError as exc:
        raise FlowParameterError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class FlowParameterDefinition:
    name: str
    type: FlowParameterType
    default: ParameterValue
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _PARAMETER_NAME_PATTERN.fullmatch(
            self.name
        ):
            raise FlowParameterError(
                "Flow parameter names must start with a letter and contain only "
                "letters, digits, and underscores."
            )
        if not isinstance(self.type, FlowParameterType):
            try:
                object.__setattr__(self, "type", FlowParameterType(self.type))
            except (TypeError, ValueError) as exc:
                raise FlowParameterError(
                    f"Flow parameter '{self.name}' has an unsupported type."
                ) from exc
        if not isinstance(self.description, str):
            raise FlowParameterError(
                f"Flow parameter '{self.name}' description must be text."
            )
        object.__setattr__(
            self,
            "default",
            coerce_parameter_value(self.type, self.default, name=self.name),
        )

    def to_dict(self) -> dict[str, ParameterValue]:
        return {
            "name": self.name,
            "type": self.type.value,
            "default": self.default,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: object) -> FlowParameterDefinition:
        if not isinstance(data, dict):
            raise FlowParameterError("Flow parameter declaration must be an object.")
        return cls(
            name=data.get("name"),  # type: ignore[arg-type]
            type=data.get("type"),  # type: ignore[arg-type]
            default=data.get("default"),  # type: ignore[arg-type]
            description=data.get("description", ""),  # type: ignore[arg-type]
        )


def validate_parameter_definitions(
    definitions: object,
) -> tuple[FlowParameterDefinition, ...]:
    if not isinstance(definitions, (list, tuple)):
        raise FlowParameterError("Flow parameters must be an array.")
    normalized: list[FlowParameterDefinition] = []
    names: set[str] = set()
    for raw_definition in definitions:
        definition = (
            raw_definition
            if isinstance(raw_definition, FlowParameterDefinition)
            else FlowParameterDefinition.from_dict(raw_definition)
        )
        if definition.name in names:
            raise FlowParameterError(
                f"Flow parameter '{definition.name}' is declared more than once."
            )
        names.add(definition.name)
        normalized.append(definition)
    return tuple(normalized)


def _validated_overrides(
    overrides: Mapping[str, Any] | object | None,
    *,
    layer: str,
) -> Mapping[str, Any]:
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise FlowParameterError(
            f"{layer.capitalize()} parameter overrides must be an object."
        )
    return overrides


def resolve_parameter_values(
    definitions: object,
    saved_overrides: Mapping[str, Any] | object | None = None,
    transient_overrides: Mapping[str, Any] | object | None = None,
) -> MappingProxyType[str, ParameterValue]:
    """Resolve declarations and override layers into an immutable mapping."""
    normalized = validate_parameter_definitions(definitions)
    by_name = {definition.name: definition for definition in normalized}
    values: dict[str, ParameterValue] = {
        definition.name: definition.default for definition in normalized
    }
    for layer_name, raw_overrides in (
        ("saved", saved_overrides),
        ("transient", transient_overrides),
    ):
        overrides = _validated_overrides(raw_overrides, layer=layer_name)
        for raw_name, raw_value in overrides.items():
            if not isinstance(raw_name, str) or raw_name not in by_name:
                raise FlowParameterError(
                    f"Unknown flow parameter '{raw_name}' in {layer_name} overrides."
                )
            definition = by_name[raw_name]
            values[raw_name] = coerce_parameter_value(
                definition.type,
                raw_value,
                name=raw_name,
            )
    return MappingProxyType(dict(values))


def canonical_parameter_text(value: ParameterValue) -> str:
    return canonical_typed_value_text(value)
