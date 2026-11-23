from __future__ import annotations

import threading
from types import SimpleNamespace

from playwright.sync_api import Error as PlaywrightError

from automadin.capabilities.browser import interaction
from automadin.capabilities.browser.interaction import (
    ElementInspection,
    PickedElement,
    assert_element_visible,
    click_element,
    count_elements,
    extract_element_text,
    extract_snapshot_element_text,
    fill_element,
    inspect_element,
    is_element_visible,
    poll_element_picker,
    release_element_snapshot,
    scroll_element_into_view,
    scroll_page,
    scroll_snapshot_element_into_view,
    snapshot_elements,
    click_snapshot_element,
    wait_for_element,
)
from automadin.engine.browser_session import BrowserSessionHandle
from automadin.engine.browser_pages import BrowserTabSnapshot, BrowserTabsSnapshot
from automadin.models.device import BrowserDevice, BrowserDeviceConfig, BrowserEngine


class _Locator:
    def __init__(self, count: int, metadata: dict[str, object] | None = None) -> None:
        self._count = count
        self._metadata = metadata or {}
        self.click_options: list[dict[str, object]] = []
        self.wait_options: list[dict[str, object]] = []
        self.fill_values: list[str] = []
        self.text = ""
        self.visible = count > 0
        self.handles: list[_ElementHandle] = []
        self.box: dict[str, float] | None = None
        self.scroll_options: list[dict[str, object]] = []
        self.on_click = None

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> "_Locator":
        return self

    def click(self, **options: object) -> None:
        self.click_options.append(options)
        if self.on_click is not None:
            self.on_click()

    def wait_for(self, **options: object) -> None:
        self.wait_options.append(options)

    def fill(self, value: str) -> None:
        self.fill_values.append(value)

    def text_content(self) -> str:
        return self.text

    def is_visible(self) -> bool:
        return self.visible

    def evaluate(self, _script: str) -> dict[str, object]:
        return self._metadata

    def element_handles(self) -> list["_ElementHandle"]:
        return list(self.handles)

    def scroll_into_view_if_needed(self, **options: object) -> None:
        self.scroll_options.append(options)

    def bounding_box(self) -> dict[str, float] | None:
        return self.box

    def locator(self, value: str) -> "_Locator":
        return self._get("locator", value)

    def _get(self, method: str, value: object, **options: object) -> "_Locator":
        nested = getattr(self, "nested", {})
        return nested.get((method, value), _Locator(0))


class _Page:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict[str, object]]] = []
        self.locators: dict[tuple[str, object], _Locator] = {}
        self.evaluate_result: object = None
        self.main_frame = self
        self.frames: list[object] = [self]
        self.keyboard_types: list[tuple[str, int]] = []
        self.keyboard = SimpleNamespace(
            type=lambda value, *, delay: self.keyboard_types.append((value, delay))
        )
        self.mouse_clicks: list[tuple[float, float]] = []
        self.mouse_wheels: list[tuple[int, int]] = []
        self.mouse = SimpleNamespace(
            click=lambda x, y: self.mouse_clicks.append((x, y)),
            wheel=lambda x, y: self.mouse_wheels.append((x, y)),
        )

    def _get(self, method: str, value: object, **options: object) -> _Locator:
        self.calls.append((method, value, options))
        return self.locators.get((method, value), _Locator(0))

    def locator(self, value: str) -> _Locator:
        return self._get("locator", value)

    def get_by_test_id(self, value: str) -> _Locator:
        return self._get("test_id", value)

    def get_by_text(self, value: str, **options: object) -> _Locator:
        return self._get("text", value, **options)

    def get_by_label(self, value: str, **options: object) -> _Locator:
        return self._get("label", value, **options)

    def get_by_placeholder(self, value: str, **options: object) -> _Locator:
        return self._get("placeholder", value, **options)

    def get_by_alt_text(self, value: str, **options: object) -> _Locator:
        return self._get("alt_text", value, **options)

    def get_by_title(self, value: str, **options: object) -> _Locator:
        return self._get("title", value, **options)

    def get_by_role(self, value: str, **options: object) -> _Locator:
        return self._get("role", value, **options)

    def evaluate(self, _script: str) -> object:
        return self.evaluate_result


