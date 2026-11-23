"""Runtime state for version-1 flow Inputs, Variables, and value references."""

from __future__ import annotations

import uuid
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType

from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowValueCatalog,
    FlowVariableDefinition,
)
from automadin.models.typed_value import (
    TypedValue,
    ValueCoercionError,
    ValueType,
    canonical_typed_value_text,
    coerce_typed_value,
)
from automadin.models.variable_update import (
    VariableUpdate,
    VariableUpdateOperation,
    variable_update_allowed_operations,
    variable_update_allowed_source_types,
)
from automadin.models.value_reference import (
    BuiltinValueReference,
    FlowValueTemplate,
    InputValueReference,
    LiteralValueReference,
    TemplateTextSegment,
    ValueReference,
    VariableValueReference,
)


class FlowRuntimeValueError(ValueError):
    """Invocation values are missing, stale, malformed, or type-incompatible."""


def _canonical_override_id(value: object, *, layer: str) -> str:
    if not isinstance(value, str):
        raise FlowRuntimeValueError(f"{layer} Input override IDs must be UUID strings.")
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise FlowRuntimeValueError(
            f"{layer} Input override ID '{value}' is not a valid UUID."
        ) from exc


def _normalize_override_layer(
    raw: Mapping[str, object] | object | None,
    *,
    layer: str,
    definitions: Mapping[str, FlowInputDefinition],
) -> dict[str, TypedValue]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise FlowRuntimeValueError(f"{layer} Input overrides must be an object.")
    normalized: dict[str, TypedValue] = {}
    for raw_id, raw_value in raw.items():
        input_id = _canonical_override_id(raw_id, layer=layer)
        if input_id in normalized:
            raise FlowRuntimeValueError(
                f"{layer} Input overrides contain duplicate ID '{input_id}'."
            )
        definition = definitions.get(input_id)
        if definition is None:
            raise FlowRuntimeValueError(
                f"{layer} Input override '{input_id}' is stale or unknown."
            )
        try:
            normalized[input_id] = coerce_typed_value(
                definition.type,
                raw_value,
                subject=f"Input '{definition.name}'",
            )
        except ValueCoercionError as exc:
            raise FlowRuntimeValueError(str(exc)) from exc
    return normalized


def resolve_flow_inputs(
    inputs: tuple[FlowInputDefinition, ...] | list[FlowInputDefinition],
    saved_overrides: Mapping[str, object] | object | None = None,
    transient_overrides: Mapping[str, object] | object | None = None,
) -> Mapping[str, TypedValue]:
    """Resolve an immutable UUID-keyed Input snapshot for one invocation."""
    definitions = {definition.id: definition for definition in inputs}
    resolved: dict[str, TypedValue] = {
        definition.id: definition.default
        for definition in inputs
        if not definition.required
    }
    resolved.update(
        _normalize_override_layer(
            saved_overrides,
            layer="Saved",
            definitions=definitions,
        )
    )
    resolved.update(
        _normalize_override_layer(
            transient_overrides,
            layer="Transient",
            definitions=definitions,
        )
    )
    missing = [definition.name for definition in inputs if definition.id not in resolved]
    if missing:
        raise FlowRuntimeValueError(
            "Required flow Inputs are missing: " + ", ".join(missing) + "."
        )
    return MappingProxyType(resolved)


@dataclass(frozen=True, slots=True)
class RuntimeTypedValue:
    type: ValueType
    value: TypedValue

    def __post_init__(self) -> None:
        try:
            normalized_type = self.type if isinstance(self.type, ValueType) else ValueType(self.type)
            normalized_value = coerce_typed_value(
                normalized_type,
                self.value,
                subject="Built-in value",
            )
        except (TypeError, ValueError, ValueCoercionError) as exc:
            raise FlowRuntimeValueError(str(exc)) from exc
        object.__setattr__(self, "type", normalized_type)
        object.__setattr__(self, "value", normalized_value)


