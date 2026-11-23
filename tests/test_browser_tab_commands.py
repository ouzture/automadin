from __future__ import annotations

import json
from pathlib import Path

import pytest

from automadin.engine.browser_engine import BrowserSessionData
from automadin.engine.browser_pages import (
    BrowserNoActivePageError,
    BrowserTabNotFoundError,
)
from automadin.execution.context import FlowContext
from automadin.execution.handlers.browser_ext import handle_close_all_tabs
from automadin.execution.handlers.navigate import (
    handle_go_back,
    handle_go_forward,
    handle_navigate_to,
    handle_reload_page,
)
from automadin.execution.handlers.tab import (
    handle_close_tab,
    handle_open_new_tab,
    handle_switch_tab,
)
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
)
from automadin.models.flow import FlowCommand


class _FakePage:
    def __init__(
        self,
        name: str,
        *,
        opener: _FakePage | None = None,
    ) -> None:
        self.name = name
        self.opener = opener
        self.url = f"https://example.test/{name}"
        self._closed = False
        self._handlers: dict[str, list[object]] = {}
        self.front_count = 0
        self.goto_calls: list[str] = []
        self.goto_options: list[dict[str, object]] = []
        self.load_state_calls: list[tuple[str, int]] = []
        self.navigation_calls: list[tuple[str, dict[str, object]]] = []
        self.before_close_callback: object | None = None

    def on(self, event: str, callback: object) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def is_closed(self) -> bool:
        return self._closed

    def title(self) -> str:
        return self.name.title()

    def bring_to_front(self) -> None:
        self.front_count += 1

    def goto(self, url: str, **kwargs: object) -> None:
        self.goto_calls.append(url)
        self.goto_options.append(kwargs)
        self.url = url

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self.load_state_calls.append((state, timeout))

    def go_back(self, **options: object) -> None:
        self.navigation_calls.append(("back", options))

    def go_forward(self, **options: object) -> None:
        self.navigation_calls.append(("forward", options))

    def reload(self, **options: object) -> None:
        self.navigation_calls.append(("reload", options))

    def screenshot(self) -> bytes:
        return self.name.encode()

    def close(self) -> None:
        callback = self.before_close_callback
        self.before_close_callback = None
        if callable(callback):
            callback()
        self._closed = True
        for callback in list(self._handlers.get("close", [])):
            callback(self)


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = list(pages)
        self._handlers: dict[str, list[object]] = {}
        self.created_pages: list[_FakePage] = []

    def on(self, event: str, callback: object) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def new_page(self) -> _FakePage:
        page = _FakePage(f"new-{len(self.created_pages) + 1}")
        self.created_pages.append(page)
        self.pages.append(page)
        for callback in list(self._handlers.get("page", [])):
            callback(page)
        return page

    def emit_popup(self, opener: _FakePage) -> _FakePage:
        page = _FakePage("popup", opener=opener)
        self.pages.append(page)
        for callback in list(self._handlers.get("page", [])):
            callback(page)
        return page


class _LogListener:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def on_log(self, message: str) -> None:
        self.messages.append(message)


def _session(
    page_count: int = 1,
) -> tuple[BrowserSessionData, _FakeContext, FlowContext, _LogListener]:
    pages = [_FakePage(f"initial-{index}") for index in range(page_count)]
    context = _FakeContext(pages)
    browser_data = BrowserSessionData(
        playwright=object(),  # type: ignore[arg-type]
        browser=None,
        context=context,  # type: ignore[arg-type]
    )
    listener = _LogListener()
    flow_context = FlowContext(
        DeviceSession(
            device=BrowserDevice(
                BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM)
            ),
            data={"browser_data": browser_data},
        ),
        listener=listener,  # type: ignore[arg-type]
    )
    return browser_data, context, flow_context, listener


def test_site_popup_from_active_page_becomes_next_command_target() -> None:
    browser_data, context, _flow_context, _listener = _session()
    original = browser_data.active_page

    popup = context.emit_popup(original)

    assert browser_data.active_page is popup
    assert browser_data.screenshot() == b"popup"
    snapshot = browser_data.tabs_snapshot()
    assert snapshot.active_tab_id == "tab-2"
    assert snapshot.tabs[1].opener_tab_id == "tab-1"


def test_navigate_to_creates_a_page_when_all_previous_pages_closed() -> None:
    browser_data, context, flow_context, listener = _session()
    browser_data.active_page.close()

    handle_navigate_to(
        FlowCommand(
            "navigate_to",
            params={"url": "example.test/daily", "timeout": 20000},
        ),
        flow_context,
    )

    assert len(context.created_pages) == 1
    assert context.created_pages[0].goto_calls == ["https://example.test/daily"]
    assert context.created_pages[0].goto_options == [
        {"wait_until": "commit", "timeout": 20000}
    ]
    assert context.created_pages[0].load_state_calls == [
        ("domcontentloaded", 500)
    ]
    assert listener.messages == ["Navigated to example.test/daily"]