class _DetachedFrame(_Page):
    def is_detached(self) -> bool:
        return True

    def get_by_text(self, value: str, **options: object) -> _Locator:
        raise AssertionError("A detached frame must not be queried")


class _DetachingFrame(_Page):
    def is_detached(self) -> bool:
        return False

    def get_by_text(self, value: str, **options: object) -> _Locator:
        raise PlaywrightError("Frame was detached")


class _ElementHandle:
    def __init__(self) -> None:
        self.click_options: list[dict[str, object]] = []
        self.disposed = False
        self.text = ""
        self.children: dict[str, _ElementHandle] = {}
        self.scroll_options: list[dict[str, object]] = []

    def click(self, **options: object) -> None:
        self.click_options.append(options)

    def dispose(self) -> None:
        self.disposed = True

    def scroll_into_view_if_needed(self, **options: object) -> None:
        self.scroll_options.append(options)

    def query_selector(self, selector: str) -> "_ElementHandle | None":
        return self.children.get(selector)

    def text_content(self) -> str:
        return self.text


def test_click_uses_first_matching_fallback_and_timeout() -> None:
    page = _Page()
    target = _Locator(1)
    page.locators[("test_id", "save")] = target
    session = SimpleNamespace(active_page=page)

    click_element(
        session,
        {
            "platform": "browser",
            "strategy": "css",
            "value": ".missing",
            "fallbacks": [{"strategy": "test_id", "value": "save"}],
        },
        timeout_ms=1250,
    )

    assert target.click_options == [{"no_wait_after": True, "timeout": 1250}]


def test_click_does_not_wait_for_navigation_started_by_the_element() -> None:
    page = _Page()
    target = _Locator(1)
    page.locators[("locator", "#submit")] = target

    click_element(SimpleNamespace(active_page=page), "#submit", timeout_ms=5000)

    assert target.click_options == [
        {"no_wait_after": True, "timeout": 5000}
    ]


def test_click_offset_uses_element_bounds_and_can_reach_outside_element() -> None:
    page = _Page()
    target = _Locator(1)
    target.box = {"x": 100.0, "y": 50.0, "width": 200.0, "height": 80.0}
    page.locators[("locator", ".card")] = target

    click_element(
        SimpleNamespace(active_page=page),
        ".card",
        timeout_ms=800,
        anchor="top_right",
        offset_x=10,
        offset_y=50,
        offset_unit="percent",
    )

    assert target.scroll_options == [{"timeout": 800}]
    assert page.mouse_clicks == [(320.0, 90.0)]


def test_click_can_require_and_activate_a_new_child_tab() -> None:
    page = _Page()
    target = _Locator(1)
    page.locators[("locator", "a.details")] = target
    snapshots = [
        BrowserTabsSnapshot(
            tabs=(BrowserTabSnapshot("tab-1", 0, "", "", True, None),),
            active_tab_id="tab-1",
        )
    ]
    selected: list[str] = []
    session = SimpleNamespace(
        active_page=page,
        tabs_snapshot=lambda: snapshots[-1],
        select_tab=selected.append,
    )

    def open_child() -> None:
        snapshots.append(
            BrowserTabsSnapshot(
                tabs=(
                    BrowserTabSnapshot("tab-1", 0, "", "", True, None),
                    BrowserTabSnapshot("tab-2", 1, "", "", False, "tab-1"),
                ),
                active_tab_id="tab-1",
            )
        )

    target.on_click = open_child
    click_element(session, "a.details", require_new_child_tab=True)

    assert selected == ["tab-2"]


