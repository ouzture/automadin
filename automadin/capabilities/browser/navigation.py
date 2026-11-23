from __future__ import annotations

from automadin.engine.browser_engine import BrowserSessionData
from automadin.engine.browser_pages import BrowserNoActivePageError


def _is_closed_target_error(error: BaseException) -> bool:
    """Identify Playwright's stable closed-target failure without leaking it.

    Importing Playwright's private error implementation here would tightly
    couple this capability to a particular Playwright release.  The public
    error text is stable and lets the browser-page layer remain testable with
    structural fake pages.
    """
    return "Target page, context or browser has been closed" in str(error)


def navigate_to(
    session_data: BrowserSessionData,
    url: str,
    *,
    timeout_ms: int = 30000,
) -> None:
    """Navigate a live page to *url*, recovering one stale page if possible."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    session_data.page_registry.ensure_page()
    page = session_data.active_page
    try:
        page.goto(url, wait_until="commit", timeout=timeout_ms)
        return
    except BaseException as exc:
        if not _is_closed_target_error(exc):
            raise

    # A page may close between registry reconciliation and ``goto``.  Discard
    # the known-dead target, create a fresh page when the context remains live,
    # and retry exactly once.  Repeated retries can mask a browser that is
    # genuinely closing and would navigate more than the user requested.
    session_data.page_registry.discard_page(page)
    try:
        session_data.page_registry.ensure_page()
        session_data.active_page.goto(
            url,
            wait_until="commit",
            timeout=timeout_ms,
        )
    except BaseException as retry_error:
        if _is_closed_target_error(retry_error):
            raise BrowserNoActivePageError(
                "The selected browser closed while navigation was starting. "
                "Reconnect it and try again."
            ) from retry_error
        raise


def go_back(session_data: BrowserSessionData, *, timeout_ms: int = 30000) -> None:
    """Go back in browser history."""
    session_data.active_page.go_back(wait_until="commit", timeout=timeout_ms)


def go_forward(session_data: BrowserSessionData, *, timeout_ms: int = 30000) -> None:
    """Go forward in browser history."""
    session_data.active_page.go_forward(wait_until="commit", timeout=timeout_ms)


def reload_page(session_data: BrowserSessionData, *, timeout_ms: int = 30000) -> None:
    """Reload the current page."""
    session_data.active_page.reload(wait_until="commit", timeout=timeout_ms)
