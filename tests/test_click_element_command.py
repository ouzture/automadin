from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit

from automadin.execution.build_executor import build_executor
from automadin.engine.browser_engine import BrowserSessionData
from automadin.execution.handlers.browser_elements import (
    handle_assert_visible,
    handle_click_element,
    handle_extract_text,
    handle_input_fill,
    handle_scroll_browser,
    handle_wait_for_selector,
)
from automadin.execution.handlers.registry import load_command_types
from automadin.models.flow import FlowCommand
from automadin.ui.flow_editor.param_editor import ParamEditorWidget


class _BrowserPort:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int | None]] = []
        self.waits: list[tuple[object, int]] = []
        self.fills: list[tuple[object, str, int, int]] = []
        self.extractions: list[object] = []
        self.assertions: list[object] = []
        self.page_scrolls: list[tuple[int, int]] = []
        self.locator_scrolls: list[tuple[object, int]] = []

    def click_locator(self, locator: object, *, timeout_ms: int | None = None) -> None:
        self.calls.append((locator, timeout_ms))

    def wait_for_locator(self, locator: object, *, timeout_ms: int = 5000) -> None:
        self.waits.append((locator, timeout_ms))

    def fill_locator(
        self,
        locator: object,
        value: str,
        *,
        min_delay: int = 0,
        max_delay: int = 0,
    ) -> None:
        self.fills.append((locator, value, min_delay, max_delay))

    def extract_locator_text(self, locator: object) -> str:
        self.extractions.append(locator)
        return "Saved"

    def extract_snapshot_locator_text(
        self,
        token: str,
        index: int,
        *,
        child_selector: str = "",
    ) -> str:
        self.extractions.append((token, index, child_selector))
        return "Search on Bing to find flights"

    def assert_locator_visible(self, locator: object) -> None:
        self.assertions.append(locator)

    def scroll_page(self, *, delta_x: int = 0, delta_y: int = 600) -> None:
        self.page_scrolls.append((delta_x, delta_y))

    def scroll_locator_into_view(
        self, locator: object, *, timeout_ms: int = 5000
    ) -> None:
        self.locator_scrolls.append((locator, timeout_ms))


class _Listener:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def on_log(self, message: str) -> None:
        self.messages.append(message)


def _context(port: _BrowserPort) -> SimpleNamespace:
    listener = _Listener()
    variables: dict[str, object] = {}
    return SimpleNamespace(
        session=SimpleNamespace(data={"browser_data": port}),
        listener=listener,
        interpolate=lambda text: text.replace("{{suffix}}", "primary"),
        set_variable=lambda name, value: variables.__setitem__(name, value),
        set_output_variable=lambda name, value: variables.__setitem__(name, value),
        variables=variables,
    )


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_click_element_handler_normalizes_interpolates_and_uses_session_port() -> None:
    port = _BrowserPort()
    ctx = _context(port)
    command = FlowCommand(
        "click_element",
        params={
            "locator": {
                "platform": "browser",
                "strategy": "css",
                "value": ".{{suffix}}",
                "fallbacks": [{"strategy": "role", "role": "button", "name": "Save"}],
            },
            "timeout": 1250,
        },
    )

    handle_click_element(command, ctx)  # type: ignore[arg-type]

    assert port.calls == [
        (
            {
                "platform": "browser",
                "strategy": "css",
                "value": ".primary",
                "fallbacks": [
                    {
                        "platform": "browser",
                        "strategy": "role",
                        "role": "button",
                        "name": "Save",
                    }
                ],
            },
            1250,
        )
    ]
    assert ctx.listener.messages == ["Clicked browser element"]


def test_click_element_preserves_legacy_selector_strings_as_css() -> None:
    port = _BrowserPort()

    handle_click_element(
        FlowCommand("click_element", params={"locator": "#save"}),
        _context(port),  # type: ignore[arg-type]
    )

    assert port.calls == [
        ({"platform": "browser", "strategy": "css", "value": "#save"}, 5000)
    ]


def test_click_element_supports_owner_thread_raw_session_data() -> None:
    session_data = object.__new__(BrowserSessionData)
    ctx = _context(_BrowserPort())
    ctx.session.data["browser_data"] = session_data

    with patch(
        "automadin.capabilities.browser.playwright_port.interaction.click_element"
    ) as click:
        handle_click_element(
            FlowCommand("click_element", params={"locator": "#save", "timeout": 900}),
            ctx,  # type: ignore[arg-type]
        )

    click.assert_called_once_with(
        session_data,
        {"platform": "browser", "strategy": "css", "value": "#save"},
        timeout_ms=900,
    )


