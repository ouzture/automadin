"""Shared scalar types and coercion for flow Inputs, Variables, and Outputs."""

from __future__ import annotations

import math
import re
from enum import Enum

_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_DURATION_PATTERN = re.compile(
    r"^\s*(\+?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(ms|millisecond|milliseconds|s|sec|second|seconds|"
    r"m|min|minute|minutes)?\s*$",
    re.IGNORECASE,
)
_MAX_DURATION_MS = 2_147_483_647

TypedValue = str | int | float | bool


class ValueType(str, Enum):
    TEXT = "text"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DURATION_MS = "duration_ms"


class ValueCoercionError(ValueError):
    """A scalar cannot be represented as its declared flow value type."""


def is_value_type_assignable(source: ValueType, target: ValueType) -> bool:
    """Return whether a typed source can be assigned without narrowing."""
    return source is target or (
        source is ValueType.INTEGER and target is ValueType.NUMBER
    )


def _coerce_integer(value: object, *, subject: str) -> int:
    if isinstance(value, bool):
        raise ValueCoercionError(f"{subject} requires an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value.strip()):
        return int(value.strip())
    raise ValueCoercionError(f"{subject} requires an integer.")


def _coerce_number(value: object, *, subject: str) -> float:
    if isinstance(value, bool):
        raise ValueCoercionError(f"{subject} requires a number.")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError as exc:
            raise ValueCoercionError(f"{subject} requires a number.") from exc
    else:
        raise ValueCoercionError(f"{subject} requires a number.")
    if not math.isfinite(result):
        raise ValueCoercionError(f"{subject} requires a finite number.")
    return result


def _coerce_boolean(value: object, *, subject: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueCoercionError(f"{subject} requires true or false.")


def _coerce_duration_ms(value: object, *, subject: str) -> int:
    if isinstance(value, bool):
        raise ValueCoercionError(f"{subject} requires a non-negative duration.")
    if isinstance(value, int):
        milliseconds = value
    elif isinstance(value, str):
        match = _DURATION_PATTERN.fullmatch(value)
        if match is None:
            raise ValueCoercionError(
                f"{subject} requires a duration such as "
                "'300 ms', '1.5 s', or '2 min'."
            )
        amount = float(match.group(1))
        unit = (match.group(2) or "ms").casefold()
        multiplier = (
            1
            if unit in {"ms", "millisecond", "milliseconds"}
            else 1_000
            if unit in {"s", "sec", "second", "seconds"}
            else 60_000
        )
        exact_milliseconds = amount * multiplier
        if (
            not math.isfinite(exact_milliseconds)
            or not exact_milliseconds.is_integer()
        ):
            raise ValueCoercionError(
                f"{subject} must resolve to whole milliseconds."
            )
        milliseconds = int(exact_milliseconds)
    else:
        raise ValueCoercionError(f"{subject} requires a non-negative duration.")
    if milliseconds < 0 or milliseconds > _MAX_DURATION_MS:
        raise ValueCoercionError(
            f"{subject} must be between 0 and {_MAX_DURATION_MS} milliseconds."
        )
    return milliseconds


def coerce_typed_value(
    value_type: ValueType,
    value: object,
    *,
    subject: str,
) -> TypedValue:
    """Coerce one UI/JSON scalar into its declared runtime representation."""
    if value_type is ValueType.TEXT:
        if not isinstance(value, str):
            raise ValueCoercionError(f"{subject} requires text.")
        return value
    if value_type is ValueType.INTEGER:
        return _coerce_integer(value, subject=subject)
    if value_type is ValueType.NUMBER:
        return _coerce_number(value, subject=subject)
    if value_type is ValueType.BOOLEAN:
        return _coerce_boolean(value, subject=subject)
    if value_type is ValueType.DURATION_MS:
        return _coerce_duration_ms(value, subject=subject)
    raise ValueCoercionError(f"{subject} has an unsupported type.")


def canonical_typed_value_text(value: TypedValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".15g")
    return str(value)


__all__ = [
    "TypedValue",
    "ValueCoercionError",
    "ValueType",
    "canonical_typed_value_text",
    "coerce_typed_value",
    "is_value_type_assignable",
]
