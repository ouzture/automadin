from __future__ import annotations

from unittest.mock import Mock, patch

from automadin.execution.context import FlowContext
from automadin.execution.handlers.control import handle_for
from automadin.execution.handlers.browser_elements import handle_input_fill
from automadin.execution.handlers.type_text import handle_type_text
from automadin.execution.handlers.wait import handle_random_wait
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
    WindowsDevice,
)
from automadin.models.flow import FlowCommand


def _browser_context() -> FlowContext:
    session = DeviceSession(
        device=BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM)),
        data={"browser_data": Mock()},
    )
    return FlowContext(session)


def test_type_text_prefers_canonical_delays_over_legacy_aliases() -> None:
    command = FlowCommand(
        "type_text",
        params={
            "text": "hello",
            "min_delay": 5,
            "max_delay": 15,
            "minDelay": 50,
            "maxDelay": 150,
        },
    )

    context = _browser_context()
    handle_type_text(command, context)

    context.session.data["browser_data"].type_text.assert_called_once_with(
        "hello", min_delay_ms=5, max_delay_ms=15
    )


def test_type_text_legacy_fixed_delay_fills_both_bounds() -> None:
    command = FlowCommand(
        "type_text",
        params={"text": "hello", "delay_ms": 25},
    )

    context = _browser_context()
    handle_type_text(command, context)

    context.session.data["browser_data"].type_text.assert_called_once_with(
        "hello", min_delay_ms=25, max_delay_ms=25
    )


def test_input_fill_accepts_legacy_camel_case_delay_range() -> None:
    command = FlowCommand(
        "input_fill",
        params={
            "selector": "#query",
            "value": "hello",
            "minDelay": 10,
            "maxDelay": 20,
        },
    )

    port = Mock()
    context = _browser_context()
    context.session.data["browser_data"] = port

    handle_input_fill(command, context)

    port.fill_locator.assert_called_once_with(
        {"platform": "browser", "strategy": "css", "value": "#query"},
        "hello",
        min_delay=10,
        max_delay=20,
    )


def test_generic_type_text_honors_delay_range_on_windows() -> None:
    command = FlowCommand(
        "type_text",
        params={"text": "ab", "min_delay": 10, "max_delay": 20},
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(123, "Target"),
            data={"windows_data": {"hwnd": 123}},
        )
    )

    with (
        patch(
            "automadin.engine.windows_engine.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 123},
        ),
        patch("automadin.engine.windows_input.type_window_text") as type_text,
    ):
        handle_type_text(command, context)

    type_text.assert_called_once_with(
        123,
        "ab",
        delivery_mode="input",
        min_delay_ms=10,
        max_delay_ms=20,
        focus_delay_ms=200,
        restore_delay_ms=100,
    )


def test_random_wait_accepts_legacy_range_and_prefers_canonical_values() -> None:
    legacy = FlowCommand(
        "random_wait",
        params={"minMs": 2000, "maxMs": 4000},
    )
    both = FlowCommand(
        "random_wait",
        params={
            "min_ms": 100,
            "max_ms": 200,
            "minMs": 2000,
            "maxMs": 4000,
        },
    )

    with (
        patch(
            "automadin.execution.handlers.wait.random.randint",
            side_effect=[3000, 150],
        ) as randint,
        patch.object(
            FlowContext,
            "interruptible_sleep",
            return_value=True,
        ) as interruptible_sleep,
    ):
        handle_random_wait(legacy, FlowContext(DeviceSession()))
        handle_random_wait(both, FlowContext(DeviceSession()))

    assert randint.call_args_list[0].args == (2000, 4000)
    assert randint.call_args_list[1].args == (100, 200)
    assert [call.args[0] for call in interruptible_sleep.call_args_list] == [
        3.0,
        0.15,
    ]


def test_for_runtime_default_matches_the_catalog_default() -> None:
    context = FlowContext(DeviceSession())
    context._executor = Mock()
    command = FlowCommand("for", children=[FlowCommand("wait", params={"ms": 1})])

    handle_for(command, context)

    assert context._executor._execute_children.call_count == 3
