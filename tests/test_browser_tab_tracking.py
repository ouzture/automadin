from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError

import pytest

from automadin.engine.browser_pages import (
    BrowserLastTabError,
    BrowserNoActivePageError,
    BrowserPageCloseError,
    BrowserPageTargetError,
    BrowserPageRegistry,
    BrowserPageThreadError,
    BrowserTabNotFoundError,
)


class _FakePage:
    def __init__(
        self,
        name: str,
        *,
        opener: _FakePage | None = None,
        title_error: bool = False,
        close_error: bool = False,
        is_closed_error: bool = False,
        title_callback: object | None = None,
        bring_to_front_callback: object | None = None,
        survive_close_callback: bool = False,
        before_close_callback: object | None = None,
    ) -> None:
        self.name = name
        self.url = f"https://example.test/{name}"
        self.opener = opener
        self._title_error = title_error
        self._close_error = close_error
        self._is_closed_error = is_closed_error
        self._title_callback = title_callback
        self._bring_to_front_callback = bring_to_front_callback
        self._survive_close_callback = survive_close_callback
        self._before_close_callback = before_close_callback
        self._closed = False
        self._handlers: dict[str, list[object]] = {}
        self.bring_to_front_calls = 0
        self.goto_calls: list[str] = []
        self.goto_options: list[dict[str, object]] = []

    def on(self, event: str, callback: object) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def title(self) -> str:
        callback = self._title_callback
        self._title_callback = None
        if callable(callback):
            callback()
        if self._title_error:
            raise RuntimeError("title unavailable")
        return f"Title {self.name}"

    def is_closed(self) -> bool:
        if self._is_closed_error:
            raise RuntimeError("close state unavailable")
        return self._closed

    def bring_to_front(self) -> None:
        self.bring_to_front_calls += 1
        callback = self._bring_to_front_callback
        self._bring_to_front_callback = None
        if callable(callback):
            callback()

    def goto(self, url: str, **options: object) -> None:
        self.goto_calls.append(url)
        self.goto_options.append(options)
        self.url = url

    def close(self, *, emit: bool = True) -> None:
        if self._close_error:
            raise RuntimeError("close failed")
        callback = self._before_close_callback
        self._before_close_callback = None
        if callable(callback):
            callback()
        if not self._survive_close_callback:
            self._closed = True
        if emit:
            self.emit("close")

    def emit(self, event: str) -> None:
        for callback in list(self._handlers.get(event, [])):
            callback(self)

    def emit_popup(self, popup: _FakePage) -> None:
        for callback in list(self._handlers.get("popup", [])):
            callback(popup)


class _FakeContext:
    def __init__(self, pages: list[_FakePage] | None = None) -> None:
        self.pages = list(pages or [])
        self._handlers: dict[str, list[object]] = {}
        self._new_page_number = 0

    def on(self, event: str, callback: object) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def add_page(self, page: _FakePage, *, emit: bool = True) -> None:
        self.pages.append(page)
        if emit:
            for callback in list(self._handlers.get("page", [])):
                callback(page)

    def new_page(self) -> _FakePage:
        self._new_page_number += 1
        page = _FakePage(f"fresh-{self._new_page_number}")
        self.add_page(page)
        return page


def test_seed_assigns_stable_ids_and_returns_immutable_safe_snapshots() -> None:
    first = _FakePage("first")
    second = _FakePage("second")
    registry = BrowserPageRegistry(_FakeContext([first, second]))

    initial = registry.snapshot()
    repeated = registry.snapshot()

    assert initial == repeated
    assert initial.active_tab_id == "tab-1"
    assert [tab.tab_id for tab in initial.tabs] == ["tab-1", "tab-2"]
    assert [tab.index for tab in initial.tabs] == [0, 1]
    assert initial.tabs[0].url == "https://example.test/first"
    assert initial.tabs[0].title == "Title first"
    assert initial.tabs[0].is_active
    assert initial.tabs[0].opener_tab_id is None
    assert all(tab is not first and tab is not second for tab in initial.tabs)
    with pytest.raises(FrozenInstanceError):
        initial.tabs[0].title = "changed"  # type: ignore[misc]


