from __future__ import annotations

import threading

import pytest

from automadin.execution.attention import (
    AttentionRequest,
    AttentionResolution,
    RequiredVisualCondition,
    resolve_condition_unmet_policy,
    resolve_not_found_policy,
)
from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.handlers.control import handle_user_checkpoint
from automadin.execution.runner import FlowRunWorker
from automadin.models.device import DeviceSession
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import RunConfig, RunStatus


def test_attention_request_returns_the_listener_resolution() -> None:
    class Listener:
        def on_attention_required(self, request) -> None:
            request.resolve(AttentionResolution.CONTINUE)

    context = FlowContext(DeviceSession(), Listener())

    assert context.request_attention(
        "Open the sign-in screen.", command_id="click_image", step_id="step"
    ) is AttentionResolution.CONTINUE


def test_attention_request_without_an_interactive_listener_stops() -> None:
    context = FlowContext(DeviceSession())

    assert context.request_attention(
        "Open the sign-in screen.", command_id="click_image", step_id="step"
    ) is AttentionResolution.STOP


def test_attention_request_unblocks_when_the_flow_is_stopped() -> None:
    class Listener:
        def on_attention_required(self, _request) -> None:
            return None

    context = FlowContext(DeviceSession(), Listener())
    result: list[AttentionResolution] = []
    thread = threading.Thread(
        target=lambda: result.append(
            context.request_attention("Open it.", command_id="find_image", step_id="step")
        )
    )
    thread.start()
    context.stop()
    thread.join(1)

    assert result == [AttentionResolution.STOP]


def test_not_found_policy_uses_custom_message_and_listener_decision() -> None:
    class Listener:
        def on_attention_required(self, request) -> None:
            assert request.message == "Open the rewards screen."
            request.resolve("retry")

    context = FlowContext(DeviceSession(), Listener())
    command = FlowCommand(
        "click_image",
        params={
            "not_found_behavior": "attention",
            "not_found_message": "Open the rewards screen.",
        },
    )

    assert resolve_not_found_policy(context, command, "fallback") is AttentionResolution.RETRY


def test_not_found_fail_bypasses_the_normal_on_error_policy() -> None:
    context = FlowContext(DeviceSession())
    command = FlowCommand(
        "click_image",
        on_error="skip",
        params={"not_found_behavior": "fail", "not_found_message": "Required button missing."},
    )

    with pytest.raises(RequiredVisualCondition, match="Required button missing"):
        resolve_not_found_policy(context, command, "fallback")


def test_condition_unmet_policy_preserves_not_found_as_an_expected_condition() -> None:
    class Listener:
        def on_attention_required(self, request) -> None:
            assert request.message == "The loading panel should be gone."
            request.resolve(AttentionResolution.RETRY)

    context = FlowContext(DeviceSession(), Listener())
    command = FlowCommand(
        "if_image",
        params={
            "condition": "not_found",
            "condition_unmet_behavior": "attention",
            "condition_unmet_message": "The loading panel should be gone.",
        },
    )

    assert (
        resolve_condition_unmet_policy(context, command, "fallback")
        is AttentionResolution.RETRY
    )


def test_user_checkpoint_offers_continue_and_stop_but_not_retry() -> None:
    class Listener:
        def on_attention_required(self, request) -> None:
            assert request.allowed_resolutions == (
                AttentionResolution.CONTINUE,
                AttentionResolution.STOP,
            )
            request.resolve(AttentionResolution.CONTINUE)

        def on_log(self, _message: str) -> None:
            return None

    context = FlowContext(DeviceSession(), Listener())
    handle_user_checkpoint(
        FlowCommand("user_checkpoint", params={"message": "Sign in, then continue."}),
        context,
    )

    assert not context.is_stopped


def test_user_checkpoint_stop_ends_the_flow() -> None:
    class Listener:
        def on_attention_required(self, request) -> None:
            request.resolve(AttentionResolution.STOP)

    context = FlowContext(DeviceSession(), Listener())
    handle_user_checkpoint(FlowCommand("user_checkpoint"), context)

    assert context.is_stopped


def test_attention_request_rejects_an_unsupported_resolution() -> None:
    request = AttentionRequest(
        "Continue when ready.",
        "user_checkpoint",
        "step",
        (AttentionResolution.CONTINUE, AttentionResolution.STOP),
    )

    with pytest.raises(ValueError, match="not allowed"):
        request.resolve(AttentionResolution.RETRY)


def test_unattended_checkpoint_marks_worker_as_requiring_attention() -> None:
    executor = FlowExecutor()
    executor.register_handler("user_checkpoint", handle_user_checkpoint)
    worker = FlowRunWorker(
        executor,
        AutomationFlow(
            "Unattended checkpoint",
            [
                FlowCommand(
                    "user_checkpoint",
                    params={"message": "Sign in before the scheduled run."},
                )
            ],
        ),
        FlowContext(DeviceSession()),
        RunConfig(),
    )

    worker._run_with_device_operation()

    assert worker.status is RunStatus.STOPPED
    assert worker.attention_required_message == "Sign in before the scheduled run."
