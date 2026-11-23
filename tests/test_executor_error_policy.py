from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from automadin.execution.context import FlowContext
from automadin.execution.executor import CommandNotMatched, FlowExecutor, FlowTerminate
from automadin.execution.handlers.control import handle_group
from automadin.engine.windows_input import WindowsInputPrivilegeError
from automadin.models.device import DeviceSession
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.services.flow_preflight import PreflightSeverity, preflight_flow


def _execute(command: FlowCommand, handler: object, *, listener: Mock | None = None) -> FlowContext:
    executor = FlowExecutor()
    executor.register_handler(command.id, handler)
    context = FlowContext(DeviceSession(), listener)
    executor.execute(AutomationFlow("error-policy", [command]), context)
    return context


def test_retry_succeeds_after_transient_failures_and_records_timing_once() -> None:
    attempts = 0
    listener = Mock()

    def handler(_command: FlowCommand, _context: FlowContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("transient")

    command = FlowCommand("unstable", on_error="retry", retry_count=2)
    context = FlowContext(DeviceSession(), listener)
    context.record_run_time = Mock()
    executor = FlowExecutor()
    executor.register_handler(command.id, handler)

    executor.execute(AutomationFlow("retry-success", [command]), context)

    assert attempts == 3
    context.record_run_time.assert_called_once_with(command.step_id)
    listener.on_step_start.assert_called_once_with(command.id, command.step_id)
    listener.on_step_end.assert_called_once_with(command.id, command.step_id)
    assert listener.on_log.call_count == 3
    detailed_messages = [
        call.args[0] for call in listener.on_detailed_log.call_args_list
    ]
    assert any(
        "completed in" in message and "attempts=3" in message
        for message in detailed_messages
    )
    listener.on_error.assert_not_called()


def test_retry_exhaustion_reports_and_raises_without_recording_timing() -> None:
    handler = Mock(side_effect=RuntimeError("still broken"))
    listener = Mock()
    command = FlowCommand("unstable", on_error="retry", retry_count=2)
    context = FlowContext(DeviceSession(), listener)
    context.record_run_time = Mock()
    executor = FlowExecutor()
    executor.register_handler(command.id, handler)

    with pytest.raises(RuntimeError, match="still broken"):
        executor.execute(AutomationFlow("retry-exhaustion", [command]), context)

    assert handler.call_count == 3
    context.record_run_time.assert_not_called()
    listener.on_error.assert_called_once()
    listener.on_step_start.assert_called_once_with(command.id, command.step_id)
    listener.on_step_end.assert_called_once_with(command.id, command.step_id)


def test_skip_logs_failure_and_continues_to_next_command() -> None:
    events: list[str] = []
    listener = Mock()
    executor = FlowExecutor()
    executor.register_handler("broken", Mock(side_effect=ValueError("bad input")))
    executor.register_handler("next", lambda _command, _context: events.append("continued"))
    flow = AutomationFlow(
        "skip",
        [
            FlowCommand("broken", on_error="skip"),
            FlowCommand("next"),
        ],
    )

    executor.execute(flow, FlowContext(DeviceSession(), listener))

    assert events == ["continued"]
    listener.on_error.assert_not_called()
    failure_messages = [
        call.args[0]
        for call in listener.on_log.call_args_list
        if "failed (reference " in call.args[0]
    ]
    assert len(failure_messages) == 1
    assert "bad input" not in failure_messages[0]
    assert "Review Diagnostics" in failure_messages[0]


def test_input_privilege_mismatch_is_visible_and_cannot_be_silently_skipped() -> None:
    listener = Mock()
    message = (
        "Windows cannot send input because the target is running as administrator."
    )
    command = FlowCommand("click", on_error="skip")

    with pytest.raises(WindowsInputPrivilegeError, match="running as administrator"):
        _execute(
            command,
            Mock(side_effect=WindowsInputPrivilegeError(message)),
            listener=listener,
        )

    listener.on_error.assert_called_once_with(message)


def test_expected_image_miss_is_a_lean_skip_without_diagnostic_reference(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    listener = Mock()
    executor = FlowExecutor()
    executor.register_handler(
        "probe",
        Mock(side_effect=CommandNotMatched("image was absent")),
    )
    executor.register_handler("next", lambda _command, _context: events.append("continued"))

    executor.execute(
        AutomationFlow(
            "expected-miss",
            [
                FlowCommand("probe", on_error="skip"),
                FlowCommand("next"),
            ],
        ),
        FlowContext(DeviceSession(), listener),
    )

    messages = [call.args[0] for call in listener.on_log.call_args_list]
    assert events == ["continued"]
    assert any("probe — no match, skipped" in message for message in messages)
    assert all("reference" not in message for message in messages)
    assert not any(record.levelno >= 30 for record in caplog.records)


def test_stop_reports_and_fails_immediately() -> None:
    next_handler = Mock()
    listener = Mock()
    executor = FlowExecutor()
    executor.register_handler("broken", Mock(side_effect=ValueError("fatal")))
    executor.register_handler("next", next_handler)
    flow = AutomationFlow(
        "stop",
        [
            FlowCommand("broken", on_error="stop"),
            FlowCommand("next"),
        ],
    )

    with pytest.raises(ValueError, match="fatal"):
        executor.execute(flow, FlowContext(DeviceSession(), listener))

    next_handler.assert_not_called()
    listener.on_error.assert_called_once()


def test_nested_child_failure_is_reported_once_at_its_origin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = Mock()
    child = FlowCommand("broken", on_error="stop")
    group = FlowCommand("group", children=[child], on_error="stop")
    executor = FlowExecutor()
    executor.register_handler("group", handle_group)
    executor.register_handler("broken", Mock(side_effect=ValueError("fatal")))

    with pytest.raises(ValueError, match="fatal"):
        executor.execute(
            AutomationFlow("nested-stop", [group]),
            FlowContext(DeviceSession(), listener),
        )

    warnings = [
        record
        for record in caplog.records
        if record.message.startswith("Command execution failed")
    ]
    assert len(warnings) == 1
    assert f"step={child.short_id}" in warnings[0].message
    listener.on_error.assert_called_once()


def test_group_can_skip_an_already_reported_child_failure() -> None:
    events: list[str] = []
    listener = Mock()
    child = FlowCommand("broken", on_error="stop")
    group = FlowCommand("group", children=[child], on_error="skip")
    executor = FlowExecutor()
    executor.register_handler("group", handle_group)
    executor.register_handler("broken", Mock(side_effect=ValueError("fatal")))
    executor.register_handler(
        "next",
        lambda _command, _context: events.append("continued"),
    )

    executor.execute(
        AutomationFlow("nested-skip", [group, FlowCommand("next")]),
        FlowContext(DeviceSession(), listener),
    )

    assert events == ["continued"]
    listener.on_error.assert_called_once()
    messages = [call.args[0] for call in listener.on_log.call_args_list]
    assert any("skipped after child failure (reference " in message for message in messages)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("on_error", "continue"),
        ("retry_count", -1),
        ("retry_count", 101),
    ],
)
def test_preflight_rejects_invalid_error_policy_fields(field: str, value: object) -> None:
    command = FlowCommand("click")
    setattr(command, field, value)

    report = preflight_flow(AutomationFlow("invalid-policy", [command]))

    matching = [issue for issue in report.issues if issue.location.parameter == field]
    assert len(matching) == 1
    assert matching[0].code == "INVALID_COMMAND_FIELD"
    assert matching[0].severity is PreflightSeverity.ERROR


