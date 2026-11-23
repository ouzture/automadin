"""Flow executor -- walks an AutomationFlow and dispatches commands."""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

from automadin.execution.context import FlowContext
from automadin.execution.error_report import ExecutionErrorReport
from automadin.execution.attention import RequiredVisualCondition
from automadin.engine.windows_input import WindowsInputPrivilegeError
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.typed_value import ValueType

logger = logging.getLogger(__name__)

_COMMANDS_WITH_INTERNAL_MIN_WAIT = {"if_elapsed"}
_FAILURE_REFERENCE_ATTRIBUTE = "_automadin_failure_reference"
_FAILURE_STEP_ATTRIBUTE = "_automadin_failure_step_id"


class FlowControlSignal(Exception):
    """Base class for non-error execution control signals."""


class FlowTerminate(FlowControlSignal):
    """Request that the current flow execution ends cleanly."""


class FlowRestart(FlowControlSignal):
    """Request that the current flow execution restarts from command one."""


class CommandNotMatched(RuntimeError):
    """An expected negative result from a probe-like command."""


def _interruptible_sleep(seconds: float, ctx: FlowContext) -> None:
    """Sleep for *seconds*, checking stop flag every 100ms."""
    ctx.interruptible_sleep(seconds)


def _resolve_duration_ms(ctx: FlowContext, value: object, default: int = 0) -> int:
    raw_value = default if value is None else value
    resolver = getattr(ctx, "resolve_command_scalar", None)
    resolved = (
        resolver(raw_value, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_value
    )
    return int(resolved)


class FlowExecutor:
    """Executes an AutomationFlow against a FlowContext.

    Command handlers are registered externally via ``register_handler``.
    The executor iterates through each command, resolves its handler,
    and invokes it with the resolved parameters.
    """

    def __init__(self, db: Any = None, *, enforce_preflight: bool = False) -> None:
        self._handlers: dict[str, Any] = {}
        self._db = db
        self._enforce_preflight = enforce_preflight
        from automadin.execution.hooks import HookDispatcher

        self.hook_dispatcher = HookDispatcher(self)

    # -- Handler registration -------------------------------------------

    def register_handler(self, command_id: str, handler: Any) -> None:
        """Register a callable ``handler`` for the given *command_id*."""
        self._handlers[command_id] = handler

    @property
    def registered_handler_ids(self) -> frozenset[str]:
        return frozenset(self._handlers)

    # -- Execution ------------------------------------------------------

    def execute_command(
        self,
        command: FlowCommand,
        ctx: FlowContext,
        *,
        flow_name: str = "Interactive command",
    ) -> None:
        """Execute one command through the same guarded boundary as a flow."""
        if command.id == "restart_flow":
            raise RuntimeError(
                "Restart Flow applies to a complete running flow and cannot be "
                "executed as an isolated step."
            )
        flow_kwargs: dict[str, Any] = {}
        interactive_flow = getattr(ctx, "interactive_flow", None)
        if ctx.flow_values is not None:
            catalog = ctx.flow_values.catalog
            flow_kwargs = {
                "flow_id": ctx.flow_values.flow_id,
                "value_model_version": CURRENT_FLOW_VALUE_MODEL_VERSION,
                "inputs": list(catalog.inputs),
                "variables": list(catalog.variables),
                "outputs": list(catalog.outputs),
            }
        self.execute(
            AutomationFlow(
                name=flow_name,
                commands=[command],
                resolution_scaling=ctx.interactive_resolution_scaling,
                hooks=(
                    list(interactive_flow.hooks)
                    if isinstance(interactive_flow, AutomationFlow)
                    else []
                ),
                **flow_kwargs,
            ),
            ctx,
        )

    def execute(self, flow: AutomationFlow, ctx: FlowContext) -> None:
        """Execute all commands in *flow* sequentially.

        Raises ``RuntimeError`` if the context is stopped mid-execution.
        Raises ``KeyError`` for unregistered command IDs.
        """
        if flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION:
            if ctx.flow_values is None or ctx.flow_values.flow_id != flow.flow_id:
                raise RuntimeError(
                    "Version-1 flow execution requires a matching isolated "
                    "invocation context."
                )
        elif ctx.flow_values is not None:
            raise RuntimeError(
                "Legacy flow execution cannot use a version-1 invocation context."
            )

        if self._enforce_preflight:
            from automadin.services.flow_preflight import (
                FlowPreflightError,
                PreflightMode,
                preflight_flow,
            )

            device = getattr(ctx.session, "device", None)
            report = preflight_flow(
                flow,
                db=self._db,
                root_name=flow.name,
                target=device,
                mode=PreflightMode.EXECUTION,
            )
            if report.errors:
                raise FlowPreflightError(report)

        call_stack = ctx.active_flow_call_stack
        flow_key = flow.name.replace("\\", "/").casefold()
        active_keys = {name.replace("\\", "/").casefold() for name in call_stack}
        call_chain = [*call_stack, flow.name]

        if flow_key in active_keys:
            chain = " -> ".join(call_chain)
            raise RuntimeError(
                f"Recursive flow call blocked: {chain}. "
                "Remove the circular run_flow reference."
            )
        if len(call_stack) >= ctx.max_flow_call_depth:
            chain = " -> ".join(call_chain)
            raise RuntimeError(
                f"Maximum flow call depth ({ctx.max_flow_call_depth}) exceeded: {chain}"
            )

        previous_executor = ctx._executor
        previous_flow = ctx.active_flow
        call_stack.append(flow.name)
        ctx._executor = self
        ctx.active_flow = flow
        try:
            with ctx.bind_resolution_scaling(flow.resolution_scaling):
                while not ctx.is_stopped:
                    try:
                        self._execute_commands(flow.commands, ctx)
                    except FlowRestart:
                        ctx.reset_for_restart()
                        if ctx.listener:
                            ctx.listener.on_log(f"Restarting flow '{flow.name}' from beginning")
                        continue
                    except FlowTerminate:
                        if ctx.listener:
                            ctx.listener.on_log(f"Terminated flow '{flow.name}'")
                        return
                    return
        finally:
            call_stack.pop()
            ctx._executor = previous_executor
            ctx.active_flow = previous_flow

    def _execute_children(
        self, commands: list[FlowCommand], ctx: FlowContext
    ) -> None:
        """Execute a list of child commands (alias for _execute_commands)."""
        self._execute_commands(commands, ctx)

    def _execute_commands(
        self, commands: list[FlowCommand], ctx: FlowContext
    ) -> None:
        for cmd in commands:
            if ctx.is_stopped:
                return

            ctx.wait_if_paused()
            if ctx.is_stopped:
                return

            sid = cmd.short_id  # e.g. "a3f2"
            ctx.current_short_id = sid

            if not cmd.enabled:
                msg = f"[{sid}] {cmd.id} — disabled, skipped"
                logger.debug(msg)
                if ctx.listener:
                    ctx.listener.on_log(msg)
                continue

            # Min-wait-time gating — value lives in params (set by UI editor)
            # with fallback to the dataclass field for backward compat.
            # Skipped in step-through mode so every command always executes.
            min_wait = _resolve_duration_ms(
                ctx, cmd.params.get("min_wait_time", 0)
            ) or cmd.min_wait_time
            if (
                cmd.id not in _COMMANDS_WITH_INTERNAL_MIN_WAIT
                and not ctx._step_through
                and min_wait > 0
                and ctx.should_skip_min_wait(cmd.step_id, min_wait)
            ):
                logger.debug("[%s] %s — skipped (min_wait_time %dms not elapsed)", sid, cmd.id, min_wait)
                ctx.log_detailed(
                    f"[{sid}] {cmd.id} — skipped; minimum interval "
                    f"{min_wait}ms has not elapsed"
                )
                continue

            # Pre-execution pause (skipped in step-through mode)
            pause_ms = _resolve_duration_ms(ctx, cmd.params.get("pause_ms", 0))
            if pause_ms > 0 and not ctx.is_stopped and not ctx._step_through:
                if ctx.listener:
                    ctx.listener.on_log(f"[{sid}] {cmd.id} — pausing {pause_ms}ms")
                _interruptible_sleep(pause_ms / 1000.0, ctx)
                if ctx.is_stopped:
                    return

            handler = self._handlers.get(cmd.id)
            if handler is None:
                msg = f"[{sid}] {cmd.id} — no handler registered"
                logger.warning(msg)
                if cmd.on_error == "skip":
                    if ctx.listener:
                        ctx.listener.on_log(f"⚠ {msg}")
                    continue
                if ctx.listener:
                    ctx.listener.on_error(msg)
                raise KeyError(msg)

            # Notify listener
            logger.debug("[%s] %s — started", sid, cmd.id)
            if ctx.listener:
                ctx.listener.on_log(f"[{sid}] {cmd.id} — started")
                ctx.listener.on_step_start(cmd.id, cmd.step_id)

            try:
                command_started = time.perf_counter()
                previous_command = ctx.active_command
                ctx.active_command = cmd
                attempt = 0
                host_completed = False
                host_started = False
                while True:
                    try:
                        if not host_completed:
                            host_started = False
                            self.hook_dispatcher.dispatch_host_before(cmd, ctx)
                            host_started = True
                            handler(cmd, ctx)
                            host_completed = True
                            if ctx.is_stopped:
                                break
                        self.hook_dispatcher.dispatch_host_after(cmd, ctx)
                    except FlowControlSignal:
                        raise
                    except RequiredVisualCondition:
                        if ctx.listener:
                            ctx.listener.on_error(
                                f"[{sid}] {cmd.id} — configured visual prerequisite was not met"
                            )
                        raise
                    except WindowsInputPrivilegeError as exc:
                        logger.warning(
                            "Windows input privilege mismatch [step=%s, command=%s]",
                            sid,
                            cmd.id,
                            exc_info=True,
                        )
                        if ctx.listener:
                            ctx.listener.on_error(str(exc))
                        raise
                    except Exception as exc:
                        if host_started and not host_completed:
                            try:
                                self.hook_dispatcher.dispatch_host_failure(cmd, ctx)
                            except FlowControlSignal:
                                raise
                            except Exception:
                                logger.exception(
                                    "Hook dispatch failed while handling host command "
                                    "failure [step=%s, command=%s]",
                                    sid,
                                    cmd.id,
                                )
                        if cmd.on_error == "skip" and isinstance(
                            exc, CommandNotMatched
                        ):
                            logger.debug(
                                "[%s] %s — no match, skipped: %s",
                                sid,
                                cmd.id,
                                exc,
                            )
                            if ctx.listener:
                                ctx.listener.on_log(
                                    f"[{sid}] {cmd.id} — no match, skipped"
                                )
                            break
                        originating_reference = getattr(
                            exc,
                            _FAILURE_REFERENCE_ATTRIBUTE,
                            None,
                        )
                        if getattr(exc, _FAILURE_STEP_ATTRIBUTE, None) == cmd.step_id:
                            # A handler may deliberately raise the same exception
                            # instance on each retry. It is still this command's
                            # failure, not a nested child failure.
                            originating_reference = None
                        attempt += 1
                        error_ref = (
                            str(originating_reference)
                            if originating_reference
                            else uuid4().hex[:8].upper()
                        )
                        error_msg = (
                            f"[{sid}] {cmd.id} — failed (reference {error_ref}). "
                            "Review Diagnostics for details."
                        )
                        if not originating_reference:
                            try:
                                setattr(exc, _FAILURE_REFERENCE_ATTRIBUTE, error_ref)
                                setattr(exc, _FAILURE_STEP_ATTRIBUTE, cmd.step_id)
                            except (AttributeError, TypeError):
                                pass
                            logger.warning(
                                "Command execution failed [reference=%s, step=%s, "
                                "command=%s]",
                                error_ref,
                                sid,
                                cmd.id,
                                exc_info=True,
                            )
                        if (
                            cmd.on_error == "retry"
                            and attempt <= cmd.retry_count
                            and not ctx.is_stopped
                        ):
                            retry_msg = (
                                f"⚠ {error_msg}; retrying "
                                f"({attempt}/{cmd.retry_count})"
                            )
                            logger.warning(retry_msg)
                            if ctx.listener:
                                ctx.listener.on_log(retry_msg)
                            continue
                        if cmd.on_error == "skip":
                            if ctx.listener:
                                if originating_reference:
                                    ctx.listener.on_log(
                                        f"⚠ [{sid}] {cmd.id} — skipped after child "
                                        f"failure (reference {error_ref})"
                                    )
                                else:
                                    ctx.listener.on_log(f"⚠ {error_msg}")
                            break
                        if ctx.listener and not originating_reference:
                            report_callback = getattr(
                                type(ctx.listener),
                                "on_error_report",
                                None,
                            )
                            if callable(report_callback):
                                report_callback(
                                    ctx.listener,
                                    ExecutionErrorReport.from_exception(
                                        exc,
                                        reference=error_ref,
                                        command_id=cmd.id,
                                        step_id=cmd.step_id,
                                    )
                                )
                            else:
                                ctx.listener.on_error(error_msg)
                        raise
                    else:
                        ctx.record_run_time(cmd.step_id)
                        logger.debug("[%s] %s — done", sid, cmd.id)
                        elapsed_ms = (time.perf_counter() - command_started) * 1000
                        ctx.log_detailed(
                            f"[{sid}] {cmd.id} — completed in {elapsed_ms:.1f}ms; "
                            f"attempts={attempt + 1}"
                        )
                        break
            finally:
                ctx.active_command = previous_command
                if ctx.listener:
                    ctx.listener.on_step_end(cmd.id, cmd.step_id)
                # Step-through: re-pause so Next Step advances exactly one command
                if ctx._step_through and not ctx.is_stopped:
                    ctx.pause()
