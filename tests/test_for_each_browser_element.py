from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from automadin.capabilities.browser.interaction import ElementCollectionSnapshot
from automadin.execution.handlers.browser_elements import (
    handle_click_element,
    handle_for_each_browser_element,
    handle_scroll_browser,
)
from automadin.models.browser_dom import BrowserElementLoopItem
from automadin.models.flow import FlowCommand


class _BrowserPort:
    def __init__(self) -> None:
        self.released: list[str] = []
        self.clicked: list[tuple[str, int, int | None]] = []
        self.click_geometry: dict[str, object] = {}
        self.scrolled: list[tuple[str, int, int]] = []

    def snapshot_locators(
        self, locator: object, *, scope: object = None
    ) -> ElementCollectionSnapshot:
        assert locator == {
            "platform": "browser",
            "strategy": "css",
            "value": ".row",
        }
        assert scope.container is not None
        assert scope.container.value == "#results"
        return ElementCollectionSnapshot("snapshot-1", 3)

    def release_locator_snapshot(self, token: str) -> None:
        self.released.append(token)

    def click_snapshot_locator(
        self,
        token: str,
        index: int,
        *,
        timeout_ms: int | None = None,
        **geometry: object,
    ) -> None:
        self.clicked.append((token, index, timeout_ms))
        self.click_geometry = geometry

    def scroll_snapshot_locator_into_view(
        self, token: str, index: int, *, timeout_ms: int = 5000
    ) -> None:
        self.scrolled.append((token, index, timeout_ms))


class _Context:
    def __init__(self, port: _BrowserPort) -> None:
        self.session = SimpleNamespace(data={"browser_data": port})
        self.listener = None
        self.flow_values = None
        self.is_stopped = False
        self.variables: dict[str, str] = {}
        self.current_browser_element: BrowserElementLoopItem | None = None
        self._executor = SimpleNamespace(_execute_children=self._execute_children)
        self.seen: list[tuple[int, int]] = []

    def interpolate(self, value: object) -> str:
        return str(value)

    def resolve_command_scalar(self, value: object, _type: object) -> object:
        return value

    def set_variable(self, name: str, value: str) -> None:
        self.variables[name] = value

    @contextmanager
    def browser_element_scope(self, item: BrowserElementLoopItem):
        previous = self.current_browser_element
        self.current_browser_element = item
        try:
            yield
        finally:
            self.current_browser_element = previous

    def _execute_children(self, _children: object, _ctx: object) -> None:
        assert self.current_browser_element is not None
        self.seen.append(
            (
                self.current_browser_element.index,
                self.current_browser_element.count,
            )
        )


def test_for_each_browser_element_snapshots_iterates_and_releases() -> None:
    port = _BrowserPort()
    ctx = _Context(port)
    command = FlowCommand(
        "for_each_browser_element",
        params={"locator": ".row", "container_locator": "#results"},
        children=[FlowCommand("section")],
    )

    handle_for_each_browser_element(command, ctx)  # type: ignore[arg-type]

    assert ctx.seen == [(0, 3), (1, 3), (2, 3)]
    assert port.released == ["snapshot-1"]
    assert ctx.current_browser_element is None


def test_click_element_can_target_current_collection_element() -> None:
    port = _BrowserPort()
    ctx = _Context(port)
    ctx.current_browser_element = BrowserElementLoopItem("snapshot-1", 2, 3)

    handle_click_element(
        FlowCommand(
            "click_element",
            params={
                "target": "current_element",
                "timeout": 900,
                "click_anchor": "bottom_left",
                "offset_unit": "pixels",
                "offset_x": -8,
                "offset_y": 12,
            },
        ),
        ctx,  # type: ignore[arg-type]
    )

    assert port.clicked == [("snapshot-1", 2, 900)]
    assert port.click_geometry == {
        "anchor": "bottom_left",
        "offset_x": -8.0,
        "offset_y": 12.0,
        "offset_unit": "pixels",
        "require_new_child_tab": False,
    }


def test_scroll_browser_can_target_current_collection_element() -> None:
    port = _BrowserPort()
    ctx = _Context(port)
    ctx.current_browser_element = BrowserElementLoopItem("snapshot-1", 1, 3)

    handle_scroll_browser(
        FlowCommand(
            "scroll_browser",
            params={"target": "current_element", "timeout": 650},
        ),
        ctx,  # type: ignore[arg-type]
    )

    assert port.scrolled == [("snapshot-1", 1, 650)]


def test_collection_snapshot_is_released_when_a_child_fails() -> None:
    port = _BrowserPort()
    ctx = _Context(port)

    def fail(*_args: object) -> None:
        raise RuntimeError("boom")

    ctx._executor = SimpleNamespace(_execute_children=fail)

    try:
        handle_for_each_browser_element(
            FlowCommand(
                "for_each_browser_element",
                params={"locator": ".row", "container_locator": "#results"},
            ),
            ctx,  # type: ignore[arg-type]
        )
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("Expected child failure")

    assert port.released == ["snapshot-1"]