def test_disabled_command_error_policy_issues_are_nonblocking() -> None:
    command = FlowCommand(
        "click",
        enabled=False,
        on_error="continue",
        retry_count=101,
    )

    report = preflight_flow(AutomationFlow("disabled-invalid-policy", [command]))

    policy_issues = [
        issue
        for issue in report.issues
        if issue.location.parameter in {"on_error", "retry_count"}
    ]
    assert len(policy_issues) == 2
    assert all(issue.severity is PreflightSeverity.INFO for issue in policy_issues)
    assert report.can_execute


def test_flow_control_signal_is_not_retried_and_step_lifecycle_is_balanced() -> None:
    listener = Mock()
    handler = Mock(side_effect=FlowTerminate())
    command = FlowCommand("terminate", on_error="retry", retry_count=100)

    context = _execute(command, handler, listener=listener)

    handler.assert_called_once()
    assert command.step_id not in context.command_last_run_times
    listener.on_error.assert_not_called()
    listener.on_step_start.assert_called_once_with(command.id, command.step_id)
    listener.on_step_end.assert_called_once_with(command.id, command.step_id)


def test_stopped_context_prevents_further_retry_attempts() -> None:
    listener = Mock()

    def stop_and_fail(_command: FlowCommand, context: FlowContext) -> None:
        context.stop()
        raise RuntimeError("stopped")

    handler = Mock(side_effect=stop_and_fail)
    command = FlowCommand("stop-and-fail", on_error="retry", retry_count=100)

    with pytest.raises(RuntimeError, match="stopped"):
        _execute(command, handler, listener=listener)

    handler.assert_called_once()
    listener.on_error.assert_called_once()
    listener.on_step_end.assert_called_once_with(command.id, command.step_id)


def test_isolated_restart_flow_is_rejected_without_entering_restart_loop() -> None:
    executor = FlowExecutor()
    executor.register_handler("restart_flow", Mock(side_effect=AssertionError))
    context = FlowContext(DeviceSession())

    with pytest.raises(RuntimeError, match="cannot be executed as an isolated step"):
        executor.execute_command(FlowCommand("restart_flow"), context)


def test_context_interruptible_sleep_returns_promptly_after_stop() -> None:
    context = FlowContext(DeviceSession())
    completed: list[bool] = []
    started = threading.Event()

    def sleep_in_background() -> None:
        started.set()
        completed.append(context.interruptible_sleep(30))

    worker = threading.Thread(target=sleep_in_background)
    worker.start()
    assert started.wait(1)
    began_stopping = time.monotonic()
    context.stop()
    worker.join(1)

    assert not worker.is_alive()
    assert completed == [False]
    assert time.monotonic() - began_stopping < 0.5