def test_active_opener_popup_activates_but_background_page_does_not() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)

    popup = _FakePage("popup", opener=first)
    context.add_page(popup)
    after_popup = registry.snapshot()

    assert after_popup.active_tab_id == "tab-2"
    assert after_popup.tabs[1].opener_tab_id == "tab-1"
    assert registry.active_page is popup

    background = _FakePage("background")
    context.add_page(background)
    after_background = registry.snapshot()

    assert after_background.active_tab_id == "tab-2"
    assert after_background.tabs[2].opener_tab_id is None
    assert registry.active_page is popup


def test_parent_popup_event_activates_child_when_opener_is_unavailable() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)
    popup = _FakePage("noopener-popup")

    context.add_page(popup)
    assert registry.active_page is first

    first.emit_popup(popup)

    assert registry.active_page is popup
    assert registry.snapshot().tabs[1].opener_tab_id == "tab-1"


def test_registration_is_duplicate_safe_and_explicit_open_can_activate() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)
    new_page = _FakePage("new")

    context.add_page(new_page)
    event_tab_id = registry.snapshot().tabs[1].tab_id
    explicit_tab_id = registry.track_page(new_page, activate=True)

    assert explicit_tab_id == event_tab_id
    assert len(registry.snapshot().tabs) == 2
    assert registry.active_tab_id == event_tab_id
    assert len(new_page._handlers["close"]) == 1


def test_opener_lookup_reentrancy_does_not_duplicate_popup_registration() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)
    popup = _FakePage("popup")

    def opener_with_reentrant_popup() -> _FakePage:
        first.emit_popup(popup)
        return first

    popup.opener = opener_with_reentrant_popup  # type: ignore[assignment]
    context.add_page(popup)

    snapshot = registry.snapshot()
    assert [tab.tab_id for tab in snapshot.tabs] == ["tab-1", "tab-2"]
    assert snapshot.active_tab_id == "tab-2"
    assert len(popup._handlers["close"]) == 1
    assert len(popup._handlers["popup"]) == 1


def test_active_close_falls_back_to_live_opener_chain() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)
    popup = _FakePage("popup", opener=first)
    nested = _FakePage("nested", opener=popup)
    context.add_page(popup)
    context.add_page(nested)

    assert registry.active_page is nested
    nested.close()
    assert registry.active_page is popup
    popup.close()
    assert registry.active_page is first


def test_active_close_without_opener_uses_nearest_surviving_page() -> None:
    first = _FakePage("first")
    second = _FakePage("second")
    third = _FakePage("third")
    registry = BrowserPageRegistry(_FakeContext([first, second, third]))

    registry.select_tab("tab-2")
    second.close()

    assert registry.active_page is third
    assert registry.active_tab_id == "tab-3"


def test_reconcile_discovers_missed_events_and_removes_silently_closed_pages() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)
    missed = _FakePage("missed", opener=first)

    context.add_page(missed, emit=False)
    discovered = registry.snapshot()
    assert [tab.tab_id for tab in discovered.tabs] == ["tab-1", "tab-2"]
    assert discovered.active_tab_id == "tab-2"

    missed.close(emit=False)
    reconciled = registry.snapshot()
    assert [tab.tab_id for tab in reconciled.tabs] == ["tab-1"]
    assert reconciled.active_tab_id == "tab-1"


def test_select_tab_uses_stable_id_and_brings_page_to_front() -> None:
    first = _FakePage("first")
    second = _FakePage("second")
    registry = BrowserPageRegistry(_FakeContext([first, second]))

    registry.select_tab("tab-2")

    assert registry.active_page is second
    assert second.bring_to_front_calls == 1
    assert first.bring_to_front_calls == 0


