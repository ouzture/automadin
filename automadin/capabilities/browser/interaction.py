from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from automadin.engine.browser_engine import BrowserSessionData
from automadin.models.browser_dom import BrowserDomScope
from automadin.models.locator import LocatorSpec, LocatorStrategy


@dataclass(frozen=True)
class ElementMetadata:
    """Small, serializable element summary safe to return across threads."""

    tag_name: str
    role: str = ""
    accessible_name: str = ""
    text: str = ""
    test_id: str = ""
    element_id: str = ""
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LocatorCandidate:
    locator: LocatorSpec
    count: int


@dataclass(frozen=True)
class ElementInspection:
    locator: LocatorSpec
    count: int
    metadata: ElementMetadata | None

    @property
    def unique(self) -> bool:
        return self.count == 1


@dataclass(frozen=True)
class PickedElement:
    metadata: ElementMetadata
    candidates: tuple[LocatorCandidate, ...]


@dataclass(frozen=True)
class ElementCollectionSnapshot:
    token: str
    count: int


_CHILD_TAB_POLL_INTERVAL_SECONDS = 0.05
_DEFAULT_CHILD_TAB_TIMEOUT_MS = 5000


def _strategy_value(strategy: LocatorStrategy | str) -> str:
    return strategy.value if isinstance(strategy, LocatorStrategy) else str(strategy)


def _direct_locator(page: Any, spec: LocatorSpec) -> Any:
    """Build one Playwright Locator without allowing it to escape the owner."""
    strategy = _strategy_value(spec.strategy)
    if strategy == "css":
        return page.locator(spec.value)
    if strategy == "xpath":
        value = spec.value or ""
        return page.locator(value if value.startswith("xpath=") else f"xpath={value}")
    if strategy == "test_id":
        return page.get_by_test_id(spec.value)
    if strategy == "text":
        return page.get_by_text(spec.value, exact=spec.exact)
    if strategy == "label":
        return page.get_by_label(spec.value, exact=spec.exact)
    if strategy == "placeholder":
        return page.get_by_placeholder(spec.value, exact=spec.exact)
    if strategy == "alt_text":
        return page.get_by_alt_text(spec.value, exact=spec.exact)
    if strategy == "title":
        return page.get_by_title(spec.value, exact=spec.exact)
    if strategy == "role":
        options: dict[str, Any] = {"exact": spec.exact}
        if spec.name:
            options["name"] = spec.name
        return page.get_by_role(spec.role, **options)
    raise ValueError(f"Unsupported browser locator strategy: {strategy}")


def _resolve_from_root(root: Any, locator: object) -> Any:
    """Resolve the first matching primary/fallback locator below one root."""
    spec = LocatorSpec.parse(locator)
    primary = _direct_locator(root, spec)
    if primary.count() > 0:
        return primary
    for fallback in spec.fallbacks:
        candidate = _direct_locator(root, fallback)
        if candidate.count() > 0:
            return candidate
    # Returning the primary lets Playwright produce its normal actionable error.
    return primary


def resolve_locator(
    page: Any,
    locator: object,
    *,
    scope: object = None,
) -> Any:
    """Resolve a locator on the page or below an optional container locator."""
    parsed_scope = BrowserDomScope.parse(scope)
    root = (
        page
        if parsed_scope.container is None
        else _resolve_from_root(page, parsed_scope.container)
    )
    return _resolve_from_root(root, locator)


def count_elements(
    session_data: BrowserSessionData,
    locator: object,
    *,
    scope: object = None,
) -> int:
    """Return the current number of matches inside an optional DOM scope."""
    return int(resolve_locator(session_data.active_page, locator, scope=scope).count())


def snapshot_elements(
    session_data: BrowserSessionData,
    locator: object,
    *,
    scope: object = None,
) -> ElementCollectionSnapshot:
    """Snapshot matching element handles while retaining them on the owner thread."""
    target = resolve_locator(session_data.active_page, locator, scope=scope)
    handles = tuple(target.element_handles())
    token = uuid4().hex
    session_data.element_snapshots[token] = (session_data.active_page, handles)
    return ElementCollectionSnapshot(token=token, count=len(handles))