def test_click_waits_for_delayed_child_tab_registration(monkeypatch) -> None:
    page = _Page()
    target = _Locator(1)
    page.locators[("locator", "a.details")] = target
    baseline = BrowserTabsSnapshot(
        tabs=(BrowserTabSnapshot("tab-1", 0, "", "", True, None),),
        active_tab_id="tab-1",
    )
    child_snapshot = BrowserTabsSnapshot(
        tabs=(
            BrowserTabSnapshot("tab-1", 0, "", "", True, None),
            BrowserTabSnapshot("tab-2", 1, "", "", False, "tab-1"),
        ),
        active_tab_id="tab-1",
    )
    calls = 0
    selected: list[str] = []

    def tabs_snapshot() -> BrowserTabsSnapshot:
        nonlocal calls
        calls += 1
        return child_snapshot if calls >= 3 else baseline

    session = SimpleNamespace(
        active_page=page,
        tabs_snapshot=tabs_snapshot,
        select_tab=selected.append,
    )
    monkeypatch.setattr(interaction, "_CHILD_TAB_POLL_INTERVAL_SECONDS", 0)

    click_element(session, "a.details", require_new_child_tab=True, timeout_ms=50)

    assert calls >= 3
    assert selected == ["tab-2"]


def test_click_activates_the_only_new_tab_when_opener_is_unavailable() -> None:
    page = _Page()
    target = _Locator(1)
    page.locators[("locator", "a.details")] = target
    snapshots = [
        BrowserTabsSnapshot(
            tabs=(BrowserTabSnapshot("tab-1", 0, "", "", True, None),),
            active_tab_id="tab-1",
        )
    ]
    selected: list[str] = []
    session = SimpleNamespace(
        active_page=page,
        tabs_snapshot=lambda: snapshots[-1],
        select_tab=selected.append,
    )

    def open_unrelated_popup_event() -> None:
        snapshots.append(
            BrowserTabsSnapshot(
                tabs=(
                    BrowserTabSnapshot("tab-1", 0, "", "", True, None),
                    BrowserTabSnapshot("tab-2", 1, "", "", False, None),
                ),
                active_tab_id="tab-1",
            )
        )

    target.on_click = open_unrelated_popup_event
    click_element(session, "a.details", require_new_child_tab=True)

    assert selected == ["tab-2"]


def test_locator_operations_share_fallback_resolution() -> None:
    page = _Page()
    target = _Locator(1)
    target.text = "Saved"
    page.locators[("role", "status")] = target
    session = SimpleNamespace(active_page=page)
    locator = {
        "strategy": "css",
        "value": ".missing",
        "fallbacks": [{"strategy": "role", "role": "status"}],
    }

    wait_for_element(session, locator, timeout_ms=900)
    fill_element(session, locator, "value")

    assert extract_element_text(session, locator) == "Saved"
    assert_element_visible(session, locator)
    assert target.wait_options == [{"state": "visible", "timeout": 900}]
    assert target.fill_values == ["value"]


def test_immediate_visibility_probe_finds_element_in_child_frame() -> None:
    page = _Page()
    frame = _Page()
    target = _Locator(1)
    frame.locators[("text", "Additional security check is required")] = target
    page.frames.append(frame)

    visible = is_element_visible(
        SimpleNamespace(active_page=page),
        {
            "strategy": "text",
            "value": "Additional security check is required",
        },
    )

    assert visible
    assert page.calls == [
        ("text", "Additional security check is required", {"exact": False})
    ]
    assert frame.calls == [
        ("text", "Additional security check is required", {"exact": False})
    ]


def test_immediate_visibility_probe_skips_detached_and_detaching_frames() -> None:
    page = _Page()
    matching_frame = _Page()
    matching_frame.locators[("text", "Captcha")] = _Locator(1)
    page.frames.extend([_DetachedFrame(), _DetachingFrame(), matching_frame])

    assert is_element_visible(
        SimpleNamespace(active_page=page),
        {"strategy": "text", "value": "Captcha"},
    )


