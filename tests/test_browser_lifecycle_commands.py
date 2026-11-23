from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from automadin.engine.browser_pages import BrowserPageEnsureResult
from automadin.execution.build_executor import build_executor
from automadin.execution.context import FlowContext
from automadin.execution.browser_wait import BrowserTimeoutBudget
from automadin.execution.handlers.tab import (
    handle_ensure_browser_page,
    handle_select_browser_page,
)
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
)
from automadin.models.flow import FlowCommand
from automadin.services.flow_preflight import TargetKind, supported_targets


def _context(registry: Mock) -> FlowContext:
    page = Mock()
    session = DeviceSession(
        device=BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM)),
        data={
            "browser_data": SimpleNamespace(
                page_registry=registry,
                active_page=page,
            )
        },
    )
    context = FlowContext(session)
    context.variables["account"] = "work"
    return context


def test_select_browser_page_handler_interpolates_durable_rule() -> None:
    registry = Mock()
    context = _context(registry)

    handle_select_browser_page(
        FlowCommand(
            "select_browser_page",
            params={
                "strategy": "url_contains",
                "value": "/{{account}}/orders",
                "match_case": True,
            },
        ),
        context,
    )

    registry.select_matching_page.assert_called_once_with(
        "url_contains",
        "/work/orders",
        match_case=True,
    )


def test_ensure_browser_page_handler_uses_url_only_for_replacement() -> None:
    registry = Mock()
    registry.ensure_page.return_value = BrowserPageEnsureResult(
        "tab-2",
        created=True,
    )

    handle_ensure_browser_page(
        FlowCommand(
            "ensure_browser_page",
            params={
                "url": "https://example.test/{{account}}",
                "timeout": 20000,
            },
        ),
        _context(registry),
    )

    registry.ensure_page.assert_called_once_with(
        "https://example.test/work",
        navigation_timeout_ms=20000,
    )


def test_browser_timeout_budget_shares_one_deadline_across_phases() -> None:
    with patch(
        "automadin.execution.browser_wait.time.monotonic",
        side_effect=[15.0, 31.0],
    ):
        budget = BrowserTimeoutBudget(20000, started_at=10.0)

        assert budget.remaining_ms() == 15000
        assert budget.remaining_ms() == 1


def test_browser_timeout_budget_preserves_playwright_no_timeout_value() -> None:
    budget = BrowserTimeoutBudget(0)

    assert budget.remaining_ms() == 0


def test_browser_lifecycle_commands_are_registered_and_browser_only() -> None:
    registered = build_executor().registered_handler_ids
    for command_id in ("select_browser_page", "ensure_browser_page"):
        assert command_id in registered
        assert supported_targets(command_id) == {TargetKind.BROWSER}


@pytest.mark.parametrize(
    ("strategy", "value"),
    [("missing", ""), ("url_exact", "")],
)
def test_invalid_page_rules_fail_before_selection(
    strategy: str,
    value: str,
) -> None:
    from automadin.engine.browser_pages import BrowserPageRegistry

    context = Mock()
    context.pages = []
    context.on = Mock()
    registry = BrowserPageRegistry(context)

    with pytest.raises(RuntimeError):
        registry.select_matching_page(strategy, value)
