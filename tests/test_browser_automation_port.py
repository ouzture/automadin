from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import Mock

from automadin.execution.context import FlowContext
from automadin.execution.handlers.click import handle_click_position
from automadin.execution.handlers.navigate import handle_navigate_to
from automadin.execution.handlers.type_text import handle_type_text
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
)
from automadin.models.flow import FlowCommand


@dataclass
class CompanionStylePort:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def click_position(
        self,
        x: int,
        y: int,
        *,
        click_type: str = "single",
        hold_duration_ms: int = 0,
    ) -> None:
        self.calls.append(("click_position", x, y, click_type, hold_duration_ms))

    def type_text(
        self,
        text: str,
        *,
        min_delay_ms: int = 0,
        max_delay_ms: int = 0,
    ) -> None:
        self.calls.append(("type_text", text, min_delay_ms, max_delay_ms))

    def navigate_to(self, url: str, *, timeout_ms: int = 30000) -> None:
        self.calls.append(("navigate_to", url, timeout_ms))

    def wait_for_load_state(self, state: str, *, timeout_ms: int) -> None:
        self.calls.append(("wait_for_load_state", state, timeout_ms))

    def is_locator_visible(self, _locator: object) -> bool:
        return False


def _context(port: CompanionStylePort) -> FlowContext:
    device = BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.CHROME))
    return FlowContext(DeviceSession(device=device, data={"browser_data": port}))


def test_existing_click_position_command_dispatches_to_companion_port() -> None:
    port = CompanionStylePort()

    handle_click_position(
        FlowCommand("click_position", params={"x": 25, "y": 40, "click": "double"}),
        _context(port),
    )

    assert port.calls == [("click_position", 25, 40, "double", 0)]


def test_existing_type_text_command_dispatches_to_companion_port() -> None:
    port = CompanionStylePort()

    handle_type_text(
        FlowCommand(
            "type_text",
            params={"text": "hello", "min_delay": 10, "max_delay": 30},
        ),
        _context(port),
    )

    assert port.calls == [("type_text", "hello", 10, 30)]


def test_existing_navigation_command_dispatches_to_companion_port() -> None:
    port = CompanionStylePort()
    context = _context(port)
    context.listener = Mock()

    handle_navigate_to(
        FlowCommand(
            "navigate_to",
            params={"url": "https://example.test", "timeout": 1200},
        ),
        context,
    )

    assert port.calls[0] == ("navigate_to", "https://example.test", 1200)
    assert port.calls[1][0:2] == ("wait_for_load_state", "domcontentloaded")
    context.listener.on_log.assert_called_once_with(
        "Navigated to https://example.test"
    )