def test_related_element_commands_use_structured_locators_and_legacy_alias() -> None:
    port = _BrowserPort()
    ctx = _context(port)
    legacy = {"selector": ".{{suffix}}"}

    handle_wait_for_selector(
        FlowCommand("wait_for_selector", params={**legacy, "timeout": 800}),
        ctx,  # type: ignore[arg-type]
    )
    handle_input_fill(
        FlowCommand(
            "input_fill",
            params={**legacy, "value": "hello", "delay_ms": 7},
        ),
        ctx,  # type: ignore[arg-type]
    )
    handle_extract_text(
        FlowCommand("extract_text", params={**legacy, "variable": "result"}),
        ctx,  # type: ignore[arg-type]
    )
    handle_assert_visible(
        FlowCommand("assert_visible", params=legacy),
        ctx,  # type: ignore[arg-type]
    )

    locator = {"platform": "browser", "strategy": "css", "value": ".primary"}
    assert port.waits == [(locator, 500)]
    assert port.fills == [(locator, "hello", 7, 7)]
    assert port.extractions == [locator]
    assert port.assertions == [locator]
    assert ctx.variables == {"result": "Saved"}


def test_scroll_browser_handles_page_and_locator_targets() -> None:
    port = _BrowserPort()
    ctx = _context(port)

    handle_scroll_browser(
        FlowCommand(
            "scroll_browser",
            params={"target": "page", "delta_x": -10, "delta_y": 800},
        ),
        ctx,  # type: ignore[arg-type]
    )
    handle_scroll_browser(
        FlowCommand(
            "scroll_browser",
            params={"target": "locator", "locator": ".{{suffix}}", "timeout": 700},
        ),
        ctx,  # type: ignore[arg-type]
    )

    assert port.page_scrolls == [(-10, 800)]
    assert port.locator_scrolls == [
        ({"platform": "browser", "strategy": "css", "value": ".primary"}, 700)
    ]


def test_extract_text_can_target_current_browser_element_child() -> None:
    port = _BrowserPort()
    ctx = _context(port)
    ctx.current_browser_element = SimpleNamespace(snapshot_token="snapshot", index=2)

    handle_extract_text(
        FlowCommand(
            "extract_text",
            params={
                "target": "current_element",
                "child_selector": "p.card-description",
                "variable": "result",
            },
        ),
        ctx,  # type: ignore[arg-type]
    )

    assert port.extractions == [("snapshot", 2, "p.card-description")]
    assert ctx.variables == {"result": "Search on Bing to find flights"}


def test_click_element_is_registered_with_locator_metadata() -> None:
    definitions = load_command_types()
    for command_id in (
        "click_element",
        "wait_for_selector",
        "input_fill",
        "extract_text",
        "assert_visible",
    ):
        assert definitions[command_id]["params"][0]["type"] == "locator"
    assert definitions["wait_for_selector"]["params"][0]["aliases"] == [
        "selector"
    ]
    assert "click_element" in build_executor().registered_handler_ids
    scroll_params = {param["name"]: param for param in definitions["scroll_browser"]["params"]}
    assert scroll_params["locator"]["type"] == "locator"
    assert "scroll_browser" in build_executor().registered_handler_ids


def test_locator_editor_emits_role_payload_and_lists_all_strategies() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {"name": "locator", "label": "Element Locator", "type": "locator"},
        "#save",
        lambda name, value: changes.append((name, value)),
    )

    strategy = widget.findChild(QComboBox, "locatorStrategy")
    value_edit = widget.findChild(QLineEdit, "locatorValue")
    role_edit = widget.findChild(QLineEdit, "locatorRole")
    name_edit = widget.findChild(QLineEdit, "locatorName")
    exact = widget.findChild(QCheckBox, "locatorExact")
    assert strategy is not None
    assert value_edit is not None
    assert role_edit is not None
    assert name_edit is not None
    assert exact is not None
    assert strategy.currentData() == "css"
    assert value_edit.text() == "#save"
    assert {strategy.itemData(index) for index in range(strategy.count())} == {
        "css",
        "xpath",
        "test_id",
        "role",
        "text",
        "label",
        "placeholder",
        "alt_text",
        "title",
    }

    strategy.setCurrentIndex(strategy.findData("role"))
    role_edit.setText("button")
    name_edit.setText("Save")
    exact.setChecked(True)

    assert changes[-1] == (
        "locator",
        {
            "platform": "browser",
            "strategy": "role",
            "role": "button",
            "name": "Save",
            "exact": True,
        },
    )
    widget.deleteLater()