def release_element_snapshot(session_data: BrowserSessionData, token: str) -> None:
    """Dispose an owner-side collection snapshot after its loop finishes."""
    stored = session_data.element_snapshots.pop(token, None)
    if stored is None:
        return
    _page, handles = stored
    for handle in handles:
        try:
            handle.dispose()
        except Exception:
            pass


def click_snapshot_element(
    session_data: BrowserSessionData,
    token: str,
    index: int,
    *,
    timeout_ms: int | None = None,
    anchor: str = "center",
    offset_x: float = 0,
    offset_y: float = 0,
    offset_unit: str = "pixels",
    require_new_child_tab: bool = False,
) -> None:
    """Click one element retained by a collection snapshot."""
    stored = session_data.element_snapshots.get(token)
    if stored is None:
        raise RuntimeError("Browser element collection snapshot is no longer available")
    page, handles = stored
    if page is not session_data.active_page:
        raise RuntimeError("Browser element collection belongs to a different active tab")
    if index < 0 or index >= len(handles):
        raise IndexError("Browser element collection index is out of range")
    before = _child_tab_baseline(session_data) if require_new_child_tab else None
    _click_target(
        page,
        handles[index],
        timeout_ms=timeout_ms,
        anchor=anchor,
        offset_x=offset_x,
        offset_y=offset_y,
        offset_unit=offset_unit,
    )
    if before is not None:
        _activate_new_child_tab(session_data, before, timeout_ms=timeout_ms)


def click_element(
    session_data: BrowserSessionData,
    locator: object,
    *,
    timeout_ms: int | None = None,
    anchor: str = "center",
    offset_x: float = 0,
    offset_y: float = 0,
    offset_unit: str = "pixels",
    require_new_child_tab: bool = False,
) -> None:
    """Click a structured locator on the active page."""
    target = resolve_locator(session_data.active_page, locator)
    before = _child_tab_baseline(session_data) if require_new_child_tab else None
    _click_target(
        session_data.active_page,
        target,
        timeout_ms=timeout_ms,
        anchor=anchor,
        offset_x=offset_x,
        offset_y=offset_y,
        offset_unit=offset_unit,
    )
    if before is not None:
        _activate_new_child_tab(session_data, before, timeout_ms=timeout_ms)


def _child_tab_baseline(session_data: BrowserSessionData) -> tuple[str, frozenset[str]]:
    snapshot = session_data.tabs_snapshot()
    if snapshot.active_tab_id is None:
        raise RuntimeError("Browser has no active opener tab")
    return snapshot.active_tab_id, frozenset(tab.tab_id for tab in snapshot.tabs)


def _activate_new_child_tab(
    session_data: BrowserSessionData,
    baseline: tuple[str, frozenset[str]],
    *,
    timeout_ms: int | None,
) -> None:
    """Select the popup created by the preceding click.

    Playwright can return from ``click`` before its browser-context ``page``
    event is delivered to the owner-thread tab registry.  Polling the immutable
    registry snapshot briefly avoids rejecting a genuine popup merely because
    registration is one event-loop turn behind the click.
    """
    opener_tab_id, previous_ids = baseline
    effective_timeout_ms = (
        _DEFAULT_CHILD_TAB_TIMEOUT_MS if timeout_ms is None else max(0, timeout_ms)
    )
    deadline = time.monotonic() + effective_timeout_ms / 1000.0
    while True:
        snapshot = session_data.tabs_snapshot()
        child = next(
            (
                tab
                for tab in reversed(snapshot.tabs)
                if tab.tab_id not in previous_ids
                and tab.opener_tab_id == opener_tab_id
            ),
            None,
        )
        if child is not None:
            session_data.select_tab(child.tab_id)
            return
        new_tabs = [tab for tab in snapshot.tabs if tab.tab_id not in previous_ids]
        if len(new_tabs) == 1:
            # Chromium occasionally exposes a popup through the context-level
            # page event without a usable opener relationship.  The click can
            # still be attributed safely when it is the only new page created
            # since the baseline.
            session_data.select_tab(new_tabs[0].tab_id)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "Click Element expected a new child tab, but none opened"
            )
        time.sleep(min(_CHILD_TAB_POLL_INTERVAL_SECONDS, remaining))