def test_navigate_to_replaces_a_page_closed_between_lookup_and_navigation() -> None:
    browser_data, context, flow_context, _listener = _session()
    original = browser_data.active_page

    def close_before_goto(*_args: object, **_kwargs: object) -> None:
        original._closed = True
        raise RuntimeError("Target page, context or browser has been closed")

    original.goto = close_before_goto  # type: ignore[method-assign]

    handle_navigate_to(
        FlowCommand("navigate_to", params={"url": "https://example.test/retry"}),
        flow_context,
    )

    assert len(context.created_pages) == 1
    assert context.created_pages[0].goto_calls == ["https://example.test/retry"]
    assert context.created_pages[0].goto_options == [
        {"wait_until": "commit", "timeout": 30000}
    ]


@pytest.mark.parametrize(
    ("handler", "command_id", "operation", "message"),
    [
        (handle_go_back, "go_back", "back", "Navigated back"),
        (handle_go_forward, "go_forward", "forward", "Navigated forward"),
        (handle_reload_page, "reload_page", "reload", "Page reloaded"),
    ],
)
def test_history_navigation_uses_bounded_commit_then_cooperative_dom_wait(
    handler,
    command_id: str,
    operation: str,
    message: str,
) -> None:
    browser_data, _context, flow_context, listener = _session()
    page = browser_data.active_page

    handler(FlowCommand(command_id), flow_context)

    assert page.navigation_calls == [
            (operation, {"wait_until": "commit", "timeout": 30000})
    ]
    assert page.load_state_calls == [("domcontentloaded", 500)]
    assert listener.messages == [message]


def test_open_and_switch_tab_log_only_after_real_state_change() -> None:
    browser_data, context, flow_context, listener = _session()

    handle_open_new_tab(
        FlowCommand(
            "open_new_tab",
            params={"url": "https://open.test/", "timeout": 20000},
        ),
        flow_context,
    )
    handle_switch_tab(
        FlowCommand("switch_tab", params={"index": 0}),
        flow_context,
    )

    assert browser_data.tabs_snapshot().active_tab_id == "tab-1"
    assert context.created_pages[0].goto_options == [
        {"wait_until": "commit", "timeout": 20000}
    ]
    assert context.created_pages[0].load_state_calls == [
        ("domcontentloaded", 500)
    ]
    assert listener.messages == [
        "Opened a new tab → https://open.test/",
        "Selected Automadin tab position 1",
    ]

    with pytest.raises(BrowserTabNotFoundError, match="index 9"):
        handle_switch_tab(
            FlowCommand("switch_tab", params={"index": 9}),
            flow_context,
        )
    assert len(listener.messages) == 2


def test_close_tab_preserves_legacy_last_tab_noop_and_logs_truthfully() -> None:
    browser_data, _context, flow_context, listener = _session()

    handle_close_tab(FlowCommand("close_tab"), flow_context)

    assert len(browser_data.tabs_snapshot().tabs) == 1
    assert listener.messages == ["Close skipped — only one tab remains"]


def test_close_tab_reports_empty_context_instead_of_claiming_one_remains() -> None:
    browser_data, _context, flow_context, listener = _session()
    browser_data.active_page.close()

    with pytest.raises(BrowserNoActivePageError, match="No live browser tab"):
        handle_close_tab(FlowCommand("close_tab"), flow_context)

    assert listener.messages == []


def test_close_tab_logs_when_reentrant_closure_requires_fresh_tab() -> None:
    browser_data, context, flow_context, listener = _session(page_count=2)
    browser_data.page_registry.select_tab("tab-2")
    context.pages[1].before_close_callback = context.pages[0].close

    handle_close_tab(FlowCommand("close_tab"), flow_context)

    assert len(browser_data.tabs_snapshot().tabs) == 1
    assert listener.messages == [
        "Closed the current tab and opened a fresh tab"
    ]


def test_legacy_switch_index_ignores_popup_inserted_before_explicit_tab() -> None:
    browser_data, context, flow_context, listener = _session()
    context.emit_popup(browser_data.active_page)
    handle_open_new_tab(FlowCommand("open_new_tab"), flow_context)

    handle_switch_tab(
        FlowCommand("switch_tab", params={"index": 1}),
        flow_context,
    )

    assert browser_data.tabs_snapshot().active_tab_id == "tab-3"
    assert listener.messages[-1] == "Selected Automadin tab position 2"


def test_switch_tab_metadata_explains_compatibility_projection() -> None:
    definitions = json.loads(
        (
            Path(__file__).parents[1]
            / "automadin"
            / "resources"
            / "command_types.json"
        ).read_text(encoding="utf-8")
    )
    switch_tab = next(item for item in definitions if item["id"] == "switch_tab")

    assert "site popups are excluded" in switch_tab["description"]
    assert switch_tab["params"][0]["label"] == (
        "Automadin Tab Position (0 = first)"
    )


def test_close_all_tabs_opens_fresh_page_and_logs_truthfully() -> None:
    browser_data, context, flow_context, listener = _session(page_count=3)
    previous = tuple(context.pages)

    handle_close_all_tabs(FlowCommand("close_all_tabs"), flow_context)

    snapshot = browser_data.tabs_snapshot()
    assert len(snapshot.tabs) == 1
    assert snapshot.active_tab_id == "tab-4"
    assert all(page.is_closed() for page in previous)
    assert listener.messages == [
        "Closed all previous tabs and opened a fresh tab"
    ]
