from __future__ import annotations

from automadin.engine.browser_engine import BrowserSessionData
from automadin.engine.browser_pages import (
    BrowserPageEnsureResult,
    BrowserLastTabError,
    BrowserTabCloseResult,
)


def open_new_tab(
    session_data: BrowserSessionData,
    url: str | None = None,
    *,
    timeout_ms: int = 30000,
) -> str:
    """Open a new tab, optionally navigating to a URL."""
    page = session_data.context.new_page()
    tab_id = session_data.page_registry.track_page(page, activate=True)
    if url:
        page.goto(url, wait_until="commit", timeout=timeout_ms)
    return tab_id


def close_tab(session_data: BrowserSessionData) -> BrowserTabCloseResult | None:
    """Close the active tab, preserving the historical last-tab no-op."""
    try:
        return session_data.page_registry.close_active_page()
    except BrowserLastTabError:
        return None


def switch_tab(session_data: BrowserSessionData, index: int) -> str:
    """Switch to the tab at the current zero-based position."""
    tab_id = session_data.page_registry.tab_id_at(index)
    session_data.page_registry.select_tab(tab_id)
    return tab_id


def close_all_tabs(session_data: BrowserSessionData) -> str:
    """Replace all current tabs with one fresh active tab."""
    return session_data.page_registry.replace_all_with_fresh_page()


def select_page(
    session_data: BrowserSessionData,
    strategy: str,
    value: str = "",
    *,
    match_case: bool = False,
) -> str:
    """Select a tab using a durable rule rather than a session-local ID."""
    return session_data.page_registry.select_matching_page(
        strategy,
        value,
        match_case=match_case,
    )


def ensure_page(
    session_data: BrowserSessionData,
    url: str | None = None,
    *,
    timeout_ms: int = 30000,
) -> BrowserPageEnsureResult:
    """Ensure that a live page exists inside the current browser context."""
    return session_data.page_registry.ensure_page(
        url,
        navigation_timeout_ms=timeout_ms,
    )
