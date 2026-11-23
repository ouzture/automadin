"""Stable declarations for the Inputs and Variables flow value model."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from automadin.models.typed_value import (
    TypedValue,
    ValueCoercionError,
    ValueType,
    coerce_typed_value,
)

FlowValueType = ValueType
FlowValue = TypedValue

_VALUE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MISSING_INPUT = object()
_DeclarationT = TypeVar("_DeclarationT")


class FlowValueDeclarationError(ValueError):
    """A flow value declaration is malformed or internally inconsistent."""


def _normalize_uuid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise FlowValueDeclarationError(f"{label} ID must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise FlowValueDeclarationError(f"{label} ID must be a UUID string.") from exc


def _validate_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _VALUE_NAME_PATTERN.fullmatch(value):
        raise FlowValueDeclarationError(
            f"{label} names must start with a letter and contain only letters, "
            "digits, and underscores."
        )
    return value


def _validate_description(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise FlowValueDeclarationError(f"{label} description must be text.")
    return value


def _normalize_type(value: object, *, label: str) -> FlowValueType:
    if isinstance(value, FlowValueType):
        return value
    try:
        return FlowValueType(value)
    except (TypeError, ValueError) as exc:
        raise FlowValueDeclarationError(f"{label} has an unsupported type.") from exc


def _coerce_value(
    value_type: FlowValueType,
    value: object,
    *,
    kind: str,
    name: str,
) -> FlowValue:
    try:
        return coerce_typed_value(
            value_type,
            value,
            subject=f"{kind} '{name}'",
        )
    except ValueCoercionError as exc:
        raise FlowValueDeclarationError(str(exc)) from exc


def _new_uuid() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class FlowInputDefinition:
    """An immutable value supplied when a flow invocation starts."""

    name: str
    type: FlowValueType
    default: FlowValue | object = field(default=_MISSING_INPUT, repr=False)
    description: str = ""
    id: str = field(default_factory=_new_uuid)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_name(self.name, label="Input"))
        object.__setattr__(
            self,
            "type",
            _normalize_type(self.type, label=f"Input '{self.name}'"),
        )
        object.__setattr__(
            self,
            "description",
            _validate_description(self.description, label=f"Input '{self.name}'"),
        )
        object.__setattr__(self, "id", _normalize_uuid(self.id, label="Input"))
        if self.default is not _MISSING_INPUT:
            object.__setattr__(
                self,
                "default",
                _coerce_value(
                    self.type,
                    self.default,
                    kind="Input",
                    name=self.name,
                ),
            )

    @property
    def required(self) -> bool:
        return self.default is _MISSING_INPUT

    def to_dict(self) -> dict[str, FlowValue]:
        data: dict[str, FlowValue] = {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "description": self.description,
        }
        if not self.required:
            data["default"] = self.default  # type: ignore[assignment]
        return data

    @classmethod
    def from_dict(cls, data: object) -> FlowInputDefinition:
        if not isinstance(data, Mapping):
            raise FlowValueDeclarationError("Input declaration must be an object.")
        return cls(
            id=data.get("id"),  # type: ignore[arg-type]
            name=data.get("name"),  # type: ignore[arg-type]
            type=data.get("type"),  # type: ignore[arg-type]
            default=data["default"] if "default" in data else _MISSING_INPUT,
            description=data.get("description", ""),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class FlowVariableDefinition:
    """A typed mutable value private to one running flow invocation."""

    name: str
    type: FlowValueType
    initial: FlowValue
    description: str = ""
    id: str = field(default_factory=_new_uuid)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_name(self.name, label="Variable"))
        object.__setattr__(
            self,
            "type",
            _normalize_type(self.type, label=f"Variable '{self.name}'"),
        )
        object.__setattr__(
            self,
            "initial",
            _coerce_value(
                self.type,
                self.initial,
                kind="Variable",
                name=self.name,
            ),
        )
        object.__setattr__(
            self,
            "description",
            _validate_description(self.description, label=f"Variable '{self.name}'"),
        )
        object.__setattr__(self, "id", _normalize_uuid(self.id, label="Variable"))

    def to_dict(self) -> dict[str, FlowValue]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "initial": self.initial,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: object) -> FlowVariableDefinition:
        if not isinstance(data, Mapping):
            raise FlowValueDeclarationError("Variable declaration must be an object.")
        return cls(
            id=data.get("id"),  # type: ignore[arg-type]
            name=data.get("name"),  # type: ignore[arg-type]
            type=data.get("type"),  # type: ignore[arg-type]
            initial=data.get("initial"),  # type: ignore[arg-type]
            description=data.get("description", ""),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class FlowOutputDefinition:
    """A stable child-flow contract backed by one declared Variable."""

    name: str
    type: FlowValueType
    source_variable_id: str
    description: str = ""
    id: str = field(default_factory=_new_uuid)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_name(self.name, label="Output"))
        object.__setattr__(
            self,
            "type",
            _normalize_type(self.type, label=f"Output '{self.name}'"),
        )
        object.__setattr__(
            self,
            "source_variable_id",
            _normalize_uuid(self.source_variable_id, label="Output source Variable"),
        )
        object.__setattr__(
            self,
            "description",
            _validate_description(self.description, label=f"Output '{self.name}'"),
        )
        object.__setattr__(self, "id", _normalize_uuid(self.id, label="Output"))

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "source_variable_id": self.source_variable_id,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: object) -> FlowOutputDefinition:
        if not isinstance(data, Mapping):
            raise FlowValueDeclarationError("Output declaration must be an object.")
        return cls(
            id=data.get("id"),  # type: ignore[arg-type]
            name=data.get("name"),  # type: ignore[arg-type]
            type=data.get("type"),  # type: ignore[arg-type]
            source_variable_id=data.get("source_variable_id"),  # type: ignore[arg-type]
            description=data.get("description", ""),  # type: ignore[arg-type]
        )


FlowReadableDefinition = FlowInputDefinition | FlowVariableDefinition


@dataclass(frozen=True, slots=True)
class FlowValueCatalog:
    """Validated declarations with the shared identity/name lookup boundary."""

    inputs: tuple[FlowInputDefinition, ...]
    variables: tuple[FlowVariableDefinition, ...]
    outputs: tuple[FlowOutputDefinition, ...]

    def __post_init__(self) -> None:
        normalized = validate_flow_value_declarations(
            self.inputs,
            self.variables,
            self.outputs,
        )
        object.__setattr__(self, "inputs", normalized[0])
        object.__setattr__(self, "variables", normalized[1])
        object.__setattr__(self, "outputs", normalized[2])

    @classmethod
    def build(
        cls,
        inputs: object,
        variables: object,
        outputs: object,
    ) -> FlowValueCatalog:
        return cls(inputs, variables, outputs)  # type: ignore[arg-type]

    @staticmethod
    def _canonical_lookup_id(declaration_id: str) -> str | None:
        try:
            return str(uuid.UUID(declaration_id))
        except (AttributeError, ValueError):
            return None

    def input_by_id(self, declaration_id: str) -> FlowInputDefinition | None:
        canonical_id = self._canonical_lookup_id(declaration_id)
        return next(
            (item for item in self.inputs if item.id == canonical_id),
            None,
        )

    def variable_by_id(self, declaration_id: str) -> FlowVariableDefinition | None:
        canonical_id = self._canonical_lookup_id(declaration_id)
        return next(
            (item for item in self.variables if item.id == canonical_id),
            None,
        )

    def output_by_id(self, declaration_id: str) -> FlowOutputDefinition | None:
        canonical_id = self._canonical_lookup_id(declaration_id)
        return next(
            (item for item in self.outputs if item.id == canonical_id),
            None,
        )

    def readable_by_name(self, name: str) -> FlowReadableDefinition | None:
        normalized_name = name.casefold()
        return next(
            (
                item
                for item in (*self.inputs, *self.variables)
                if item.name.casefold() == normalized_name
            ),
            None,
        )

    def output_by_name(self, name: str) -> FlowOutputDefinition | None:
        normalized_name = name.casefold()
        return next(
            (item for item in self.outputs if item.name.casefold() == normalized_name),
            None,
        )


def _normalize_declarations(
    values: object,
    *,
    kind: str,
    expected_type: type[_DeclarationT],
    loader: Callable[[object], _DeclarationT],
) -> tuple[_DeclarationT, ...]:
    if not isinstance(values, (list, tuple)):
        raise FlowValueDeclarationError(f"Flow {kind} must be an array.")
    return tuple(
        value if isinstance(value, expected_type) else loader(value) for value in values
    )


def validate_flow_value_declarations(
    inputs: object,
    variables: object,
    outputs: object,
) -> tuple[
    tuple[FlowInputDefinition, ...],
    tuple[FlowVariableDefinition, ...],
    tuple[FlowOutputDefinition, ...],
]:
    """Normalize declarations and enforce one unambiguous flow value namespace."""
    normalized_inputs = _normalize_declarations(
        inputs,
        kind="Inputs",
        expected_type=FlowInputDefinition,
        loader=FlowInputDefinition.from_dict,
    )
    normalized_variables = _normalize_declarations(
        variables,
        kind="Variables",
        expected_type=FlowVariableDefinition,
        loader=FlowVariableDefinition.from_dict,
    )
    normalized_outputs = _normalize_declarations(
        outputs,
        kind="Outputs",
        expected_type=FlowOutputDefinition,
        loader=FlowOutputDefinition.from_dict,
    )

    seen_ids: set[str] = set()
    seen_readable_names: set[str] = set()
    seen_output_names: set[str] = set()
    for declarations in (
        normalized_inputs,
        normalized_variables,
        normalized_outputs,
    ):
        for declaration in declarations:
            if declaration.id in seen_ids:
                raise FlowValueDeclarationError(
                    f"Flow value ID '{declaration.id}' is declared more than once."
                )
            seen_ids.add(declaration.id)
    for declaration in (*normalized_inputs, *normalized_variables):
        normalized_name = declaration.name.casefold()
        if normalized_name in seen_readable_names:
            raise FlowValueDeclarationError(
                f"Flow value name '{declaration.name}' is declared more than once."
            )
        seen_readable_names.add(normalized_name)
    for output in normalized_outputs:
        normalized_name = output.name.casefold()
        if normalized_name in seen_output_names:
            raise FlowValueDeclarationError(
                f"Flow Output name '{output.name}' is declared more than once."
            )
        seen_output_names.add(normalized_name)

    variables_by_id = {variable.id: variable for variable in normalized_variables}
    for output in normalized_outputs:
        source = variables_by_id.get(output.source_variable_id)
        if source is None:
            raise FlowValueDeclarationError(
                f"Output '{output.name}' references a missing source Variable."
            )
        if source.type is not output.type:
            raise FlowValueDeclarationError(
                f"Output '{output.name}' type must match source Variable "
                f"'{source.name}'."
            )

    return normalized_inputs, normalized_variables, normalized_outputs


__all__ = [
    "FlowInputDefinition",
    "FlowOutputDefinition",
    "FlowReadableDefinition",
    "FlowValue",
    "FlowValueCatalog",
    "FlowValueDeclarationError",
    "FlowValueType",
    "FlowVariableDefinition",
    "validate_flow_value_declarations",
]
