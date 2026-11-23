from __future__ import annotations

from unittest.mock import Mock, call

import pytest

from automadin.execution.context import MAX_FLOW_CALL_DEPTH, FlowContext
from automadin.execution.executor import FlowExecutor, FlowRestart
from automadin.execution.handlers.control import handle_run_flow, handle_terminate_flow
from automadin.models.device import DeviceSession
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.services.flow_preflight import FlowPreflightError


class _FlowDatabase:
    def __init__(self, flows: dict[str, AutomationFlow]) -> None:
        self.flows = flows

    def load_flow(self, name: str) -> AutomationFlow | None:
        return self.flows.get(name)


def _executor(flows: dict[str, AutomationFlow]) -> FlowExecutor:
    executor = FlowExecutor(_FlowDatabase(flows))
    executor.register_handler("run_flow", handle_run_flow)
    executor.register_handler("terminate_flow", handle_terminate_flow)
    return executor


def test_direct_recursive_flow_call_is_blocked_and_stack_is_cleaned() -> None:
    flow = AutomationFlow("A", [FlowCommand("run_flow", params={"flow_name": "A"})])
    executor = _executor({"A": flow})
    ctx = FlowContext(DeviceSession())

    with pytest.raises(RuntimeError, match=r"Recursive flow call blocked: A -> A"):
        executor.execute(flow, ctx)

    assert ctx.active_flow_call_stack == []
    assert ctx._executor is None


def test_legacy_parent_cannot_silently_share_context_with_v1_child() -> None:
    child = AutomationFlow(
        "typed-child",
        [FlowCommand("comment")],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    parent = AutomationFlow(
        "legacy-parent",
        [FlowCommand("run_flow", params={"flow_name": child.name})],
    )

    with pytest.raises(RuntimeError, match="requires explicit Input/Output mappings"):
        _executor({parent.name: parent, child.name: child}).execute(
            parent,
            FlowContext(DeviceSession()),
        )


def test_indirect_recursive_flow_call_reports_actionable_chain() -> None:
    flow_a = AutomationFlow("A", [FlowCommand("run_flow", params={"flow_name": "B"})])
    flow_b = AutomationFlow("B", [FlowCommand("run_flow", params={"flow_name": "A"})])
    executor = _executor({"A": flow_a, "B": flow_b})
    ctx = FlowContext(DeviceSession())

    with pytest.raises(RuntimeError, match=r"A -> B -> A.*circular run_flow"):
        executor.execute(flow_a, ctx)

    assert ctx.active_flow_call_stack == []


def test_dynamic_flow_call_resolving_to_active_flow_cleans_up_context() -> None:
    flow = AutomationFlow(
        "dynamic-parent",
        [FlowCommand("run_flow", params={"flow_name": "{{callee}}"})],
    )
    executor = _executor({"dynamic-parent": flow})
    prior_executor = Mock()
    ctx = FlowContext(DeviceSession())
    ctx.set_variable("callee", "dynamic-parent")
    ctx._executor = prior_executor

    with pytest.raises(
        RuntimeError,
        match=(
            r"Recursive flow call blocked: "
            r"dynamic-parent -> dynamic-parent.*circular run_flow"
        ),
    ):
        executor.execute(flow, ctx)

    assert ctx.active_flow_call_stack == []
    assert ctx._executor is prior_executor


def test_flow_call_depth_limit_is_checked_before_execution() -> None:
    ctx = FlowContext(DeviceSession())
    ctx.active_flow_call_stack.extend(f"flow-{index}" for index in range(MAX_FLOW_CALL_DEPTH))

    with pytest.raises(RuntimeError, match=r"Maximum flow call depth \(64\) exceeded"):
        FlowExecutor().execute(AutomationFlow("one-too-many"), ctx)

    assert len(ctx.active_flow_call_stack) == MAX_FLOW_CALL_DEPTH


def test_subflow_terminate_returns_to_parent_and_restores_executor_context() -> None:
    events: list[str] = []
    child = AutomationFlow("child", [FlowCommand("terminate_flow")])
    parent = AutomationFlow(
        "parent",
        [
            FlowCommand("run_flow", params={"flow_name": "child"}),
            FlowCommand("record"),
        ],
    )
    executor = _executor({"child": child})
    executor.register_handler("record", lambda _cmd, _ctx: events.append("parent continued"))
    prior_executor = Mock()
    ctx = FlowContext(DeviceSession())
    ctx._executor = prior_executor

    executor.execute(parent, ctx)

    assert events == ["parent continued"]
    assert ctx.active_flow_call_stack == []
    assert ctx._executor is prior_executor


def test_restart_restarts_only_the_current_execution_and_keeps_stack_balanced() -> None:
    events: list[str] = []
    attempts = 0

    def restart_once(_cmd: FlowCommand, _ctx: FlowContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FlowRestart()
        events.append("completed")

    executor = FlowExecutor()
    executor.register_handler("restart_once", restart_once)
    ctx = FlowContext(DeviceSession())

    executor.execute(AutomationFlow("restartable", [FlowCommand("restart_once")]), ctx)

    assert attempts == 2
    assert events == ["completed"]
    assert ctx.active_flow_call_stack == []


def test_retried_subflow_failure_does_not_leave_false_recursion_state() -> None:
    attempts = 0
    listener = Mock()
    child_command = FlowCommand("fail_once")
    child = AutomationFlow("child", [child_command])
    call_child = FlowCommand(
        "run_flow",
        params={"flow_name": "child"},
        on_error="retry",
        retry_count=1,
    )
    parent = AutomationFlow("parent", [call_child])
    executor = _executor({"child": child})

    def fail_once(_cmd: FlowCommand, _ctx: FlowContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient child failure")

    executor.register_handler("fail_once", fail_once)
    ctx = FlowContext(DeviceSession(), listener)

    executor.execute(parent, ctx)

    assert attempts == 2
    assert ctx.active_flow_call_stack == []
    assert ctx._executor is None
    assert listener.on_step_start.call_args_list == [
        call("run_flow", call_child.step_id),
        call("fail_once", child_command.step_id),
        call("fail_once", child_command.step_id),
    ]
    assert listener.on_step_end.call_args_list == [
        call("fail_once", child_command.step_id),
        call("fail_once", child_command.step_id),
        call("run_flow", call_child.step_id),
    ]


def test_dynamic_subflow_is_preflighted_after_runtime_resolution() -> None:
    invalid_handler = Mock()
    invalid_command = FlowCommand("click", retry_count=-1)
    child = AutomationFlow("invalid-child", [invalid_command])
    parent = AutomationFlow(
        "parent",
        [FlowCommand("run_flow", params={"flow_name": "{{callee}}"})],
    )
    executor = FlowExecutor(
        _FlowDatabase({"invalid-child": child}),
        enforce_preflight=True,
    )
    executor.register_handler("run_flow", handle_run_flow)
    executor.register_handler("click", invalid_handler)
    ctx = FlowContext(DeviceSession())
    ctx.set_variable("callee", "invalid-child")

    with pytest.raises(FlowPreflightError) as exc_info:
        executor.execute(parent, ctx)

    assert any(
        issue.code == "INVALID_COMMAND_FIELD"
        and issue.location.flow_name == "invalid-child"
        and issue.location.parameter == "retry_count"
        for issue in exc_info.value.report.errors
    )
    invalid_handler.assert_not_called()
    assert ctx.active_flow_call_stack == []
    assert ctx._executor is None