def test_select_tab_does_not_reactivate_page_closed_during_bring_to_front() -> None:
    first = _FakePage("first")
    second = _FakePage("second")
    registry = BrowserPageRegistry(_FakeContext([first, second]))
    second._bring_to_front_callback = second.close

    with pytest.raises(BrowserTabNotFoundError, match="closed while"):
        registry.select_tab("tab-2")

    assert registry.active_page is first
    assert registry.active_tab_id == "tab-1"


def test_legacy_indices_ignore_discovered_popup_but_include_explicit_tab() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)
    popup = _FakePage("popup", opener=first)
    context.add_page(popup)
    explicit = _FakePage("explicit")
    context.add_page(explicit)
    registry.track_page(explicit, activate=True)

    assert registry.tab_id_at(0) == "tab-1"
    assert registry.tab_id_at(1) == "tab-3"
    with pytest.raises(BrowserTabNotFoundError, match="index 2"):
        registry.tab_id_at(2)


def test_close_active_page_fails_for_last_tab_and_closes_selected_tab() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)

    with pytest.raises(BrowserLastTabError, match="only browser tab"):
        registry.close_active_page()
    assert not first.is_closed()

    second = _FakePage("second")
    context.add_page(second)
    registry.select_tab("tab-2")

    result = registry.close_active_page()
    assert result.closed_tab_id == "tab-2"
    assert not result.opened_replacement
    assert second.is_closed()
    assert registry.active_page is first


def test_close_active_opens_fresh_page_if_other_tab_closes_reentrantly() -> None:
    first = _FakePage("first")
    second = _FakePage("second", before_close_callback=first.close)
    context = _FakeContext([first, second])
    registry = BrowserPageRegistry(context)
    registry.select_tab("tab-2")

    result = registry.close_active_page()
    snapshot = registry.snapshot()

    assert result.closed_tab_id == "tab-2"
    assert result.opened_replacement
    assert [tab.tab_id for tab in snapshot.tabs] == ["tab-3"]
    assert snapshot.active_tab_id == "tab-3"


def test_replace_all_with_fresh_page_closes_every_previous_page() -> None:
    first = _FakePage("first")
    second = _FakePage("second")
    context = _FakeContext([first, second])
    registry = BrowserPageRegistry(context)

    fresh_tab_id = registry.replace_all_with_fresh_page()
    snapshot = registry.snapshot()

    assert first.is_closed()
    assert second.is_closed()
    assert snapshot.active_tab_id == fresh_tab_id
    assert [tab.tab_id for tab in snapshot.tabs] == [fresh_tab_id]


def test_replace_all_reports_any_unclosed_previous_page() -> None:
    stuck = _FakePage("stuck", close_error=True)
    registry = BrowserPageRegistry(_FakeContext([stuck]))

    with pytest.raises(BrowserPageCloseError, match="Not every previous"):
        registry.replace_all_with_fresh_page()

    snapshot = registry.snapshot()
    assert len(snapshot.tabs) == 2
    assert snapshot.active_tab_id == "tab-2"


def test_indeterminate_close_state_is_not_reported_as_closed() -> None:
    first = _FakePage("first")
    stuck = _FakePage(
        "stuck",
        close_error=True,
        is_closed_error=True,
    )
    registry = BrowserPageRegistry(_FakeContext([first, stuck]))
    registry.select_tab("tab-2")

    with pytest.raises(BrowserPageCloseError, match="could not be confirmed"):
        registry.close_active_page()

    snapshot = registry.snapshot()
    assert [tab.tab_id for tab in snapshot.tabs] == ["tab-1", "tab-2"]
    assert snapshot.active_tab_id == "tab-2"


def test_close_callback_cannot_confirm_a_page_that_survives_under_new_id() -> None:
    first = _FakePage("first")
    survivor = _FakePage("survivor", survive_close_callback=True)
    registry = BrowserPageRegistry(_FakeContext([first, survivor]))
    registry.select_tab("tab-2")

    with pytest.raises(BrowserPageCloseError, match="could not be confirmed"):
        registry.close_active_page()

    snapshot = registry.snapshot()
    assert len(snapshot.tabs) == 2
    assert any(tab.title == "Title survivor" for tab in snapshot.tabs)


