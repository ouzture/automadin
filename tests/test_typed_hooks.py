from __future__ import annotations

from dataclasses import dataclass

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from automadin.engine.device_controller import DEVICE_CONTROLLER_SESSION_KEY
from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor, FlowTerminate
from automadin.execution.flow_values import FlowInvocationValues, FlowRuntimeValueError
from automadin.execution.handlers.detection import _take_screenshot_for_device
from automadin.execution.handlers.control import handle_group
from automadin.execution.handlers.registry import get_default_params, load_command_types
from automadin.execution.hooks import handle_check_hooks
from automadin.execution.handlers.browser_elements import handle_wait_for_selector
from automadin.execution.handlers.browser_ext import handle_wait_for_navigation
from automadin.execution.handlers.navigate import handle_navigate_to
from automadin.execution.handlers.wait import handle_wait
from automadin.models.device import DeviceSession
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.hook import (
    AutomationHook,
    BrowserElementHookCondition,
    HookAutomaticScope,
    HookBinding,
    HookMatch,
    HookPhase,
    HookRetriggerPolicy,
    VisualImageHookCondition,
)
from automadin.services.flow_preflight import PreflightMode, preflight_flow
from automadin.models.typed_value import ValueType
from automadin.models.value_reference import BuiltinValueReference


class _Images:
    def load_image(self, name: str) -> bytes | None:
        return b"template" if name == "interrupt.png" else None

    def list_images(self) -> list[dict[str, str]]:
        return [{"name": "interrupt.png"}]


class _Screens:
    def __init__(self, values: list[bytes]) -> None:
        self.values = values
        self.calls = 0

    def take_screenshot(self) -> bytes:
        value = self.values[self.calls]
        self.calls += 1
        return value


@dataclass
class _Result:
    center_x: int = 1
    center_y: int = 2
    confidence: float = 1.0


