"""Browser automation port backed by the explicitly granted extension tab."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import time
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from automadin.capabilities.browser.interaction import ElementCollectionSnapshot
from automadin.engine.browser_pages import BrowserTabSnapshot, BrowserTabsSnapshot
from automadin.engine.browser_pages import BrowserPageEnsureResult, BrowserTabCloseResult
from automadin.engine.windows_input import drag_window
from automadin.services.browser_companion_broker import (
    BrowserCompanionBroker,
    CompanionTarget,
)

def _renderer_window(parent: int) -> int:
    if not parent or not ctypes.windll.user32.IsWindow(parent):
        raise RuntimeError("The companion browser window is no longer available")
    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd: int, _param: int) -> bool:
        buffer = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buffer, len(buffer))
        if buffer.value == "Chrome_RenderWidgetHostHWND" and ctypes.windll.user32.IsWindowVisible(
            hwnd
        ):
            matches.append(int(hwnd))
        return True

    ctypes.windll.user32.EnumChildWindows(parent, callback, 0)
    if not matches:
        raise RuntimeError("Could not locate the active browser viewport window")
    return matches[-1]


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _matching_browser_windows(title: str) -> list[int]:
    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd: int, _param: int) -> bool:
        value = int(hwnd)
        if (
            ctypes.windll.user32.IsWindowVisible(value)
            and _window_class(value) == "Chrome_WidgetWin_1"
            and (not title or title in _window_title(value))
        ):
            matches.append(value)
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return matches


def _renderer_point_to_parent(parent: int, renderer: int, x: int, y: int) -> tuple[int, int]:
    point = ctypes.wintypes.POINT(int(x), int(y))
    if not ctypes.windll.user32.ClientToScreen(renderer, ctypes.byref(point)):
        raise RuntimeError("Could not resolve the browser viewport position")
    if not ctypes.windll.user32.ScreenToClient(parent, ctypes.byref(point)):
        raise RuntimeError("Could not resolve the browser window position")
    return int(point.x), int(point.y)


def _window_title(hwnd: int) -> str:
    length = int(ctypes.windll.user32.GetWindowTextLengthW(hwnd) or 0)
    buffer = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


class CompanionBrowserPort:
    def __init__(self, broker: BrowserCompanionBroker, target: CompanionTarget) -> None:
        self._broker = broker
        self._target = target
        self._pending_load_epoch = 0

    def _request(self, operation: str, **params: Any) -> Any:
        return self._broker.request(self._target.client_id, operation, params)

    def _browser_window(self) -> int:
        if self._target.parent_window:
            if not ctypes.windll.user32.IsWindow(self._target.parent_window):
                raise RuntimeError("The companion browser window is no longer available")
            return self._target.parent_window

        tab = self._request("tab_info")
        if not bool(tab.get("active")):
            raise RuntimeError("Select the granted browser tab before native input")
        title = str(tab.get("title") or "").strip()
        matches = _matching_browser_windows(title)
        if len(matches) == 1:
            return matches[0]
        raise RuntimeError("Could not uniquely locate the window containing the granted browser tab")

    def close(self) -> None:
        return None

    def screenshot(self) -> bytes:
        data = str(self._request("screenshot"))
        return base64.b64decode(data.partition(",")[2], validate=True)

    def viewport_size(self) -> tuple[int, int]:
        result = self._request("viewport")
        return int(result["width"]), int(result["height"])

    def navigate_to(self, url: str, *, timeout_ms: int = 30000) -> None:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self._pending_load_epoch = int(self._request("navigate", url=url) or 0)

    def go_back(self, *, timeout_ms: int = 30000) -> None:
        self._pending_load_epoch = int(self._request("back") or 0)

    def go_forward(self, *, timeout_ms: int = 30000) -> None:
        self._pending_load_epoch = int(self._request("forward") or 0)

    def reload_page(self, *, timeout_ms: int = 30000) -> None:
        self._pending_load_epoch = int(self._request("reload") or 0)

    def wait_for_load_state(self, state: str, *, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            result = self._request("load_status", epoch=self._pending_load_epoch)
            if bool(result.get("complete")):
                self._request("viewport")
                self._pending_load_epoch = 0
                return
            time.sleep(0.05)
        raise PlaywrightTimeoutError(f"Browser companion did not reach {state}")

    def tabs_snapshot(self) -> BrowserTabsSnapshot:
        result = self._request("tabs_snapshot")
        active = str(result.get("active_target_id") or "")
        tabs = tuple(
            BrowserTabSnapshot(
                str(item["target_id"]),
                index,
                str(item.get("url") or ""),
                str(item.get("title") or ""),
                str(item["target_id"]) == active,
                None,
            )
            for index, item in enumerate(result.get("tabs", []))
        )
        return BrowserTabsSnapshot(tabs, active or None)

    def select_tab(self, tab_id: str) -> None:
        self._request("select_tab", target_id=tab_id)

    def open_new_tab(self, url: str | None = None, *, timeout_ms: int = 30000) -> str:
        return str(self._request("open_tab", url=url or "about:blank"))

    def close_tab(self) -> BrowserTabCloseResult | None:
        result = self._request("close_tab")
        if result is None:
            return None
        return BrowserTabCloseResult(
            str(result["closed_target_id"]), bool(result["opened_replacement"])
        )

    def close_all_tabs(self) -> str:
        return str(self._request("close_all_tabs"))

    def switch_tab(self, index: int) -> str:
        return str(self._request("switch_tab", index=index))

    def select_page(self, strategy: str, value: str = "", *, match_case: bool = False) -> str:
        return str(
            self._request("select_page", strategy=strategy, value=value, match_case=match_case)
        )

    def ensure_page(
        self, url: str | None = None, *, timeout_ms: int = 30000
    ) -> BrowserPageEnsureResult:
        result = self._request("ensure_page", url=url or "about:blank")
        return BrowserPageEnsureResult(str(result["target_id"]), bool(result["created"]))

    def current_url(self) -> str:
        return str(self._request("tab_info").get("url") or "")

    def evaluate_js(self, script: str) -> str:
        return str(self._request("evaluate", script=script))

    def is_locator_visible(self, locator: object) -> bool:
        try:
            return bool(self._request("visible", locator=locator))
        except RuntimeError as exc:
            # During a document swap Chrome has removed the old content-script
            # receiver but has not yet emitted the final load event that causes
            # reinjection. Hook polling must treat that brief gap as no match.
            if "Receiving end does not exist" in str(exc):
                return False
            raise

    def assert_locator_visible(self, locator: object) -> None:
        if not self.is_locator_visible(locator):
            raise AssertionError("Browser element is not visible")

    def wait_for_locator(self, locator: object, *, timeout_ms: int = 5000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.is_locator_visible(locator):
                return
            time.sleep(0.1)
        raise TimeoutError("Timed out waiting for browser element")

    def extract_locator_text(self, locator: object) -> str:
        return str(self._request("extract", locator=locator))

    def count_locators(self, locator: object, *, scope: object = None) -> int:
        if scope not in (None, {}):
            raise RuntimeError("Companion container scope is not implemented")
        return int(self._request("count", locator=locator))

    def scroll_page(self, *, delta_x: int = 0, delta_y: int = 600) -> None:
        self._request("scroll_page", delta_x=delta_x, delta_y=delta_y)

    def scroll_locator_into_view(self, locator: object, *, timeout_ms: int = 5000) -> None:
        self._request("scroll", locator=locator)

    def click_locator(self, locator: object, **kwargs: object) -> None:
        self.wait_for_locator(
            locator,
            timeout_ms=int(kwargs.get("timeout_ms", 5000)),
        )
        self._request(
            "click",
            locator=locator,
            anchor=str(kwargs.get("anchor", "center")),
            offset_x=float(kwargs.get("offset_x", 0)),
            offset_y=float(kwargs.get("offset_y", 0)),
            offset_unit=str(kwargs.get("offset_unit", "px")),
            click_type=str(kwargs.get("click_type", "single")),
            hold_duration_ms=int(kwargs.get("hold_duration_ms", 0)),
        )

    def fill_locator(
        self, locator: object, value: str, *, min_delay: int = 0, max_delay: int = 0
    ) -> None:
        self.wait_for_locator(locator)
        self._request(
            "fill",
            locator=locator,
            value=value,
            min_delay=min_delay,
            max_delay=max_delay,
        )

    def click_position(
        self, x: int, y: int, *, click_type: str = "single", hold_duration_ms: int = 0
    ) -> None:
        self._request(
            "click_position",
            x=x,
            y=y,
            click_type=click_type,
            hold_duration_ms=hold_duration_ms,
        )

    def type_text(self, text: str, *, min_delay_ms: int = 0, max_delay_ms: int = 0) -> None:
        self._request(
            "type",
            text=text,
            min_delay_ms=min_delay_ms,
            max_delay_ms=max_delay_ms,
        )

    def clear_text_field(self, selector: str) -> None:
        self.fill_locator(selector, "")

    def press_keys(self, keys: list[str], *, mode: str, pause_ms: int = 0) -> None:
        self._request("press", keys=keys, mode=mode, pause_ms=pause_ms)

    def swipe(
        self, start_x: int, start_y: int, end_x: int, end_y: int, *, duration_ms: int
    ) -> None:
        parent = self._browser_window()
        renderer = _renderer_window(parent)
        start = _renderer_point_to_parent(parent, renderer, start_x, start_y)
        end = _renderer_point_to_parent(parent, renderer, end_x, end_y)
        drag_window(
            parent,
            *start,
            *end,
            mode="input",
            drag_duration_ms=duration_ms,
        )

    def snapshot_locators(
        self, locator: object, *, scope: object = None
    ) -> ElementCollectionSnapshot:
        if scope not in (None, {}):
            raise RuntimeError("Companion container scope is not implemented")
        result = self._request("snapshot", locator=locator)
        return ElementCollectionSnapshot(str(result["token"]), int(result["count"]))

    def click_snapshot_locator(self, token: str, index: int, **kwargs: object) -> None:
        locator = {"strategy": "snapshot", "value": token, "index": index}
        self.click_locator(locator, **kwargs)

    def extract_snapshot_locator_text(
        self, token: str, index: int, *, child_selector: str = ""
    ) -> str:
        return str(
            self._request(
                "snapshot_extract", token=token, index=index, child_selector=child_selector
            )
        )

    def scroll_snapshot_locator_into_view(
        self, token: str, index: int, *, timeout_ms: int = 5000
    ) -> None:
        self._request("snapshot_scroll", token=token, index=index)

    def release_locator_snapshot(self, token: str) -> None:
        self._request("snapshot_release", token=token)


__all__ = ["CompanionBrowserPort"]
