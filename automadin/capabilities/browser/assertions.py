from __future__ import annotations

from automadin.engine.browser_engine import BrowserSessionData


def assert_url_contains(session_data: BrowserSessionData, text: str) -> None:
    """Assert that the current URL contains the given text."""
    page = session_data.active_page
    if text not in page.url:
        raise AssertionError(f"URL '{page.url}' does not contain '{text}'")