def test_delayed_fill_focuses_without_pointer_click_that_an_overlay_can_block() -> None:
    page = _Page()
    target = _Locator(1)
    page.locators[("locator", "#name")] = target

    def blocked_pointer_click() -> None:
        raise AssertionError("The page overlay intercepted the pointer click")

    target.on_click = blocked_pointer_click

    fill_element(
        SimpleNamespace(active_page=page),
        "#name",
        "A",
        min_delay=1,
        max_delay=1,
    )

    assert target.click_options == []
    assert target.fill_values == [""]
    assert page.keyboard_types == [("A", 1)]


def test_scroll_supports_page_locator_and_snapshot_targets() -> None:
    page = _Page()
    target = _Locator(1)
    retained = _ElementHandle()
    page.locators[("locator", "#details")] = target
    session = SimpleNamespace(
        active_page=page,
        element_snapshots={"snapshot": (page, (retained,))},
    )

    scroll_page(session, delta_x=25, delta_y=-450)
    scroll_element_into_view(session, "#details", timeout_ms=900)
    scroll_snapshot_element_into_view(session, "snapshot", 0, timeout_ms=750)

    assert page.mouse_wheels == [(25, -450)]
    assert target.scroll_options == [{"timeout": 900}]
    assert retained.scroll_options == [{"timeout": 750}]


def test_count_elements_narrows_target_to_container_scope() -> None:
    page = _Page()
    container = _Locator(1)
    target = _Locator(4)
    container.nested = {("locator", ".row"): target}
    page.locators[("locator", "#results")] = container

    count = count_elements(
        SimpleNamespace(active_page=page),
        ".row",
        scope="#results",
    )

    assert count == 4


def test_element_snapshot_retains_owner_side_handles_until_release() -> None:
    page = _Page()
    target = _Locator(2)
    target.handles = [_ElementHandle(), _ElementHandle()]
    page.locators[("locator", ".row")] = target
    session = SimpleNamespace(active_page=page, element_snapshots={})

    snapshot = snapshot_elements(session, ".row")
    click_snapshot_element(session, snapshot.token, 1, timeout_ms=750)
    release_element_snapshot(session, snapshot.token)

    assert snapshot.count == 2
    assert target.handles[0].disposed
    assert target.handles[1].click_options == [
        {"no_wait_after": True, "timeout": 750}
    ]
    assert target.handles[1].disposed
    assert session.element_snapshots == {}


def test_extract_snapshot_element_text_reads_a_relative_child_selector() -> None:
    page = _Page()
    card = _ElementHandle()
    description = _ElementHandle()
    description.text = "Search on Bing to find flights"
    card.children["p.card-description"] = description
    session = SimpleNamespace(active_page=page, element_snapshots={"snapshot": (page, (card,))})

    assert extract_snapshot_element_text(
        session,
        "snapshot",
        0,
        child_selector="p.card-description",
    ) == "Search on Bing to find flights"


def test_visibility_assertion_reports_structured_locator() -> None:
    page = _Page()
    target = _Locator(1)
    target.visible = False
    page.locators[("test_id", "save")] = target

    try:
        assert_element_visible(
            SimpleNamespace(active_page=page),
            {"strategy": "test_id", "value": "save"},
        )
    except AssertionError as exc:
        assert "test_id" in str(exc)
    else:
        raise AssertionError("Expected an invisible element assertion")


def test_inspection_is_bounded_and_reports_uniqueness() -> None:
    page = _Page()
    page.locators[("role", "button")] = _Locator(
        1,
        {
            "tagName": "button",
            "role": "button",
            "accessibleName": "Save",
            "text": "Save" * 100,
            "attributes": {"type": "submit"},
        },
    )
    result = inspect_element(
        SimpleNamespace(active_page=page),
        {"strategy": "role", "role": "button", "name": "Save", "exact": True},
    )

    assert result.unique
    assert result.metadata is not None
    assert result.metadata.tag_name == "button"
    assert len(result.metadata.text) == 200
    assert result.metadata.attributes == (("type", "submit"),)