class FlowVariableState:
    """Typed mutable Variables owned by exactly one flow invocation."""

    def __init__(self, definitions: tuple[FlowVariableDefinition, ...]) -> None:
        self._definitions = {definition.id: definition for definition in definitions}
        self._values: dict[str, TypedValue] = {
            definition.id: definition.initial for definition in definitions
        }

    @property
    def snapshot(self) -> Mapping[str, TypedValue]:
        return MappingProxyType(dict(self._values))

    def get(self, variable_id: str) -> TypedValue:
        definition = self._definitions.get(variable_id)
        if definition is None:
            raise FlowRuntimeValueError(
                f"Variable reference '{variable_id}' is stale or unknown."
            )
        return self._values[definition.id]

    def set(self, variable_id: str, value: object) -> TypedValue:
        definition = self._definitions.get(variable_id)
        if definition is None:
            raise FlowRuntimeValueError(
                f"Variable reference '{variable_id}' is stale or unknown."
            )
        try:
            normalized = coerce_typed_value(
                definition.type,
                value,
                subject=f"Variable '{definition.name}'",
            )
        except ValueCoercionError as exc:
            raise FlowRuntimeValueError(str(exc)) from exc
        self._values[definition.id] = normalized
        return normalized

    def reset(self, variable_id: str) -> TypedValue:
        definition = self._definitions.get(variable_id)
        if definition is None:
            raise FlowRuntimeValueError(
                f"Variable reference '{variable_id}' is stale or unknown."
            )
        self._values[definition.id] = definition.initial
        return definition.initial


