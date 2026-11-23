"""Typed comparison contract for version-1 If Value commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from automadin.models.typed_value import TypedValue, ValueType
from automadin.models.value_reference import (
    ValueReference,
    ValueReferenceError,
    value_reference_from_dict,
)


class ValueConditionError(ValueError):
    """A typed value comparison is malformed or incompatible."""


class ValueComparisonOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    GREATER_THAN = "greater_than"
    GREATER_OR_EQUAL = "greater_or_equal"
    LESS_THAN = "less_than"
    LESS_OR_EQUAL = "less_or_equal"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    NOT_STARTS_WITH = "not_starts_with"
    ENDS_WITH = "ends_with"
    NOT_ENDS_WITH = "not_ends_with"
    IS_EMPTY = "is_empty"
    IS_NOT_EMPTY = "is_not_empty"


_UNARY_OPERATORS = frozenset(
    {ValueComparisonOperator.IS_EMPTY, ValueComparisonOperator.IS_NOT_EMPTY}
)
_TEXT_OPERATORS = frozenset(
    {
        ValueComparisonOperator.CONTAINS,
        ValueComparisonOperator.NOT_CONTAINS,
        ValueComparisonOperator.STARTS_WITH,
        ValueComparisonOperator.NOT_STARTS_WITH,
        ValueComparisonOperator.ENDS_WITH,
        ValueComparisonOperator.NOT_ENDS_WITH,
        *_UNARY_OPERATORS,
    }
)
_ORDERED_OPERATORS = frozenset(
    {
        ValueComparisonOperator.GREATER_THAN,
        ValueComparisonOperator.GREATER_OR_EQUAL,
        ValueComparisonOperator.LESS_THAN,
        ValueComparisonOperator.LESS_OR_EQUAL,
    }
)
_NUMERIC_TYPES = frozenset({ValueType.INTEGER, ValueType.NUMBER, ValueType.DURATION_MS})


@dataclass(frozen=True, slots=True)
class ValueCondition:
    left: ValueReference
    operator: ValueComparisonOperator
    right: ValueReference | None = None
    ignore_case: bool = False

    @classmethod
    def from_params(cls, params: object) -> ValueCondition:
        if not isinstance(params, Mapping):
            raise ValueConditionError("If Value condition must be an object.")
        expected = {"left", "operator", "ignore_case"}
        operator_value = params.get("operator")
        try:
            operator = ValueComparisonOperator(operator_value)
        except (TypeError, ValueError) as exc:
            raise ValueConditionError("If Value has an unsupported operator.") from exc
        if operator not in _UNARY_OPERATORS:
            expected.add("right")
        if set(params) != expected:
            missing = sorted(expected - set(params))
            extra = sorted(str(name) for name in set(params) - expected)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unexpected " + ", ".join(extra))
            raise ValueConditionError("If Value has invalid fields: " + "; ".join(details) + ".")
        if not isinstance(params["ignore_case"], bool):
            raise ValueConditionError("If Value ignore_case must be true or false.")
        try:
            left = value_reference_from_dict(params["left"])
            right = value_reference_from_dict(params["right"]) if "right" in params else None
        except ValueReferenceError as exc:
            raise ValueConditionError(str(exc)) from exc
        return cls(left, operator, right, params["ignore_case"])

    def validate_types(self, left_type: ValueType, right_type: ValueType | None) -> None:
        if self.operator in _UNARY_OPERATORS:
            if left_type is not ValueType.TEXT:
                raise ValueConditionError("Empty checks require a text value.")
            return
        if right_type is None:
            raise ValueConditionError("This comparison requires a right value.")
        if self.operator in _TEXT_OPERATORS:
            if left_type is not ValueType.TEXT or right_type is not ValueType.TEXT:
                raise ValueConditionError(f"'{self.operator.value}' requires text values.")
            return
        if self.operator in _ORDERED_OPERATORS:
            if left_type is ValueType.DURATION_MS or right_type is ValueType.DURATION_MS:
                if left_type is right_type is ValueType.DURATION_MS:
                    return
            elif left_type in _NUMERIC_TYPES and right_type in _NUMERIC_TYPES:
                return
            raise ValueConditionError(
                f"'{self.operator.value}' requires matching duration values or numeric values."
            )
        if left_type is right_type or {left_type, right_type} == {
            ValueType.INTEGER,
            ValueType.NUMBER,
        }:
            return
        raise ValueConditionError(
            f"'{self.operator.value}' cannot compare {left_type.value} with {right_type.value}."
        )

    def evaluate(self, left: TypedValue, right: TypedValue | None) -> bool:
        if self.operator is ValueComparisonOperator.IS_EMPTY:
            return left == ""
        if self.operator is ValueComparisonOperator.IS_NOT_EMPTY:
            return left != ""
        assert right is not None
        if isinstance(left, str) and isinstance(right, str):
            compared_left = left.casefold() if self.ignore_case else left
            compared_right = right.casefold() if self.ignore_case else right
        else:
            compared_left, compared_right = left, right
        if self.operator is ValueComparisonOperator.EQUALS:
            return compared_left == compared_right
        if self.operator is ValueComparisonOperator.NOT_EQUALS:
            return compared_left != compared_right
        if self.operator is ValueComparisonOperator.GREATER_THAN:
            return compared_left > compared_right  # type: ignore[operator]
        if self.operator is ValueComparisonOperator.GREATER_OR_EQUAL:
            return compared_left >= compared_right  # type: ignore[operator]
        if self.operator is ValueComparisonOperator.LESS_THAN:
            return compared_left < compared_right  # type: ignore[operator]
        if self.operator is ValueComparisonOperator.LESS_OR_EQUAL:
            return compared_left <= compared_right  # type: ignore[operator]
        if self.operator is ValueComparisonOperator.CONTAINS:
            return str(compared_right) in str(compared_left)
        if self.operator is ValueComparisonOperator.NOT_CONTAINS:
            return str(compared_right) not in str(compared_left)
        if self.operator is ValueComparisonOperator.STARTS_WITH:
            return str(compared_left).startswith(str(compared_right))
        if self.operator is ValueComparisonOperator.NOT_STARTS_WITH:
            return not str(compared_left).startswith(str(compared_right))
        if self.operator is ValueComparisonOperator.ENDS_WITH:
            return str(compared_left).endswith(str(compared_right))
        if self.operator is ValueComparisonOperator.NOT_ENDS_WITH:
            return not str(compared_left).endswith(str(compared_right))
        raise AssertionError(f"Unhandled If Value operator: {self.operator}")


def value_condition_requires_right(operator: object) -> bool:
    try:
        return ValueComparisonOperator(operator) not in _UNARY_OPERATORS
    except (TypeError, ValueError):
        return True


__all__ = [
    "ValueComparisonOperator",
    "ValueCondition",
    "ValueConditionError",
    "value_condition_requires_right",
]
