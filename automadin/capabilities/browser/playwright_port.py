"""Playwright implementation of the browser execution port."""

from __future__ import annotations

import time

from automadin.capabilities.browser import interaction, navigation, tabs
from automadin.engine.browser_engine import BrowserSessionData
from automadin.models.browser_dom import BrowserDomScope


class PlaywrightBrowserPort:
    def __init__(self, session: BrowserSessionData) -> None:
        self._session = session

    def screenshot(self) -> bytes:
        return self._session.screenshot()

    def viewport_size(self) -> tuple[int, int]:
        size = self._session.active_page.viewport_size
        if size is None:
            size = self._session.active_page.evaluate(
                "() => ({width: window.innerWidth, height: window.innerHeight})"
            )
        return int(size["width"]), int(size["height"])

    def navigate_to(self, url: str, *, timeout_ms: int = 30000) -> None:
        navigation.navigate_to(self._session, url, timeout_ms=timeout_ms)

    def go_back(self, *, timeout_ms: int = 30000) -> None:
        navigation.go_back(self._session, timeout_ms=timeout_ms)

    def go_forward(self, *, timeout_ms: int = 30000) -> None:
        navigation.go_forward(self._session, timeout_ms=timeout_ms)

    def reload_page(self, *, timeout_ms: int = 30000) -> None:
        navigation.reload_page(self._session, timeout_ms=timeout_ms)

    def wait_for_load_state(self, state: str, *, timeout_ms: int) -> None:
        self._session.active_page.wait_for_load_state(state, timeout=timeout_ms)

    def tabs_snapshot(self):
        return self._session.tabs_snapshot()

    def select_tab(self, tab_id: str) -> None:
        self._session.select_tab(tab_id)

    def open_new_tab(self, url: str | None = None, *, timeout_ms: int = 30000) -> str:
        return tabs.open_new_tab(self._session, url, timeout_ms=timeout_ms)

    def close_tab(self):
        return tabs.close_tab(self._session)

    def close_all_tabs(self) -> str:
        return tabs.close_all_tabs(self._session)

    def switch_tab(self, index: int) -> str:
        return tabs.switch_tab(self._session, index)

    def select_page(self, strategy: str, value: str = "", *, match_case: bool = False) -> str:
        return tabs.select_page(self._session, strategy, value, match_case=match_case)

    def ensure_page(self, url: str | None = None, *, timeout_ms: int = 30000):
        return tabs.ensure_page(self._session, url, timeout_ms=timeout_ms)

    def click_position(self, x: int, y: int, *, click_type: str = "single", hold_duration_ms: int = 0) -> None:
        page = self._session.active_page
        if hold_duration_ms > 0:
            page.mouse.move(x, y)
            page.mouse.down()
            time.sleep(hold_duration_ms / 1000.0)
            page.mouse.up()
        elif click_type == "double":
            page.mouse.dblclick(x, y)
        else:
            page.mouse.click(x, y)

    def swipe(self, start_x: int, start_y: int, end_x: int, end_y: int, *, duration_ms: int) -> None:
        page = self._session.active_page
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.move(end_x, end_y, steps=max(10, duration_ms // 16))
        page.mouse.up()

    def press_keys(self, keys: list[str], *, mode: str, pause_ms: int = 0) -> None:
        page = self._session.active_page
        if mode == "simultaneous" and len(keys) > 1:
            page.keyboard.press("+".join(keys))
            return
        for key in keys:
            page.keyboard.press(key)
            if pause_ms > 0 and mode == "sequential":
                time.sleep(pause_ms / 1000.0)

    def type_text(self, text: str, *, min_delay_ms: int = 0, max_delay_ms: int = 0) -> None:
        interaction.type_text(self._session, text, min_delay_ms, max_delay_ms)

    def clear_text_field(self, selector: str) -> None:
        interaction.clear_text_field(self._session, selector)

    def evaluate_js(self, script: str) -> str:
        return interaction.evaluate_js(self._session, script)

    def current_url(self) -> str:
        return str(self._session.active_page.url)

    def click_locator(self, locator: object, **kwargs: object) -> None:
        interaction.click_element(self._session, locator, **kwargs)

    def wait_for_locator(self, locator: object, *, timeout_ms: int = 5000) -> None:
        interaction.wait_for_element(self._session, locator, timeout_ms=timeout_ms)

    def fill_locator(self, locator: object, value: str, *, min_delay: int = 0, max_delay: int = 0) -> None:
        interaction.fill_element(self._session, locator, value, min_delay, max_delay)

    def extract_locator_text(self, locator: object) -> str:
        return interaction.extract_element_text(self._session, locator)

    def extract_snapshot_locator_text(self, token: str, index: int, *, child_selector: str = "") -> str:
        return interaction.extract_snapshot_element_text(
            self._session, token, index, child_selector=child_selector
        )

    def assert_locator_visible(self, locator: object) -> None:
        interaction.assert_element_visible(self._session, locator)

    def is_locator_visible(self, locator: object) -> bool:
        direct = getattr(self._session, "is_locator_visible", None)
        if callable(direct):
            return bool(direct(locator))
        return interaction.is_element_visible(self._session, locator)

    def scroll_page(self, *, delta_x: int = 0, delta_y: int = 600) -> None:
        interaction.scroll_page(self._session, delta_x=delta_x, delta_y=delta_y)

    def scroll_locator_into_view(self, locator: object, *, timeout_ms: int = 5000) -> None:
        interaction.scroll_element_into_view(self._session, locator, timeout_ms=timeout_ms)

    def scroll_snapshot_locator_into_view(self, token: str, index: int, *, timeout_ms: int = 5000) -> None:
        interaction.scroll_snapshot_element_into_view(
            self._session, token, index, timeout_ms=timeout_ms
        )

    def count_locators(self, locator: object, *, scope: object = None) -> int:
        return interaction.count_elements(
            self._session, locator, scope=BrowserDomScope.parse(scope)
        )

    def snapshot_locators(self, locator: object, *, scope: object = None):
        return interaction.snapshot_elements(
            self._session, locator, scope=BrowserDomScope.parse(scope)
        )

    def click_snapshot_locator(self, token: str, index: int, **kwargs: object) -> None:
        interaction.click_snapshot_element(self._session, token, index, **kwargs)

    def release_locator_snapshot(self, token: str) -> None:
        interaction.release_element_snapshot(self._session, token)


__all__ = ["PlaywrightBrowserPort"]
