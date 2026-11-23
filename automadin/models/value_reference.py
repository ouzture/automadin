"""Stable-ID value references and rename-safe free-text templates."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from automadin.models.typed_value import (
    TypedValue,
    ValueCoercionError,
    ValueType,
    canonical_typed_value_text,
    coerce_typed_value,
)

if TYPE_CHECKING:
    from automadin.models.flow_value import FlowValueCatalog

_BUILTIN_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ValueReferenceError(ValueError):
    """A saved reference/template is malformed or cannot be resolved safely."""


def _require_exact_fields(
    data: Mapping[object, object],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(str(key) for key in actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise ValueReferenceError(f"{label} has invalid fields: {'; '.join(details)}.")


def _canonical_uuid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueReferenceError(f"{label} must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ValueReferenceError(f"{label} must be a UUID string.") from exc


def _normalize_value_type(value: object, *, label: str) -> ValueType:
    if isinstance(value, ValueType):
        return value
    try:
        return ValueType(value)
    except (TypeError, ValueError) as exc:
        raise ValueReferenceError(f"{label} has an unsupported type.") from exc


@dataclass(frozen=True, slots=True)
class LiteralValueReference:
    type: ValueType
    value: TypedValue

    def __post_init__(self) -> None:
        value_type = _normalize_value_type(self.type, label="Literal")
        object.__setattr__(self, "type", value_type)
        try:
            normalized = coerce_typed_value(
                value_type,
                self.value,
                subject="Literal value",
            )
        except ValueCoercionError as exc:
            raise ValueReferenceError(str(exc)) from exc
        object.__setattr__(self, "value", normalized)

    def to_dict(self) -> dict[str, object]:
        return {"kind": "literal", "type": self.type.value, "value": self.value}


@dataclass(frozen=True, slots=True)
class InputValueReference:
    id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _canonical_uuid(self.id, label="Input ID"))

    def to_dict(self) -> dict[str, str]:
        return {"kind": "input", "id": self.id}


@dataclass(frozen=True, slots=True)
class VariableValueReference:
    id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _canonical_uuid(self.id, label="Variable ID"))

    def to_dict(self) -> dict[str, str]:
        return {"kind": "variable", "id": self.id}


@dataclass(frozen=True, slots=True)
class BuiltinValueReference:
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _BUILTIN_NAME_PATTERN.fullmatch(
            self.name
        ):
            raise ValueReferenceError(
                "Built-in name must contain only letters, digits, and underscores."
            )

    def to_dict(self) -> dict[str, str]:
        return {"kind": "builtin", "name": self.name}


ValueReference = (
    LiteralValueReference
    | InputValueReference
    | VariableValueReference
    | BuiltinValueReference
)
TemplateValueReference = (
    InputValueReference | VariableValueReference | BuiltinValueReference
)


def value_reference_from_dict(data: object) -> ValueReference:
    if not isinstance(data, Mapping):
        raise ValueReferenceError("Value reference must be an object.")
    kind = data.get("kind")
    if kind == "literal":
        _require_exact_fields(
            data,
            frozenset({"kind", "type", "value"}),
            label="Literal reference",
        )
        return LiteralValueReference(data["type"], data["value"])  # type: ignore[arg-type]
    if kind == "input":
        _require_exact_fields(
            data,
            frozenset({"kind", "id"}),
            label="Input reference",
        )
        return InputValueReference(data["id"])  # type: ignore[arg-type]
    if kind == "variable":
        _require_exact_fields(
            data,
            frozenset({"kind", "id"}),
            label="Variable reference",
        )
        return VariableValueReference(data["id"])  # type: ignore[arg-type]
    if kind == "builtin":
        _require_exact_fields(
            data,
            frozenset({"kind", "name"}),
            label="Built-in reference",
        )
        return BuiltinValueReference(data["name"])  # type: ignore[arg-type]
    raise ValueReferenceError(f"Unknown value reference kind '{kind}'.")


@dataclass(frozen=True, slots=True)
class TemplateTextSegment:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueReferenceError("Template text segment must contain text.")

    def to_dict(self) -> dict[str, str]:
        return {"kind": "text", "text": self.text}


TemplateSegment = TemplateTextSegment | TemplateValueReference


def _template_segment_from_dict(data: object) -> TemplateSegment:
    if not isinstance(data, Mapping):
        raise ValueReferenceError("Template segment must be an object.")
    if data.get("kind") == "text":
        _require_exact_fields(
            data,
            frozenset({"kind", "text"}),
            label="Template text segment",
        )
        return TemplateTextSegment(data["text"])  # type: ignore[arg-type]
    reference = value_reference_from_dict(data)
    if isinstance(reference, LiteralValueReference):
        raise ValueReferenceError("Template segments cannot contain literal references.")
    return reference


@dataclass(frozen=True, slots=True)
class FlowValueTemplate:
    segments: tuple[TemplateSegment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.segments, (list, tuple)):
            raise ValueReferenceError("Template segments must be an array.")
        normalized: list[TemplateSegment] = []
        pending_text = ""
        for raw_segment in self.segments:
            segment = (
                raw_segment
                if isinstance(
                    raw_segment,
                    (
                        TemplateTextSegment,
                        InputValueReference,
                        VariableValueReference,
                        BuiltinValueReference,
                    ),
                )
                else _template_segment_from_dict(raw_segment)
            )
            if isinstance(segment, TemplateTextSegment):
                pending_text += segment.text
                continue
            if pending_text:
                normalized.append(TemplateTextSegment(pending_text))
                pending_text = ""
            normalized.append(segment)
        if pending_text:
            normalized.append(TemplateTextSegment(pending_text))
        object.__setattr__(self, "segments", tuple(normalized))

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "template",
            "segments": [segment.to_dict() for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, data: object) -> FlowValueTemplate:
        if not isinstance(data, Mapping):
            raise ValueReferenceError("Value template must be an object.")
        _require_exact_fields(
            data,
            frozenset({"kind", "segments"}),
            label="Value template",
        )
        if data.get("kind") != "template":
            raise ValueReferenceError("Value template kind must be 'template'.")
        segments = data["segments"]
        if not isinstance(segments, (list, tuple)):
            raise ValueReferenceError("Template segments must be an array.")
        return cls(tuple(_template_segment_from_dict(item) for item in segments))


def resolve_value_reference_type(
    reference: ValueReference,
    catalog: FlowValueCatalog,
    *,
    builtin_types: Mapping[str, ValueType] | None = None,
) -> ValueType:
    """Resolve a source type without applying command-field compatibility rules."""
    if isinstance(reference, LiteralValueReference):
        return reference.type
    if isinstance(reference, InputValueReference):
        declaration = catalog.input_by_id(reference.id)
        if declaration is None:
            raise ValueReferenceError(f"Input reference '{reference.id}' is stale.")
        return declaration.type
    if isinstance(reference, VariableValueReference):
        declaration = catalog.variable_by_id(reference.id)
        if declaration is None:
            raise ValueReferenceError(f"Variable reference '{reference.id}' is stale.")
        return declaration.type
    value_type = (builtin_types or {}).get(reference.name)
    if value_type is None:
        raise ValueReferenceError(
            f"Built-in reference '{reference.name}' is unavailable in this scope."
        )
    return _normalize_value_type(
        value_type,
        label=f"Built-in '{reference.name}'",
    )


def render_value_reference(
    reference: ValueReference,
    catalog: FlowValueCatalog,
) -> str:
    """Render a repairable editor label without changing stable persistence."""
    if isinstance(reference, LiteralValueReference):
        return canonical_typed_value_text(reference.value)
    if isinstance(reference, InputValueReference):
        declaration = catalog.input_by_id(reference.id)
        return (
            f"{{{{{declaration.name}}}}}"
            if declaration is not None
            else f"{{{{<missing Input {reference.id}>}}}}"
        )
    if isinstance(reference, VariableValueReference):
        declaration = catalog.variable_by_id(reference.id)
        return (
            f"{{{{{declaration.name}}}}}"
            if declaration is not None
            else f"{{{{<missing Variable {reference.id}>}}}}"
        )
    return f"{{{{{reference.name}}}}}"


def render_value_template(
    template: FlowValueTemplate,
    catalog: FlowValueCatalog,
) -> str:
    return "".join(
        segment.text
        if isinstance(segment, TemplateTextSegment)
        else render_value_reference(segment, catalog)
        for segment in template.segments
    )


__all__ = [
    "BuiltinValueReference",
    "FlowValueTemplate",
    "InputValueReference",
    "LiteralValueReference",
    "TemplateSegment",
    "TemplateTextSegment",
    "TemplateValueReference",
    "ValueReference",
    "ValueReferenceError",
    "VariableValueReference",
    "render_value_reference",
    "render_value_template",
    "resolve_value_reference_type",
    "value_reference_from_dict",
]
