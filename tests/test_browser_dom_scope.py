from __future__ import annotations

import pytest

from automadin.models.browser_dom import BrowserDomScope


def test_page_scope_round_trips() -> None:
    scope = BrowserDomScope.parse({"mode": "page"})

    assert scope.container is None
    assert BrowserDomScope.parse(scope.to_dict()) == scope


def test_container_scope_accepts_legacy_locator_and_round_trips() -> None:
    scope = BrowserDomScope.parse("#results")

    assert scope.to_dict() == {
        "mode": "container",
        "container": {
            "platform": "browser",
            "strategy": "css",
            "value": "#results",
        },
    }
    assert BrowserDomScope.parse(scope.to_dict()) == scope


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "unknown"},
        {"mode": "container"},
        {"mode": "page", "container": "#results"},
        {"mode": "page", "extra": True},
    ],
)
def test_invalid_scopes_fail_closed(value: object) -> None:
    with pytest.raises(ValueError):
        BrowserDomScope.parse(value)