def _visual_flow(*, retrigger=HookRetriggerPolicy.REARM_AFTER_ABSENT):
    hook = AutomationHook(
        name="Interrupt",
        condition=VisualImageHookCondition(("interrupt.png",)),
        actions=[FlowCommand("mark")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
        retrigger=retrigger,
    )
    return AutomationFlow("Visual", [FlowCommand("click_image")], hooks=[hook])


def test_nonmatching_visual_hook_reuses_the_host_screenshot(monkeypatch) -> None:
    screens = _Screens([b"host"])
    session = DeviceSession(data={DEVICE_CONTROLLER_SESSION_KEY: screens})
    context = FlowContext(session)
    executor = FlowExecutor(_Images())
    observed: list[bytes] = []

    def match(_ctx, _names, screenshot, *_args, **_kwargs):
        observed.append(screenshot)
        return None, None

    monkeypatch.setattr("automadin.execution.hooks._find_any_image", match)
    executor.register_handler(
        "click_image",
        lambda _command, ctx: _take_screenshot_for_device(ctx),
    )
    executor.register_handler("mark", lambda _command, _ctx: None)

    executor.execute(_visual_flow(), context)

    assert screens.calls == 1
    assert observed == [b"host"]


@pytest.mark.parametrize(
    ("phase", "expected_events"),
    [
        (HookPhase.BEFORE, ["hook", "child"]),
        (HookPhase.AFTER, ["child", "hook"]),
    ],
)
def test_group_visual_hook_uses_fresh_boundary_screenshot(
    monkeypatch,
    phase,
    expected_events,
) -> None:
    screens = _Screens([b"matched", b"settled"])
    session = DeviceSession(data={DEVICE_CONTROLLER_SESSION_KEY: screens})
    context = FlowContext(session)
    executor = FlowExecutor(_Images())
    events: list[str] = []
    hook = AutomationHook(
        name="Interrupt",
        condition=VisualImageHookCondition(("interrupt.png",)),
        actions=[FlowCommand("mark_hook")],
        automatic_scope=HookAutomaticScope.SELECTED_COMMANDS,
    )
    group = FlowCommand(
        "group",
        children=[FlowCommand("mark_child")],
        hook_bindings=[HookBinding(hook.hook_id, (phase,))],
    )
    flow = AutomationFlow("Group Hook", [group], hooks=[hook])

    monkeypatch.setattr(
        "automadin.execution.hooks._find_any_image",
        lambda _ctx, _names, screenshot, *_args, **_kwargs: (
            ("interrupt.png", _Result())
            if screenshot == b"matched"
            else (None, None)
        ),
    )
    executor.register_handler("group", handle_group)
    executor.register_handler(
        "mark_hook", lambda _command, _ctx: events.append("hook")
    )
    executor.register_handler(
        "mark_child", lambda _command, _ctx: events.append("child")
    )

    executor.execute(flow, context)

    assert events == expected_events
    assert screens.calls == 2


def test_matching_visual_hook_invalidates_evidence_before_host_continues(
    monkeypatch,
) -> None:
    screens = _Screens([b"matched", b"fresh"])
    session = DeviceSession(data={DEVICE_CONTROLLER_SESSION_KEY: screens})
    context = FlowContext(session)
    executor = FlowExecutor(_Images())
    events: list[str] = []

    def match(_ctx, _names, screenshot, *_args, **_kwargs):
        return (
            ("interrupt.png", _Result())
            if screenshot == b"matched"
            else (None, None)
        )

    monkeypatch.setattr("automadin.execution.hooks._find_any_image", match)
    executor.register_handler(
        "click_image",
        lambda _command, ctx: events.append(
            f"host:{_take_screenshot_for_device(ctx).decode()}"
        ),
    )
    executor.register_handler("mark", lambda _command, _ctx: events.append("hook"))

    executor.execute(_visual_flow(), context)

    assert events == ["hook", "host:fresh"]
    assert screens.calls == 2


def test_visual_hook_actions_receive_scoped_match_evidence(monkeypatch) -> None:
    screens = _Screens([b"matched", b"fresh"])
    session = DeviceSession(data={DEVICE_CONTROLLER_SESSION_KEY: screens})
    hook = AutomationHook(
        name="Interrupt",
        condition=VisualImageHookCondition(("interrupt.png",)),
        actions=[FlowCommand("observe")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Visual",
        [FlowCommand("click_image")],
        value_model_version=1,
        hooks=[hook],
    )
    values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
    context = FlowContext(session, flow_values=values)
    executor = FlowExecutor(_Images())
    observed: list[object] = []

    def match(_ctx, _names, screenshot, *_args, **_kwargs):
        if screenshot == b"matched":
            return "interrupt.png", _Result(321, 654, 0.975)
        return None, None

    def observe(_command, ctx):
        for name, value_type in (
            ("_match_x", ValueType.INTEGER),
            ("_match_y", ValueType.INTEGER),
            ("_match_confidence", ValueType.NUMBER),
            ("_matched_image", ValueType.TEXT),
        ):
            observed.append(
                ctx.resolve_command_scalar(
                    BuiltinValueReference(name).to_dict(),
                    value_type,
                )
            )

    monkeypatch.setattr("automadin.execution.hooks._find_any_image", match)
    executor.register_handler("observe", observe)
    executor.register_handler(
        "click_image",
        lambda _command, ctx: _take_screenshot_for_device(ctx),
    )
    executor.execute(flow, context)

    assert observed == [321, 654, 0.975, "interrupt.png"]
    with pytest.raises(FlowRuntimeValueError, match="unavailable in this scope"):
        values.resolve(BuiltinValueReference("_match_x"))


def test_visual_not_found_hook_does_not_expose_match_coordinates(monkeypatch) -> None:
    screens = _Screens([b"absent", b"host"])
    session = DeviceSession(data={DEVICE_CONTROLLER_SESSION_KEY: screens})
    hook = AutomationHook(
        name="Missing",
        condition=VisualImageHookCondition(
            ("interrupt.png",),
            HookMatch.NOT_FOUND,
        ),
        actions=[FlowCommand("observe")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Visual",
        [FlowCommand("click_image")],
        value_model_version=1,
        hooks=[hook],
    )
    values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
    context = FlowContext(session, flow_values=values)
    executor = FlowExecutor(_Images())
    unavailable = False

    monkeypatch.setattr(
        "automadin.execution.hooks._find_any_image",
        lambda *_args, **_kwargs: (None, None),
    )

    def observe(_command, _ctx):
        nonlocal unavailable
        with pytest.raises(FlowRuntimeValueError, match="unavailable in this scope"):
            values.resolve(BuiltinValueReference("_match_x"))
        unavailable = True

    executor.register_handler("observe", observe)
    executor.register_handler(
        "click_image",
        lambda _command, ctx: _take_screenshot_for_device(ctx),
    )
    executor.execute(flow, context)

    assert unavailable


def test_browser_hook_reprobes_after_action_before_running_host() -> None:
    class Browser:
        visible = True
        probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.visible

    browser = Browser()
    session = DeviceSession(data={"browser_data": browser})
    context = FlowContext(session)
    executor = FlowExecutor()
    events: list[str] = []
    hook = AutomationHook(
        name="Expired",
        condition=BrowserElementHookCondition("#login"),
        actions=[FlowCommand("mark")],
        automatic_scope=HookAutomaticScope.SELECTED_COMMANDS,
    )
    command = FlowCommand(
        "click_element",
        hook_bindings=[HookBinding(hook.hook_id)],
    )
    flow = AutomationFlow("Browser", [command], hooks=[hook])

    def mark(_command, _ctx):
        events.append("hook")
        browser.visible = False

    executor.register_handler("mark", mark)
    executor.register_handler(
        "click_element", lambda _command, _ctx: events.append("host")
    )

    executor.execute(flow, context)

    assert events == ["hook", "host"]
    assert browser.probes == 2


def test_hook_action_dispatch_is_suspended_recursively() -> None:
    browser = type(
        "Browser",
        (),
        {"is_locator_visible": lambda self, _locator: True},
    )()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    actions = 0
    hook = AutomationHook(
        name="Only once",
        condition=BrowserElementHookCondition("#interrupt"),
        actions=[FlowCommand("click_element")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
        retrigger=HookRetriggerPolicy.ONCE_PER_RUN,
    )
    flow = AutomationFlow("No recursion", [FlowCommand("click_element")], hooks=[hook])

    def click(_command, _ctx):
        nonlocal actions
        actions += 1

    executor.register_handler("click_element", click)
    executor.execute(flow, context)

    assert actions == 2


def test_explicit_probe_does_not_run_actions_or_change_latches() -> None:
    browser = type(
        "Browser",
        (),
        {"is_locator_visible": lambda self, _locator: True},
    )()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    context.hook_probe_only = True
    executor = FlowExecutor()
    executor.register_handler("check_hooks", handle_check_hooks)
    action_calls = 0
    hook = AutomationHook(
        name="Probe only",
        condition=BrowserElementHookCondition("#/blocker"),
        actions=[FlowCommand("mark")],
    )
    flow = AutomationFlow(
        "Probe",
        [FlowCommand("check_hooks", params={"hooks": [hook.hook_id]})],
        hooks=[hook],
    )

    def mark(_command, _ctx):
        nonlocal action_calls
        action_calls += 1

    executor.register_handler("mark", mark)
    executor.execute(flow, context)

    assert context.hook_probe_results == {hook.hook_id: True}
    assert action_calls == 0
    assert not context.hook_is_latched(flow.flow_id, hook.hook_id)


def test_after_hook_retry_does_not_replay_successful_host_command() -> None:
    browser = type(
        "Browser",
        (),
        {"is_locator_visible": lambda self, _locator: True},
    )()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    host_calls = 0
    action_calls = 0
    hook = AutomationHook(
        name="After",
        condition=BrowserElementHookCondition("#done"),
        actions=[FlowCommand("flaky")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
        automatic_phases=(HookPhase.AFTER,),
    )
    command = FlowCommand("click_element", on_error="retry", retry_count=1)
    flow = AutomationFlow("After retry", [command], hooks=[hook])

    def host(_command, _ctx):
        nonlocal host_calls
        host_calls += 1

    def flaky(_command, _ctx):
        nonlocal action_calls
        action_calls += 1
        if action_calls == 1:
            raise RuntimeError("temporary")

    executor.register_handler("click_element", host)
    executor.register_handler("flaky", flaky)
    executor.execute(flow, context)

    assert host_calls == 1
    assert action_calls == 2


def test_browser_before_hook_reprobes_after_host_failure() -> None:
    class Browser:
        probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    events: list[str] = []
    hook = AutomationHook(
        name="Block page",
        condition=BrowserElementHookCondition("#blocked"),
        actions=[FlowCommand("mark")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Browser failure",
        [FlowCommand("click_element")],
        hooks=[hook],
    )

    executor.register_handler("mark", lambda _command, _ctx: events.append("hook"))

    def fail_host(_command, _ctx):
        events.append("host")
        raise RuntimeError("click failed")

    executor.register_handler("click_element", fail_host)

    with pytest.raises(RuntimeError, match="click failed"):
        executor.execute(flow, context)

    assert events == ["host", "hook"]
    assert browser.probes == 3


def test_browser_failure_hook_can_terminate_flow() -> None:
    class Browser:
        probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    hook = AutomationHook(
        name="Block page",
        condition=BrowserElementHookCondition("#blocked"),
        actions=[FlowCommand("terminate")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Browser failure termination",
        [FlowCommand("click_element")],
        hooks=[hook],
    )

    executor.register_handler(
        "click_element",
        lambda _command, _ctx: (_ for _ in ()).throw(RuntimeError("click failed")),
    )
    executor.register_handler(
        "terminate",
        lambda _command, _ctx: (_ for _ in ()).throw(FlowTerminate()),
    )

    executor.execute(flow, context)

    assert browser.probes == 2


def test_browser_failure_hook_runs_once_across_host_retry() -> None:
    class Browser:
        probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    events: list[str] = []
    hook = AutomationHook(
        name="Block page",
        condition=BrowserElementHookCondition("#blocked"),
        actions=[FlowCommand("mark")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    command = FlowCommand("click_element", on_error="retry", retry_count=1)
    flow = AutomationFlow("Browser failure retry", [command], hooks=[hook])

    executor.register_handler("mark", lambda _command, _ctx: events.append("hook"))

    def flaky_host(_command, _ctx):
        events.append("host")
        if events.count("host") == 1:
            raise RuntimeError("click failed")

    executor.register_handler("click_element", flaky_host)
    executor.execute(flow, context)

    assert events == ["host", "hook", "host"]
    assert browser.probes == 4


def test_browser_before_hook_failure_is_not_reprobed_as_host_failure() -> None:
    class Browser:
        probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            raise RuntimeError("probe failed")

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    hook = AutomationHook(
        name="Broken probe",
        condition=BrowserElementHookCondition("#blocked"),
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Browser probe failure",
        [FlowCommand("click_element")],
        hooks=[hook],
    )
    host_calls = 0

    def host(_command, _ctx):
        nonlocal host_calls
        host_calls += 1

    executor.register_handler("click_element", host)

    with pytest.raises(RuntimeError, match="probe failed"):
        executor.execute(flow, context)

    assert browser.probes == 1
    assert host_calls == 0


def test_browser_failure_hook_action_error_preserves_host_error() -> None:
    class Browser:
        probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    hook = AutomationHook(
        name="Broken action",
        condition=BrowserElementHookCondition("#blocked"),
        actions=[FlowCommand("broken")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Browser hook action failure",
        [FlowCommand("click_element")],
        hooks=[hook],
    )

    executor.register_handler(
        "click_element",
        lambda _command, _ctx: (_ for _ in ()).throw(RuntimeError("click failed")),
    )
    executor.register_handler(
        "broken",
        lambda _command, _ctx: (_ for _ in ()).throw(RuntimeError("hook failed")),
    )

    with pytest.raises(RuntimeError, match="click failed"):
        executor.execute(flow, context)

    assert not context.hook_is_latched(flow.flow_id, hook.hook_id)


def test_browser_hook_guards_navigation_command_without_declared_hook_point() -> None:
    class Browser:
        probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    host_calls = 0
    hook = AutomationHook(
        name="Captcha",
        condition=BrowserElementHookCondition("Additional security check is required"),
        actions=[FlowCommand("terminate")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Navigation guard",
        [FlowCommand("wait_for_navigation")],
        hooks=[hook],
    )

    def fail_navigation(_command, _ctx):
        nonlocal host_calls
        host_calls += 1
        raise TimeoutError("networkidle timeout")

    executor.register_handler("wait_for_navigation", fail_navigation)
    executor.register_handler(
        "terminate",
        lambda _command, _ctx: (_ for _ in ()).throw(FlowTerminate()),
    )

    executor.execute(flow, context)

    assert host_calls == 1
    assert browser.probes == 2


def test_navigation_wait_polls_browser_hook_while_command_is_blocked() -> None:
    class Page:
        waits = 0

        def wait_for_load_state(self, _state, *, timeout):
            self.waits += 1
            raise PlaywrightTimeoutError(f"slice timed out after {timeout}ms")

    class Browser:
        def __init__(self) -> None:
            self.active_page = Page()
            self.probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    hook = AutomationHook(
        name="Captcha",
        condition=BrowserElementHookCondition("Additional security check is required"),
        actions=[FlowCommand("terminate")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Navigation polling",
        [FlowCommand("wait_for_navigation", params={"timeout": 10_000})],
        hooks=[hook],
    )
    executor.register_handler("wait_for_navigation", handle_wait_for_navigation)
    executor.register_handler(
        "terminate",
        lambda _command, _ctx: (_ for _ in ()).throw(FlowTerminate()),
    )

    executor.execute(flow, context)

    assert browser.active_page.waits == 1
    assert browser.probes == 2


def test_navigate_to_polls_browser_hook_while_dom_content_is_loading() -> None:
    class Registry:
        def ensure_page(self) -> None:
            return None

    class Browser:
        def __init__(self) -> None:
            self.active_page = self
            self.page_registry = Registry()
            self.goto_options: list[dict[str, object]] = []
            self.waits: list[int] = []
            self.probes = 0

        def goto(self, _url: str, **options: object) -> None:
            self.goto_options.append(options)

        def wait_for_load_state(self, _state: str, *, timeout: int) -> None:
            self.waits.append(timeout)
            raise PlaywrightTimeoutError(f"slice timed out after {timeout}ms")

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    hook = AutomationHook(
        name="Captcha",
        condition=BrowserElementHookCondition("Additional security check is required"),
        actions=[FlowCommand("terminate")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Navigation start polling",
        [FlowCommand("navigate_to", params={"url": "https://example.test"})],
        hooks=[hook],
    )
    executor.register_handler("navigate_to", handle_navigate_to)
    executor.register_handler(
        "terminate",
        lambda _command, _ctx: (_ for _ in ()).throw(FlowTerminate()),
    )

    executor.execute(flow, context)

    assert browser.goto_options == [{"wait_until": "commit", "timeout": 30000}]
    assert browser.waits == [500]
    assert browser.probes == 2


def test_navigation_wait_honors_stop_between_playwright_slices() -> None:
    class Page:
        def __init__(self) -> None:
            self.waits = 0

        def wait_for_load_state(self, _state: str, *, timeout: int) -> None:
            self.waits += 1
            context.stop()
            raise PlaywrightTimeoutError(f"slice timed out after {timeout}ms")

    page = Page()
    browser = type("Browser", (), {"active_page": page})()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    events: list[str] = []
    flow = AutomationFlow(
        "Stop navigation wait",
        [
            FlowCommand("wait_for_navigation", params={"timeout": 30_000}),
            FlowCommand("mark"),
        ],
    )
    executor.register_handler("wait_for_navigation", handle_wait_for_navigation)
    executor.register_handler("mark", lambda _command, _ctx: events.append("mark"))

    executor.execute(flow, context)

    assert page.waits == 1
    assert events == []


def test_element_wait_polls_browser_hook_while_command_is_blocked() -> None:
    class Browser:
        def __init__(self) -> None:
            self.waits: list[int] = []
            self.probes = 0

        def wait_for_locator(self, _locator, *, timeout_ms: int) -> None:
            self.waits.append(timeout_ms)
            raise PlaywrightTimeoutError(f"slice timed out after {timeout_ms}ms")

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    executor = FlowExecutor()
    hook = AutomationHook(
        name="Captcha",
        condition=BrowserElementHookCondition("Additional security check is required"),
        actions=[FlowCommand("terminate")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Element wait polling",
        [
            FlowCommand(
                "wait_for_selector",
                params={"locator": "#ready", "timeout": 10_000},
            )
        ],
        hooks=[hook],
    )
    executor.register_handler("wait_for_selector", handle_wait_for_selector)
    executor.register_handler(
        "terminate",
        lambda _command, _ctx: (_ for _ in ()).throw(FlowTerminate()),
    )

    executor.execute(flow, context)

    assert browser.waits == [500]
    assert browser.probes == 2


def test_timed_wait_polls_browser_hook_while_command_is_blocked() -> None:
    class Browser:
        def __init__(self) -> None:
            self.probes = 0

        def is_locator_visible(self, _locator) -> bool:
            self.probes += 1
            return self.probes > 1

    browser = Browser()
    context = FlowContext(DeviceSession(data={"browser_data": browser}))
    sleeps: list[float] = []
    context.interruptible_sleep = lambda seconds: sleeps.append(seconds) or True
    executor = FlowExecutor()
    hook = AutomationHook(
        name="Captcha",
        condition=BrowserElementHookCondition("Additional security check is required"),
        actions=[FlowCommand("terminate")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow(
        "Timed wait polling",
        [FlowCommand("wait", params={"ms": 10_000})],
        hooks=[hook],
    )
    executor.register_handler("wait", handle_wait)
    executor.register_handler(
        "terminate",
        lambda _command, _ctx: (_ for _ in ()).throw(FlowTerminate()),
    )

    executor.execute(flow, context)

    assert sleeps == [0.5]
    assert browser.probes == 2


def test_automatic_browser_hook_is_skipped_without_browser_evidence() -> None:
    context = FlowContext(DeviceSession())
    executor = FlowExecutor()
    events: list[str] = []
    hook = AutomationHook(
        name="Browser only",
        condition=BrowserElementHookCondition("#captcha"),
        actions=[FlowCommand("mark")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
    )
    flow = AutomationFlow("No browser", [FlowCommand("wait")], hooks=[hook])
    executor.register_handler("wait", lambda _command, _ctx: events.append("host"))
    executor.register_handler("mark", lambda _command, _ctx: events.append("hook"))

    executor.execute(flow, context)

    assert events == ["host"]


def test_preflight_rejects_stale_target_and_recursive_hook_references() -> None:
    hook = AutomationHook(
        name="Browser guard",
        condition=BrowserElementHookCondition("#login", HookMatch.FOUND),
        actions=[FlowCommand("check_hooks", params={"hooks": []})],
        automatic_scope=HookAutomaticScope.SELECTED_COMMANDS,
    )
    flow = AutomationFlow(
        "Invalid",
        [
            FlowCommand(
                "click_image",
                hook_bindings=[
                    HookBinding(hook.hook_id),
                    HookBinding("00000000-0000-4000-8000-000000000001"),
                ],
            )
        ],
        hooks=[hook],
    )

    report = preflight_flow(
        flow,
        target="android",
        mode=PreflightMode.EXECUTION,
    )
    codes = {issue.code for issue in report.errors}

    assert "INCOMPATIBLE_HOOK_BINDING" not in codes
    assert "STALE_HOOK_REFERENCE" in codes
    assert "UNSUPPORTED_HOOK_TARGET" in codes
    assert "HOOK_RECURSION" in codes


def test_preflight_accepts_browser_hook_binding_on_navigation_command() -> None:
    hook = AutomationHook(
        name="Captcha",
        condition=BrowserElementHookCondition("#captcha"),
        automatic_scope=HookAutomaticScope.SELECTED_COMMANDS,
    )
    flow = AutomationFlow(
        "Navigation hook",
        [
            FlowCommand(
                "wait_for_navigation",
                hook_bindings=[HookBinding(hook.hook_id)],
            )
        ],
        hooks=[hook],
    )

    report = preflight_flow(flow, target="browser", mode=PreflightMode.EXECUTION)

    assert "INCOMPATIBLE_HOOK_BINDING" not in {
        issue.code for issue in report.errors
    }


def test_preflight_scopes_visual_match_builtins_to_found_hook_actions() -> None:
    command_types = load_command_types()
    click_params = get_default_params(command_types["click_position"])
    click_params.update(
        {
            "x": BuiltinValueReference("_match_x").to_dict(),
            "y": BuiltinValueReference("_match_y").to_dict(),
        }
    )
    found_hook = AutomationHook(
        name="Found",
        condition=VisualImageHookCondition(("interrupt.png",)),
        actions=[FlowCommand("click_position", params=click_params)],
    )
    found_flow = AutomationFlow(
        "Found",
        value_model_version=1,
        hooks=[found_hook],
    )

    found_report = preflight_flow(
        found_flow,
        db=_Images(),
        target="android",
        mode=PreflightMode.EXECUTION,
    )
    assert not found_report.errors

    missing_hook = AutomationHook(
        name="Missing",
        condition=VisualImageHookCondition(
            ("interrupt.png",),
            HookMatch.NOT_FOUND,
        ),
        actions=[FlowCommand("click_position", params=click_params)],
    )
    missing_flow = AutomationFlow(
        "Missing",
        value_model_version=1,
        hooks=[missing_hook],
    )
    missing_report = preflight_flow(
        missing_flow,
        db=_Images(),
        target="android",
        mode=PreflightMode.EXECUTION,
    )
    assert any(
        "unavailable" in issue.message.lower()
        for issue in missing_report.errors
    )


def test_preflight_rejects_binding_to_explicit_only_hook() -> None:
    hook = AutomationHook(
        name="Explicit only",
        condition=BrowserElementHookCondition("#dialog"),
        automatic_scope=HookAutomaticScope.NONE,
    )
    flow = AutomationFlow(
        "Inactive binding",
        [
            FlowCommand(
                "click_element",
                hook_bindings=[HookBinding(hook.hook_id)],
            )
        ],
        hooks=[hook],
    )

    report = preflight_flow(flow, target="browser", mode=PreflightMode.EXECUTION)

    assert "INACTIVE_HOOK_BINDING" in {issue.code for issue in report.errors}