def _click_target(
    page: Any,
    target: Any,
    *,
    timeout_ms: int | None,
    anchor: str,
    offset_x: float,
    offset_y: float,
    offset_unit: str,
) -> None:
    if anchor == "center" and offset_x == 0 and offset_y == 0:
        # A click is an input action, not a navigation wait.  Flows that need
        # to wait for the resulting page use Wait For Navigation, which can
        # cooperatively poll automatic browser hooks while the page changes.
        # Leaving Playwright's implicit navigation wait enabled makes a
        # successful click look like a failed click and prevents those hooks
        # from running until the click timeout expires.
        options: dict[str, object] = {"no_wait_after": True}
        if timeout_ms is not None:
            options["timeout"] = timeout_ms
        target.click(**options)
        return
    anchors = {
        "center": (0.5, 0.5),
        "top_left": (0.0, 0.0),
        "top_right": (1.0, 0.0),
        "bottom_left": (0.0, 1.0),
        "bottom_right": (1.0, 1.0),
    }
    if anchor not in anchors:
        raise ValueError(f"Unsupported browser click anchor: {anchor}")
    if offset_unit not in {"pixels", "percent"}:
        raise ValueError("Browser click offset unit must be pixels or percent")
    options = {} if timeout_ms is None else {"timeout": timeout_ms}
    target.scroll_into_view_if_needed(**options)
    box = target.bounding_box()
    if box is None:
        raise RuntimeError("Browser element has no clickable bounding box")
    anchor_x, anchor_y = anchors[anchor]
    scale_x = box["width"] / 100.0 if offset_unit == "percent" else 1.0
    scale_y = box["height"] / 100.0 if offset_unit == "percent" else 1.0
    page.mouse.click(
        box["x"] + box["width"] * anchor_x + offset_x * scale_x,
        box["y"] + box["height"] * anchor_y + offset_y * scale_y,
    )


def wait_for_element(
    session_data: BrowserSessionData,
    locator: object,
    *,
    timeout_ms: int = 5000,
) -> None:
    """Wait until a structured locator is visible on the active page."""
    resolve_locator(session_data.active_page, locator).wait_for(
        state="visible",
        timeout=timeout_ms,
    )


def scroll_page(
    session_data: BrowserSessionData,
    *,
    delta_x: int = 0,
    delta_y: int = 600,
) -> None:
    """Scroll the active page by viewport pixel deltas."""
    session_data.active_page.mouse.wheel(delta_x, delta_y)


def scroll_element_into_view(
    session_data: BrowserSessionData,
    locator: object,
    *,
    timeout_ms: int = 5000,
) -> None:
    """Scroll the first element matched by a structured locator into view."""
    resolve_locator(session_data.active_page, locator).scroll_into_view_if_needed(
        timeout=timeout_ms
    )


def scroll_snapshot_element_into_view(
    session_data: BrowserSessionData,
    token: str,
    index: int,
    *,
    timeout_ms: int = 5000,
) -> None:
    """Scroll one retained collection element into view."""
    stored = session_data.element_snapshots.get(token)
    if stored is None:
        raise RuntimeError("Browser element collection snapshot is no longer available")
    page, handles = stored
    if page is not session_data.active_page:
        raise RuntimeError("Browser element collection belongs to a different active tab")
    if index < 0 or index >= len(handles):
        raise IndexError("Browser element collection index is out of range")
    handles[index].scroll_into_view_if_needed(timeout=timeout_ms)


def fill_element(
    session_data: BrowserSessionData,
    locator: object,
    value: str,
    min_delay: int = 0,
    max_delay: int = 0,
) -> None:
    """Fill a located field, optionally typing with a bounded random delay."""
    import random

    target = resolve_locator(session_data.active_page, locator)
    if min_delay <= 0 and max_delay <= 0:
        target.fill(value)
        return
    # Use Playwright's programmatic fill action to focus and clear the field.
    # A preparatory pointer click can be intercepted by an unrelated overlay
    # even though the input itself is visible and editable.
    target.fill("")
    for char in value:
        delay = (
            min_delay
            if min_delay == max_delay
            else random.randint(min_delay, max(min_delay, max_delay))
        )
        session_data.active_page.keyboard.type(char, delay=delay)