class FlowInvocationValues:
    """Version-1 values and scoped built-ins for one flow invocation."""

    def __init__(
        self,
        flow_id: str,
        catalog: FlowValueCatalog,
        inputs: Mapping[str, TypedValue],
    ) -> None:
        try:
            self.flow_id = str(uuid.UUID(flow_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise FlowRuntimeValueError("Invocation flow ID must be a UUID.") from exc
        expected_ids = {definition.id for definition in catalog.inputs}
        if set(inputs) != expected_ids:
            raise FlowRuntimeValueError(
                "Input snapshot does not match the flow's declared Inputs."
            )
        normalized_inputs: dict[str, TypedValue] = {}
        for definition in catalog.inputs:
            try:
                normalized_inputs[definition.id] = coerce_typed_value(
                    definition.type,
                    inputs[definition.id],
                    subject=f"Input '{definition.name}'",
                )
            except ValueCoercionError as exc:
                raise FlowRuntimeValueError(str(exc)) from exc
        self.catalog = catalog
        self.inputs: Mapping[str, TypedValue] = MappingProxyType(normalized_inputs)
        self.variables = FlowVariableState(catalog.variables)
        self._builtin_scopes: list[dict[str, RuntimeTypedValue]] = []

    @contextmanager
    def builtin_scope(
        self,
        values: Mapping[str, RuntimeTypedValue],
    ) -> Iterator[None]:
        scope = dict(values)
        if any(not isinstance(value, RuntimeTypedValue) for value in scope.values()):
            raise FlowRuntimeValueError("Built-in scopes require typed runtime values.")
        self._builtin_scopes.append(scope)
        try:
            yield
        finally:
            self._builtin_scopes.pop()

    def _builtin(self, name: str) -> RuntimeTypedValue:
        for scope in reversed(self._builtin_scopes):
            if name in scope:
                return scope[name]
        raise FlowRuntimeValueError(
            f"Built-in reference '{name}' is unavailable in this scope."
        )

    def current_builtin(self, name: str) -> RuntimeTypedValue | None:
        """Return an active scoped Built-in without turning absence into an error."""
        for scope in reversed(self._builtin_scopes):
            value = scope.get(name)
            if value is not None:
                return value
        return None

    def resolve(self, reference: ValueReference) -> TypedValue:
        return self.resolve_typed(reference).value

    def resolve_typed(self, reference: ValueReference) -> RuntimeTypedValue:
        if isinstance(reference, LiteralValueReference):
            return RuntimeTypedValue(reference.type, reference.value)
        if isinstance(reference, InputValueReference):
            declaration = self.catalog.input_by_id(reference.id)
            if declaration is None:
                raise FlowRuntimeValueError(
                    f"Input reference '{reference.id}' is stale."
                )
            return RuntimeTypedValue(declaration.type, self.inputs[declaration.id])
        if isinstance(reference, VariableValueReference):
            declaration = self.catalog.variable_by_id(reference.id)
            if declaration is None:
                raise FlowRuntimeValueError(
                    f"Variable reference '{reference.id}' is stale."
                )
            return RuntimeTypedValue(
                declaration.type,
                self.variables.get(reference.id),
            )
        if isinstance(reference, BuiltinValueReference):
            return self._builtin(reference.name)
        raise FlowRuntimeValueError("Unsupported value reference.")

    def apply_variable_update(self, update: VariableUpdate) -> TypedValue:
        """Evaluate one update completely, then commit the target exactly once."""
        target = self.catalog.variable_by_id(update.target_variable_id)
        if target is None:
            raise FlowRuntimeValueError(
                f"Update target Variable '{update.target_variable_id}' is stale."
            )
        operation = update.operation
        current = self.variables.get(target.id)

        if operation not in variable_update_allowed_operations(target.type):
            raise FlowRuntimeValueError(
                f"Operation '{operation.value}' is not valid for "
                f"{target.type.value} Variable '{target.name}'."
            )
        if operation is VariableUpdateOperation.RESET:
            return self.variables.reset(target.id)
        if operation is VariableUpdateOperation.TOGGLE:
            return self.variables.set(target.id, not current)

        if operation is VariableUpdateOperation.REPLACE:
            assert update.find is not None and update.replacement is not None
            find = self.resolve_typed(update.find)
            replacement = self.resolve_typed(update.replacement)
            if find.type is not ValueType.TEXT or replacement.type is not ValueType.TEXT:
                raise FlowRuntimeValueError(
                    "Text Replace requires text find and replacement values."
                )
            if find.value == "":
                raise FlowRuntimeValueError("Text Replace find value cannot be empty.")
            result: object = str(current).replace(
                str(find.value),
                str(replacement.value),
            )
        else:
            assert update.source is not None
            source = self.resolve_typed(update.source)
            allowed_sources = variable_update_allowed_source_types(
                target.type,
                operation,
            )
            if source.type not in allowed_sources:
                raise FlowRuntimeValueError(
                    f"Operation '{operation.value}' on Variable '{target.name}' "
                    f"cannot use a {source.type.value} source."
                )
            source_value = source.value
            if operation is VariableUpdateOperation.SET:
                result = source_value
            elif operation is VariableUpdateOperation.ADD:
                result = current + source_value  # type: ignore[operator]
            elif operation is VariableUpdateOperation.SUBTRACT:
                result = current - source_value  # type: ignore[operator]
            elif operation is VariableUpdateOperation.MULTIPLY:
                result = current * source_value  # type: ignore[operator]
            elif operation is VariableUpdateOperation.DIVIDE:
                if source_value == 0:
                    raise FlowRuntimeValueError("Update Variable cannot divide by zero.")
                result = current / source_value  # type: ignore[operator]
            elif operation is VariableUpdateOperation.APPEND:
                result = str(current) + str(source_value)
            else:
                result = str(source_value) + str(current)

        if target.type in {ValueType.INTEGER, ValueType.DURATION_MS} and isinstance(
            result,
            float,
        ):
            if not math.isfinite(result) or not result.is_integer():
                raise FlowRuntimeValueError(
                    f"Operation '{operation.value}' on Variable '{target.name}' "
                    "must produce a whole value."
                )
            result = int(result)
        try:
            normalized = coerce_typed_value(
                target.type,
                result,
                subject=f"Variable '{target.name}'",
            )
        except ValueCoercionError as exc:
            raise FlowRuntimeValueError(str(exc)) from exc
        return self.variables.set(target.id, normalized)

    def interpolate(self, template: FlowValueTemplate) -> str:
        return "".join(
            segment.text
            if isinstance(segment, TemplateTextSegment)
            else canonical_typed_value_text(self.resolve(segment))
            for segment in template.segments
        )


__all__ = [
    "FlowInvocationValues",
    "FlowRuntimeValueError",
    "FlowVariableState",
    "RuntimeTypedValue",
    "resolve_flow_inputs",
]
