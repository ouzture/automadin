"""Pure editor state rules for the If Value comparison contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from automadin.models.value_condition import value_condition_requires_right


def normalize_value_condition_editor_change(
    params: Mapping[str, Any], name: str, value: Any
) -> dict[str, Any]:
    result = dict(params)
    result[name] = value
    if name == "operator" and not value_condition_requires_right(value):
        result.pop("right", None)
    return result


def configure_value_condition_param_defs(
    param_defs: Sequence[Mapping[str, Any]], params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    return [
        dict(definition)
        for definition in param_defs
        if definition.get("name") != "right"
        or value_condition_requires_right(params.get("operator"))
    ]


__all__ = [
    "configure_value_condition_param_defs",
    "normalize_value_condition_editor_change",
]