def extract_element_text(
    session_data: BrowserSessionData,
    locator: object,
) -> str:
    """Return text content from the first matching structured locator."""
    return resolve_locator(session_data.active_page, locator).text_content() or ""


def extract_snapshot_element_text(
    session_data: BrowserSessionData,
    token: str,
    index: int,
    *,
    child_selector: str = "",
) -> str:
    """Return text from one retained collection element or a CSS descendant.

    Collection handles preserve the exact element chosen when the loop began.
    A relative CSS selector lets a child command read one meaningful part of a
    card without rebuilding a position-dependent page locator.
    """
    stored = session_data.element_snapshots.get(token)
    if stored is None:
        raise RuntimeError("Browser element collection snapshot is no longer available")
    page, handles = stored
    if page is not session_data.active_page:
        raise RuntimeError("Browser element collection belongs to a different active tab")
    if index < 0 or index >= len(handles):
        raise IndexError("Browser element collection index is out of range")

    target = handles[index]
    if child_selector:
        target = target.query_selector(child_selector)
        if target is None:
            raise RuntimeError(
                "Extract Text could not find the child CSS selector "
                f"{child_selector!r} inside the current browser element"
            )
    return target.text_content() or ""


def assert_element_visible(
    session_data: BrowserSessionData,
    locator: object,
) -> None:
    """Assert that a structured locator resolves to a visible element."""
    spec = LocatorSpec.parse(locator)
    if not resolve_locator(session_data.active_page, spec).is_visible():
        raise AssertionError(f"Browser element is not visible: {spec.to_dict()}")


def is_element_visible(
    session_data: BrowserSessionData,
    locator: object,
) -> bool:
    """Probe visibility once across the active page and its child frames.

    Automatic browser hooks use this immediate, non-waiting probe while a host
    command is running.  Interruption pages such as Imperva's security check
    can place all visible content inside a cross-origin iframe, so limiting the
    probe to the top-level document makes a correctly configured hook
    impossible to match.  Playwright exposes cross-origin frames directly.

    Frames can detach at any point during navigation.  A failed child-frame
    probe therefore means only that the frame is no longer usable; failures in
    the active page probe are still allowed to surface normally.
    """
    spec = LocatorSpec.parse(locator)
    page = session_data.active_page
    if bool(resolve_locator(page, spec).is_visible()):
        return True

    main_frame = getattr(page, "main_frame", None)
    for frame in tuple(getattr(page, "frames", ())):
        if frame is main_frame:
            continue
        try:
            is_detached = getattr(frame, "is_detached", None)
            if callable(is_detached) and is_detached():
                continue
            if bool(resolve_locator(frame, spec).is_visible()):
                return True
        except PlaywrightError:
            # Navigation may detach a frame between enumeration and probing.
            continue
    return False


def inspect_element(
    session_data: BrowserSessionData,
    locator: object,
) -> ElementInspection:
    """Return match count and a bounded preview for a structured locator."""
    root = LocatorSpec.parse(locator)
    selected = root
    target = _direct_locator(session_data.active_page, root)
    count = target.count()
    if count == 0:
        for fallback in root.fallbacks:
            fallback_target = _direct_locator(session_data.active_page, fallback)
            fallback_count = fallback_target.count()
            if fallback_count:
                selected, target, count = fallback, fallback_target, fallback_count
                break
    metadata = _metadata(target.first.evaluate(_ELEMENT_METADATA_JS)) if count else None
    return ElementInspection(locator=selected, count=count, metadata=metadata)


def start_element_picker(session_data: BrowserSessionData) -> None:
    """Install a click-to-pick overlay on the active page."""
    session_data.active_page.evaluate(_START_PICKER_JS)


def poll_element_picker(session_data: BrowserSessionData) -> PickedElement | None:
    """Read a picker result without blocking the browser owner thread."""
    raw = session_data.active_page.evaluate(_POLL_PICKER_JS)
    return _picked_element(session_data.active_page, raw)


def stop_element_picker(session_data: BrowserSessionData) -> PickedElement | None:
    """Remove the picker and return its last result, if any."""
    page = session_data.active_page
    raw = page.evaluate(_STOP_PICKER_JS)
    return _picked_element(page, raw)


