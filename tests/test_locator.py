from __future__ import annotations

import json

import pytest

from automadin.models.locator import LocatorSpec, LocatorStrategy, normalize_locator


def test_legacy_selector_normalizes_to_css() -> None:
    assert normalize_locator("  #login button  ") == {
        "platform": "browser",
        "strategy": "css",
        "value": "#login button",
    }


def test_role_locator_and_fallbacks_round_trip_through_json() -> None:
    raw = {
        "platform": "browser",
        "strategy": "role",
        "role": "button",
        "name": "Sign in",
        "exact": True,
        "fallbacks": [
            {"strategy": "test_id", "value": "sign-in"},
            "#login button",
        ],
    }

    normalized = normalize_locator(raw)

    assert json.loads(json.dumps(normalized)) == normalized
    spec = LocatorSpec.parse(normalized)
    assert spec.strategy is LocatorStrategy.ROLE
    assert spec.fallbacks[0].strategy is LocatorStrategy.TEST_ID


@pytest.mark.parametrize(
    "value",
    [
        "",
        {"strategy": "css", "value": ""},
        {"strategy": "role", "name": "Submit"},
        {"platform": "android", "strategy": "text", "value": "Submit"},
        {"strategy": "css", "value": "button", "typo": True},
        {"strategy": "css", "value": "button", "exact": "yes"},
        {
            "strategy": "css",
            "value": "button",
            "fallbacks": [
                {
                    "strategy": "text",
                    "value": "Submit",
                    "fallbacks": ["button"],
                }
            ],
        },
    ],
)
def test_invalid_locator_values_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        LocatorSpec.parse(value)