def test_missing_tabs_and_empty_context_raise_explicit_errors() -> None:
    registry = BrowserPageRegistry(_FakeContext())

    with pytest.raises(BrowserNoActivePageError, match="No live browser tab"):
        _ = registry.active_page
    with pytest.raises(BrowserTabNotFoundError, match="tab-missing"):
        registry.select_tab("tab-missing")
    with pytest.raises(BrowserTabNotFoundError, match="index 0"):
        registry.tab_id_at(0)


def test_snapshot_tolerates_page_metadata_failure() -> None:
    page = _FakePage("broken-title", title_error=True)
    registry = BrowserPageRegistry(_FakeContext([page]))

    snapshot = registry.snapshot()

    assert snapshot.tabs[0].url == "https://example.test/broken-title"
    assert snapshot.tabs[0].title == ""


def test_snapshot_retries_when_title_dispatches_a_close_callback() -> None:
    first = _FakePage("first")
    second = _FakePage("second")
    first._title_callback = first.close
    registry = BrowserPageRegistry(_FakeContext([first, second]))

    snapshot = registry.snapshot()

    assert [tab.tab_id for tab in snapshot.tabs] == ["tab-2"]
    assert snapshot.active_tab_id == "tab-2"
    assert snapshot.tabs[0].is_active


def test_registry_rejects_cross_thread_page_access() -> None:
    registry = BrowserPageRegistry(_FakeContext([_FakePage("first")]))
    failures: list[BaseException] = []

    def read_from_wrong_thread() -> None:
        try:
            registry.snapshot()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=read_from_wrong_thread)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], BrowserPageThreadError)


def test_durable_url_and_title_rules_select_unique_page() -> None:
    first = _FakePage("first")
    second = _FakePage("orders")
    registry = BrowserPageRegistry(_FakeContext([first, second]))

    selected = registry.select_matching_page(
        "url_contains",
        "/ORDERS",
        match_case=False,
    )
    assert selected == "tab-2"
    assert registry.active_page is second

    selected = registry.select_matching_page("title_exact", "Title first")
    assert selected == "tab-1"
    assert registry.active_page is first


def test_durable_metadata_rule_rejects_ambiguous_matches() -> None:
    first = _FakePage("orders-one")
    second = _FakePage("orders-two")
    registry = BrowserPageRegistry(_FakeContext([first, second]))

    with pytest.raises(BrowserPageTargetError, match="matched 2"):
        registry.select_matching_page("url_contains", "orders")


def test_durable_popup_and_opener_rules_follow_relationships() -> None:
    first = _FakePage("first")
    context = _FakeContext([first])
    registry = BrowserPageRegistry(context)
    first_popup = _FakePage("popup-one", opener=first)
    second_popup = _FakePage("popup-two", opener=first)
    context.add_page(first_popup)
    context.add_page(second_popup)

    registry.select_tab("tab-1")
    assert registry.select_matching_page("newest_child") == "tab-3"
    assert registry.active_page is second_popup
    assert registry.select_matching_page("opener") == "tab-1"
    assert registry.select_matching_page("newest_popup") == "tab-3"


def test_ensure_page_creates_and_optionally_navigates_only_when_empty() -> None:
    context = _FakeContext()
    registry = BrowserPageRegistry(context)

    created = registry.ensure_page("https://example.test/recovered")
    existing = registry.ensure_page("https://example.test/not-used")

    assert created.created
    assert created.tab_id == "tab-1"
    assert context.pages[0].goto_calls == ["https://example.test/recovered"]
    assert context.pages[0].goto_options == [
        {"wait_until": "commit", "timeout": 30000}
    ]
    assert not existing.created
    assert existing.tab_id == "tab-1"


def test_ensure_page_reports_closed_browser_context() -> None:
    context = _FakeContext()
    context.new_page = lambda: (_ for _ in ()).throw(RuntimeError("closed"))  # type: ignore[method-assign]
    registry = BrowserPageRegistry(context)

    with pytest.raises(BrowserNoActivePageError, match="reconnect"):
        registry.ensure_page()