def _picked_element(page: Any, raw: object) -> PickedElement | None:
    if not isinstance(raw, Mapping):
        return None
    raw_metadata = raw.get("metadata")
    if not isinstance(raw_metadata, Mapping):
        return None
    candidates: list[LocatorCandidate] = []
    raw_candidates = raw.get("candidates", ())
    if isinstance(raw_candidates, list):
        for value in raw_candidates:
            try:
                spec = LocatorSpec.parse(value)
                count = int(_direct_locator(page, spec).count())
            except (TypeError, ValueError):
                continue
            candidates.append(LocatorCandidate(locator=spec, count=count))
    return PickedElement(
        metadata=_metadata(raw_metadata),
        candidates=tuple(candidates),
    )


def _metadata(raw: Mapping[object, object]) -> ElementMetadata:
    attributes = raw.get("attributes")
    attribute_pairs = (
        tuple(
            (str(key), str(value))
            for key, value in attributes.items()
            if value is not None
        )
        if isinstance(attributes, Mapping)
        else ()
    )
    return ElementMetadata(
        tag_name=str(raw.get("tagName") or ""),
        role=str(raw.get("role") or ""),
        accessible_name=str(raw.get("accessibleName") or ""),
        text=str(raw.get("text") or "")[:200],
        test_id=str(raw.get("testId") or ""),
        element_id=str(raw.get("elementId") or ""),
        attributes=attribute_pairs,
    )


_ELEMENT_METADATA_JS = """
element => {
  const text = (element.innerText || element.textContent || '').trim();
  const labelled = element.getAttribute('aria-label') || element.getAttribute('alt') ||
    element.getAttribute('title') || '';
  const attributes = {};
  for (const name of ['name', 'type', 'href', 'placeholder', 'aria-label']) {
    const value = element.getAttribute(name);
    if (value !== null) attributes[name] = value.slice(0, 200);
  }
  return {
    tagName: element.tagName.toLowerCase(),
    role: element.getAttribute('role') || '',
    accessibleName: labelled || text.slice(0, 200),
    text: text.slice(0, 200),
    testId: element.getAttribute('data-testid') || element.getAttribute('data-test') || '',
    elementId: element.id || '',
    attributes
  };
}
"""


_START_PICKER_JS = """
() => {
  const key = '__automadinElementPicker';
  const old = window[key];
  if (old && old.cleanup) old.cleanup();
  const overlay = document.createElement('div');
  overlay.setAttribute('data-automadin-picker-overlay', '');
  Object.assign(overlay.style, {
    position: 'fixed', pointerEvents: 'none', zIndex: '2147483647',
    border: '2px solid #4f8cff', background: 'rgba(79,140,255,.12)',
    display: 'none', boxSizing: 'border-box'
  });
  document.documentElement.appendChild(overlay);
  const implicitRole = element => {
    const tag = element.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && element.hasAttribute('href')) return 'link';
    if (tag === 'input') return element.type === 'checkbox' ? 'checkbox' :
      element.type === 'radio' ? 'radio' : 'textbox';
    if (tag === 'select') return 'combobox';
    return element.getAttribute('role') || '';
  };
  const css = element => {
    if (element.id) return '#' + CSS.escape(element.id);
    const parts = [];
    for (let node = element; node && node.nodeType === 1 && parts.length < 4;
         node = node.parentElement) {
      let part = node.tagName.toLowerCase();
      const siblings = node.parentElement ?
        Array.from(node.parentElement.children).filter(x => x.tagName === node.tagName) : [];
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      parts.unshift(part);
    }
    return parts.join(' > ');
  };
  const describe = element => {
    const text = (element.innerText || element.textContent || '').trim().slice(0, 200);
    const testId = element.getAttribute('data-testid') || element.getAttribute('data-test') || '';
    const role = implicitRole(element);
    const name = (element.getAttribute('aria-label') || element.getAttribute('alt') ||
      element.getAttribute('title') || text).slice(0, 200);
    const candidates = [];
    if (testId) candidates.push({platform:'browser', strategy:'test_id', value:testId});
    if (role && name) candidates.push({platform:'browser', strategy:'role', role, name, exact:true});
    if (role) candidates.push({platform:'browser', strategy:'role', role});
    if (element.labels && element.labels[0]) {
      const label = (element.labels[0].innerText || '').trim();
      if (label) candidates.push({platform:'browser', strategy:'label', value:label, exact:true});
    }
    const placeholder = element.getAttribute('placeholder');
    if (placeholder) candidates.push({platform:'browser', strategy:'placeholder', value:placeholder, exact:true});
    if (element.id) candidates.push({platform:'browser', strategy:'css', value:'#' + CSS.escape(element.id)});
    const href = element.getAttribute('href');
    if (href) candidates.push({platform:'browser', strategy:'css', value:`${element.tagName.toLowerCase()}[href=${JSON.stringify(href)}]`});
    const classes = Array.from(element.classList).slice(0, 3);
    if (classes.length) candidates.push({platform:'browser', strategy:'css', value:classes.map(x => '.' + CSS.escape(x)).join('')});
    if (text) candidates.push({platform:'browser', strategy:'text', value:text, exact:true});
    candidates.push({platform:'browser', strategy:'css', value:css(element)});
    return {metadata: {
      tagName: element.tagName.toLowerCase(), role, accessibleName:name, text,
      testId, elementId:element.id || '', attributes:{}
    }, candidates};
  };
  const move = event => {
    const element = event.target;
    if (!(element instanceof Element) || element === overlay) return;
    const rect = element.getBoundingClientRect();
    Object.assign(overlay.style, {display:'block', left:`${rect.left}px`, top:`${rect.top}px`,
      width:`${rect.width}px`, height:`${rect.height}px`});
  };
  const click = event => {
    const element = event.target;
    if (!(element instanceof Element) || element === overlay) return;
    event.preventDefault(); event.stopImmediatePropagation();
    state.selected = describe(element); state.cleanup();
  };
  const state = {selected:null, cleanup() {
    document.removeEventListener('mousemove', move, true);
    document.removeEventListener('click', click, true);
    overlay.remove();
  }};
  window[key] = state;
  document.addEventListener('mousemove', move, true);
  document.addEventListener('click', click, true);
}
"""