def test_picker_result_contains_json_safe_metadata_and_candidate_counts() -> None:
    page = _Page()
    page.locators[("test_id", "save")] = _Locator(1)
    page.evaluate_result = {
        "metadata": {"tagName": "button", "text": "Save", "testId": "save"},
        "candidates": [
            {"platform": "browser", "strategy": "test_id", "value": "save"},
            {"platform": "browser", "strategy": "css", "value": "button"},
        ],
    }

    result = poll_element_picker(SimpleNamespace(active_page=page))

    assert isinstance(result, PickedElement)
    assert [candidate.count for candidate in result.candidates] == [1, 0]
    assert result.metadata.test_id == "save"


class _Engine:
    def __init__(self, data: object) -> None:
        self.data = data
        self.owner_thread: int | None = None

    def connect(self, _device: object, *, profile_lease: object = None) -> object:
        self.owner_thread = threading.get_ident()
        return self.data

    def disconnect(self, _data: object) -> None:
        pass


def test_handle_keeps_element_operations_on_owner_thread(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    inspection = ElementInspection(
        locator=__import__("automadin.models.locator", fromlist=["LocatorSpec"]).LocatorSpec.parse("#save"),
        count=1,
        metadata=None,
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.click_element",
        lambda *_args, **_kwargs: calls.append(("click", threading.get_ident())),
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.inspect_element",
        lambda *_args: (calls.append(("inspect", threading.get_ident())), inspection)[1],
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.wait_for_element",
        lambda *_args, **_kwargs: calls.append(("wait", threading.get_ident())),
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.fill_element",
        lambda *_args, **_kwargs: calls.append(("fill", threading.get_ident())),
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.extract_element_text",
        lambda *_args: (calls.append(("extract", threading.get_ident())), "Saved")[1],
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.extract_snapshot_element_text",
        lambda *_args, **_kwargs: (
            calls.append(("extract_snapshot", threading.get_ident())),
            "Card description",
        )[1],
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.assert_element_visible",
        lambda *_args: calls.append(("assert", threading.get_ident())),
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.scroll_page",
        lambda *_args, **_kwargs: calls.append(("scroll_page", threading.get_ident())),
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.scroll_element_into_view",
        lambda *_args, **_kwargs: calls.append(("scroll_locator", threading.get_ident())),
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.scroll_snapshot_element_into_view",
        lambda *_args, **_kwargs: calls.append(("scroll_snapshot", threading.get_ident())),
    )
    monkeypatch.setattr(
        "automadin.engine.browser_session.count_elements",
        lambda *_args, **_kwargs: (
            calls.append(("count", threading.get_ident())),
            3,
        )[1],
    )
    engine = _Engine(SimpleNamespace())
    device = BrowserDevice(config=BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM))
    handle = BrowserSessionHandle(device, engine)  # type: ignore[arg-type]
    try:
        handle.click_locator("#save")
        handle.wait_for_locator("#save")
        handle.fill_locator("#save", "value")
        assert handle.extract_locator_text("#save") == "Saved"
        assert (
            handle.extract_snapshot_locator_text(
                "snapshot",
                0,
                child_selector="p.description",
            )
            == "Card description"
        )
        handle.assert_locator_visible("#save")
        handle.scroll_page(delta_x=10, delta_y=20)
        handle.scroll_locator_into_view("#save")
        handle.scroll_snapshot_locator_into_view("snapshot", 0)
        assert handle.inspect_locator("#save") is inspection
        assert handle.count_locators(".row", scope="#results") == 3
    finally:
        handle.close()

    assert engine.owner_thread is not None
    assert engine.owner_thread != threading.get_ident()
    assert calls == [
        ("click", engine.owner_thread),
        ("wait", engine.owner_thread),
        ("fill", engine.owner_thread),
        ("extract", engine.owner_thread),
        ("extract_snapshot", engine.owner_thread),
        ("assert", engine.owner_thread),
        ("scroll_page", engine.owner_thread),
        ("scroll_locator", engine.owner_thread),
        ("scroll_snapshot", engine.owner_thread),
        ("inspect", engine.owner_thread),
        ("count", engine.owner_thread),
    ]
