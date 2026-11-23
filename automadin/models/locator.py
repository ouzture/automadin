"""Structured, JSON-safe element locators used by flow commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class LocatorStrategy(str, Enum):
    CSS = "css"
    XPATH = "xpath"
    TEST_ID = "test_id"
    ROLE = "role"
    TEXT = "text"
    LABEL = "label"
    PLACEHOLDER = "placeholder"
    ALT_TEXT = "alt_text"
    TITLE = "title"


_VALUE_STRATEGIES = frozenset(LocatorStrategy).difference({LocatorStrategy.ROLE})
_FIELDS = frozenset(
    {"platform", "strategy", "value", "role", "name", "exact", "fallbacks"}
)
_MAX_FALLBACKS = 8


@dataclass(frozen=True)
class LocatorSpec:
    """Validated description of one browser element locator.

    Legacy selector strings are interpreted as CSS. Fallbacks are deliberately
    flat and bounded so a saved flow cannot contain an arbitrarily deep tree.
    """

    strategy: LocatorStrategy
    value: str | None = None
    role: str | None = None
    name: str | None = None
    exact: bool = False
    fallbacks: tuple[LocatorSpec, ...] = ()

    @classmethod
    def parse(cls, value: object, *, _fallback: bool = False) -> LocatorSpec:
        if isinstance(value, cls):
            if _fallback and value.fallbacks:
                raise ValueError("Locator fallbacks cannot contain nested fallbacks")
            return value
        if isinstance(value, str):
            selector = value.strip()
            if not selector:
                raise ValueError("Locator selector cannot be empty")
            return cls(strategy=LocatorStrategy.CSS, value=selector)
        if not isinstance(value, Mapping):
            raise ValueError("Locator must be a selector string or an object")

        unknown = set(value).difference(_FIELDS)
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise ValueError(f"Locator contains unknown field(s): {names}")
        if value.get("platform", "browser") != "browser":
            raise ValueError("Locator platform must be 'browser'")

        try:
            strategy = LocatorStrategy(value.get("strategy"))
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(item.value for item in LocatorStrategy)
            raise ValueError(f"Locator strategy must be one of: {allowed}") from exc

        exact = value.get("exact", False)
        if not isinstance(exact, bool):
            raise ValueError("Locator exact must be a boolean")

        raw_value = _optional_text(value, "value")
        role = _optional_text(value, "role")
        name = _optional_text(value, "name")
        if strategy is LocatorStrategy.ROLE:
            if not role:
                raise ValueError("Role locator requires a non-empty role")
            if raw_value is not None:
                raise ValueError("Role locator does not use value")
        else:
            if strategy not in _VALUE_STRATEGIES or not raw_value:
                raise ValueError(f"{strategy.value} locator requires a non-empty value")
            if role is not None or name is not None:
                raise ValueError(f"{strategy.value} locator does not use role or name")

        raw_fallbacks = value.get("fallbacks", [])
        if not isinstance(raw_fallbacks, (list, tuple)):
            raise ValueError("Locator fallbacks must be an array")
        if _fallback and raw_fallbacks:
            raise ValueError("Locator fallbacks cannot contain nested fallbacks")
        if len(raw_fallbacks) > _MAX_FALLBACKS:
            raise ValueError(f"Locator supports at most {_MAX_FALLBACKS} fallbacks")
        fallbacks = tuple(cls.parse(item, _fallback=True) for item in raw_fallbacks)

        return cls(
            strategy=strategy,
            value=raw_value,
            role=role,
            name=name,
            exact=exact,
            fallbacks=fallbacks,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "platform": "browser",
            "strategy": self.strategy.value,
        }
        if self.strategy is LocatorStrategy.ROLE:
            result["role"] = self.role
            if self.name is not None:
                result["name"] = self.name
        else:
            result["value"] = self.value
        if self.exact:
            result["exact"] = True
        if self.fallbacks:
            result["fallbacks"] = [item.to_dict() for item in self.fallbacks]
        return result

    def text_values(self) -> tuple[str, ...]:
        own = tuple(
            text for text in (self.value, self.role, self.name) if text is not None
        )
        return own + tuple(
            text for fallback in self.fallbacks for text in fallback.text_values()
        )


def _optional_text(value: Mapping[object, object], field: str) -> str | None:
    raw = value.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"Locator {field} must be text")
    normalized = raw.strip()
    return normalized or None


def normalize_locator(value: object) -> dict[str, Any]:
    """Return a canonical structure containing only JSON-native values."""
    return LocatorSpec.parse(value).to_dict()


def is_locator_value(value: object, *, allow_empty: bool = False) -> bool:
    """Return whether a flow/metadata value is a supported locator value."""
    if allow_empty and value == "":
        return True
    try:
        LocatorSpec.parse(value)
    except (TypeError, ValueError):
        return False
    return True


def validate_locator(value: object) -> bool:
    """Compatibility alias used by command metadata and semantic preflight."""
    return is_locator_value(value)


__all__ = [
    "LocatorSpec",
    "LocatorStrategy",
    "is_locator_value",
    "normalize_locator",
    "validate_locator",
]