_POLL_PICKER_JS = "() => window.__automadinElementPicker?.selected || null"
_STOP_PICKER_JS = """
() => {
  const state = window.__automadinElementPicker;
  if (!state) return null;
  state.cleanup();
  delete window.__automadinElementPicker;
  return state.selected;
}
"""


def click_position(session_data: BrowserSessionData, x: float, y: float) -> None:
    """Click at normalized coordinates (0.0-1.0) within the viewport."""
    page = session_data.active_page
    viewport = page.viewport_size
    px_x = int(x * viewport["width"])
    px_y = int(y * viewport["height"])
    page.mouse.click(px_x, px_y)


def type_text(
    session_data: BrowserSessionData,
    text: str,
    min_delay: int = 0,
    max_delay: int = 0,
) -> None:
    """Type text using keyboard input, optionally char-by-char with random delays."""
    import random

    page = session_data.active_page
    if min_delay <= 0 and max_delay <= 0:
        page.keyboard.type(text)
    else:
        for ch in text:
            delay = (
                min_delay
                if min_delay == max_delay
                else random.randint(min_delay, max(min_delay, max_delay))
            )
            page.keyboard.type(ch, delay=delay)



def clear_text_field(session_data: BrowserSessionData, selector: str) -> None:
    """Clear an input field matched by CSS selector."""
    page = session_data.active_page
    page.fill(selector, "")


def input_fill(
    session_data: BrowserSessionData,
    selector: object,
    value: str,
    min_delay: int = 0,
    max_delay: int = 0,
) -> None:
    """Compatibility wrapper for legacy CSS and structured locator values."""
    fill_element(session_data, selector, value, min_delay, max_delay)



def extract_text(session_data: BrowserSessionData, selector: object) -> str:
    """Compatibility wrapper for legacy CSS and structured locator values."""
    return extract_element_text(session_data, selector)


def evaluate_js(session_data: BrowserSessionData, script: str) -> str:
    """Evaluate JavaScript in the page and return the result as a string."""
    page = session_data.active_page
    result = page.evaluate(script)
    return str(result) if result is not None else ""


def take_screenshot(session_data: BrowserSessionData) -> bytes:
    """Capture the active browser viewport and return PNG bytes."""
    page = session_data.active_page
    return page.screenshot()
