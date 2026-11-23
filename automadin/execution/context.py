from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from automadin.execution.flow_values import (
    FlowInvocationValues,
    FlowRuntimeValueError,
)
from automadin.execution.attention import AttentionRequest, AttentionResolution
from automadin.execution.error_report import ExecutionErrorReport
from automadin.models.device import DeviceSession
from automadin.models.browser_dom import BrowserElementLoopItem
from automadin.models.flow_parameter import (
    ParameterValue,
    canonical_parameter_text,
)
from automadin.models.resolution_scaling import (
    ResolutionScalingMode,
    ResolutionScalingOverride,
    ResolutionScalingSettings,
)
from automadin.execution.resolution_scaling import (
    ResolutionTransform,
    resolve_resolution_transform,
)
from automadin.models.typed_value import (
    TypedValue,
    ValueCoercionError,
    ValueType,
    coerce_typed_value,
    is_value_type_assignable,
)
from automadin.models.value_reference import (
    FlowValueTemplate,
    ValueReference,
    ValueReferenceError,
    value_reference_from_dict,
)


@runtime_checkable
class FlowExecutionListener(Protocol):
    """Callback protocol for flow execution events."""

    def on_step_start(self, command_id: str, step_id: str) -> None: ...

    def on_step_end(self, command_id: str, step_id: str) -> None: ...

    def on_log(self, message: str) -> None: ...

    def on_detailed_log(self, message: str) -> None: ...

    def on_trace_log(self, message: str) -> None: ...

    def on_error(self, message: str) -> None: ...

    def on_error_report(self, report: ExecutionErrorReport) -> None: ...

    def on_complete(self, message: str) -> None: ...

    def on_attention_required(self, request: AttentionRequest) -> None: ...


_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")
MAX_FLOW_CALL_DEPTH = 64


@dataclass(slots=True)
class _FlowExecutionControl:
    paused: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)
    step_through: bool = False
    active_flow_call_stack: list[str] = field(default_factory=list)
    max_flow_call_depth: int = MAX_FLOW_CALL_DEPTH
    hook_latches: dict[tuple[str, str], bool] = field(default_factory=dict)
    hook_dispatch_depth: int = 0

    def __post_init__(self) -> None:
        self.paused.set()


class FlowContext:
    """Runtime context for a single flow execution.

    Holds the device session, runtime variables, pause/stop state,
    and per-step timing for min_wait_time gating.
    """

    def __init__(
        self,
        session: DeviceSession,
        listener: FlowExecutionListener | None = None,
        *,
        parameters: Mapping[str, ParameterValue] | None = None,
        flow_values: FlowInvocationValues | None = None,
        resolution_scaling_override: ResolutionScalingOverride = (
            ResolutionScalingOverride.INHERIT
        ),
        custom_scale_percent: int = 100,
        _control: _FlowExecutionControl | None = None,
    ) -> None:
        self.session = session
        self.parameters: Mapping[str, ParameterValue] = MappingProxyType(
            dict(parameters or {})
        )
        self.flow_values = flow_values
        self.inputs = (
            flow_values.inputs if flow_values is not None else MappingProxyType({})
        )
        self.variables: dict[str, str] = {}
        self.command_last_run_times: dict[str, float] = {}
        self.elapsed_block_last_enter_times: dict[str, float] = {}
        self.listener = listener
        self.resolution_scaling_override = resolution_scaling_override
        self.custom_scale_percent = custom_scale_percent
        self._active_resolution_scaling = ResolutionScalingSettings()
        self.interactive_resolution_scaling = ResolutionScalingSettings()
        self._control = _control or _FlowExecutionControl()
        self._paused = self._control.paused

        # Set by FlowExecutor so control-flow handlers can call back.
        self._executor: Any = None

        # Active top-level/sub-flow calls.  FlowExecutor owns push/pop so this
        # remains accurate even when a handler raises or requests flow control.
        self.active_flow_call_stack = self._control.active_flow_call_stack
        self.max_flow_call_depth = self._control.max_flow_call_depth

        # Scratch space for handler-produced artefacts (e.g. last screenshot).
        self._last_screenshot: bytes | None = None

        # Set by executor before each command so listeners can auto-prefix.
        self.current_short_id: str = ""
        self.active_command: Any | None = None
        self.active_flow: Any | None = None
        self._browser_element_scopes: list[BrowserElementLoopItem] = []

        # Transient UI probe state.  Hook condition tests use the normal worker
        # and device/browser ownership paths but never run actions or alter latches.
        self.hook_probe_only = False
        self.hook_probe_results: dict[str, bool] = {}
        self.hook_probe_details: dict[str, dict[str, object]] = {}
        self._logged_resolution_transforms: set[tuple[object, ...]] = set()

        # Step-through debugging: when True, executor re-pauses after each command.

    def log_detailed(self, message: str) -> None:
        """Emit a useful diagnostic intended for the Detailed log level."""
        callback = getattr(self.listener, "on_detailed_log", None)
        if callable(callback):
            callback(message)

    def log_trace(self, message: str) -> None:
        """Emit a high-volume diagnostic intended for the Trace log level."""
        callback = getattr(self.listener, "on_trace_log", None)
        if callable(callback):
            callback(message)

    @property
    def _step_through(self) -> bool:
        return self._control.step_through

    @_step_through.setter
    def _step_through(self, value: bool) -> None:
        self._control.step_through = bool(value)

    @property
    def max_flow_call_depth(self) -> int:
        return self._control.max_flow_call_depth

    @max_flow_call_depth.setter
    def max_flow_call_depth(self, value: int) -> None:
        self._control.max_flow_call_depth = value

    def fork_child(self, flow_values: FlowInvocationValues) -> FlowContext:
        """Create isolated child state that shares only execution control."""
        return FlowContext(
            self.session,
            self.listener,
            flow_values=flow_values,
            resolution_scaling_override=self.resolution_scaling_override,
            custom_scale_percent=self.custom_scale_percent,
            _control=self._control,
        )

    @contextmanager
    def bind_resolution_scaling(
        self,
        settings: ResolutionScalingSettings,
    ) -> Iterator[None]:
        """Activate one flow's reference surface for its execution scope."""
        previous = self._active_resolution_scaling
        self._active_resolution_scaling = settings
        try:
            yield
        finally:
            self._active_resolution_scaling = previous

    @property
    def resolution_scaling_active(self) -> bool:
        if self.resolution_scaling_override is ResolutionScalingOverride.OFF:
            return False
        if self.resolution_scaling_override in {
            ResolutionScalingOverride.ADAPTIVE,
            ResolutionScalingOverride.CUSTOM,
        }:
            return True
        return self._active_resolution_scaling.mode is ResolutionScalingMode.ADAPTIVE

    def resolution_transform(
        self,
        surface_size: tuple[int, int],
        *,
        reference_size: tuple[int, int] | None = None,
    ) -> ResolutionTransform:
        transform = resolve_resolution_transform(
            self._active_resolution_scaling,
            self.resolution_scaling_override,
            self.custom_scale_percent,
            surface_size,
            reference_size=reference_size,
        )
        if transform.enabled:
            reference = reference_size or self._active_resolution_scaling.reference_size
            reference_detail = (
                f"; reference={reference[0]}x{reference[1]}"
                if reference is not None
                else ""
            )
            command = getattr(self, "active_command", None)
            diagnostic_key = (
                getattr(command, "step_id", ""),
                surface_size,
                reference,
                transform.x_scale,
                transform.y_scale,
                transform.scale_pixel_geometry,
            )
            if diagnostic_key not in self._logged_resolution_transforms:
                self._logged_resolution_transforms.add(diagnostic_key)
                self.log_detailed(
                    "Resolution scaling: "
                    f"surface={surface_size[0]}x{surface_size[1]}{reference_detail}; "
                    f"scale=({transform.x_scale:.4f}, {transform.y_scale:.4f}); "
                    f"pixel geometry={'on' if transform.scale_pixel_geometry else 'off'}"
                )
        return transform

    @contextmanager
    def bind_listener_context(self) -> Iterator[None]:
        """Temporarily point context-aware listeners at this invocation."""
        listener = self.listener
        if listener is None or not hasattr(listener, "_ctx"):
            yield
            return
        previous = listener._ctx  # type: ignore[attr-defined]
        listener._ctx = self  # type: ignore[attr-defined]
        try:
            yield
        finally:
            listener._ctx = previous  # type: ignore[attr-defined]

    @contextmanager
    def browser_element_scope(
        self,
        item: BrowserElementLoopItem,
    ) -> Iterator[None]:
        """Expose one ephemeral browser element only to nested commands."""
        self._browser_element_scopes.append(item)
        try:
            yield
        finally:
            self._browser_element_scopes.pop()

    @contextmanager
    def suspend_hooks(self) -> Iterator[None]:
        """Prevent direct or indirect hook recursion inside a hook action."""
        self._control.hook_dispatch_depth += 1
        try:
            yield
        finally:
            self._control.hook_dispatch_depth -= 1

    @property
    def hooks_suspended(self) -> bool:
        return self._control.hook_dispatch_depth > 0

    def hook_is_latched(self, flow_id: str, hook_id: str) -> bool:
        return self._control.hook_latches.get((flow_id, hook_id), False)

    def set_hook_latched(self, flow_id: str, hook_id: str, value: bool) -> None:
        self._control.hook_latches[(flow_id, hook_id)] = bool(value)

    @property
    def current_browser_element(self) -> BrowserElementLoopItem | None:
        return self._browser_element_scopes[-1] if self._browser_element_scopes else None

    # ── Variable interpolation ────────────────────────────────────────

    def interpolate(self, text: object) -> str:
        """Resolve legacy interpolation or pass through a version-1 literal."""
        if self.flow_values is not None:
            if isinstance(text, Mapping):
                try:
                    template = FlowValueTemplate.from_dict(text)
                except ValueReferenceError as exc:
                    raise FlowRuntimeValueError(str(exc)) from exc
                return self.interpolate_value_template(template)
            if not isinstance(text, (str, int, float, bool)):
                raise FlowRuntimeValueError(
                    "This version-1 command field does not support a structured "
                    "value template yet."
                )
            text = str(text)
            if _VAR_PATTERN.search(text):
                raise FlowRuntimeValueError(
                    "Legacy name-based interpolation is unavailable in version-1 "
                    "flows; this command field must use a stable value reference or "
                    "template."
                )
            return text

        text = str(text)

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in self.parameters:
                return canonical_parameter_text(self.parameters[name])
            return self.variables.get(name, match.group(0))
        return _VAR_PATTERN.sub(_replace, text)

    def resolve_value_reference(self, reference: ValueReference) -> TypedValue:
        if self.flow_values is None:
            raise RuntimeError("This context has no version-1 flow values.")
        return self.flow_values.resolve(reference)

    def resolve_command_scalar(
        self,
        value: object,
        expected_type: ValueType,
    ) -> TypedValue:
        """Resolve a version-1 scalar literal/reference for a command handler."""
        if self.flow_values is None:
            if expected_type is ValueType.DURATION_MS:
                return int(value)  # type: ignore[arg-type]
            return value  # type: ignore[return-value]
        raw_value = value
        if isinstance(value, Mapping):
            try:
                reference = value_reference_from_dict(value)
                resolved = self.flow_values.resolve_typed(reference)
            except (ValueReferenceError, FlowRuntimeValueError) as exc:
                raise FlowRuntimeValueError(str(exc)) from exc
            if not is_value_type_assignable(resolved.type, expected_type):
                raise FlowRuntimeValueError(
                    f"Command value requires {expected_type.value}, but received "
                    f"{resolved.type.value}."
                )
            raw_value = resolved.value
        try:
            return coerce_typed_value(
                expected_type,
                raw_value,
                subject=f"Command value ({expected_type.value})",
            )
        except ValueCoercionError as exc:
            raise FlowRuntimeValueError(str(exc)) from exc

    def interpolate_value_template(self, template: FlowValueTemplate) -> str:
        if self.flow_values is None:
            raise RuntimeError("This context has no version-1 flow values.")
        return self.flow_values.interpolate(template)

    def get_parameter(self, name: str) -> ParameterValue | None:
        return self.parameters.get(name)

    def set_variable(self, name: str, value: str) -> None:
        self.variables[name] = value

    def get_variable(self, name: str) -> str:
        return self.variables.get(name, "")

    def set_flow_variable(self, variable_id: str, value: object) -> TypedValue:
        if self.flow_values is None:
            raise RuntimeError("This context has no version-1 flow values.")
        return self.flow_values.variables.set(variable_id, value)

    def set_output_variable(self, target: object, value: str) -> TypedValue | str:
        """Write a command result through the active flow value model."""
        if not isinstance(target, str) or not target:
            raise FlowRuntimeValueError("Command output target is missing.")
        if self.flow_values is not None:
            return self.set_flow_variable(target, value)
        self.set_variable(target, value)
        return value

    def get_flow_variable(self, variable_id: str) -> TypedValue:
        if self.flow_values is None:
            raise RuntimeError("This context has no version-1 flow values.")
        return self.flow_values.variables.get(variable_id)

    # ── Pause / stop ──────────────────────────────────────────────────

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def stop(self) -> None:
        self._control.stopped.set()
        # Also release any pause-wait so the thread can exit.
        self._paused.set()

    def reset_for_restart(self) -> None:
        """Clear transient timing state before restarting the flow from step one."""
        self.command_last_run_times.clear()
        self.elapsed_block_last_enter_times.clear()
        self._last_screenshot = None
        self.current_short_id = ""

    @property
    def is_stopped(self) -> bool:
        return self._control.stopped.is_set()

    def wait_if_paused(self) -> None:
        """Block until resumed (or stopped)."""
        if not self._paused.is_set():
            callback = getattr(self.listener, "on_pause_reached", None)
            if callable(callback):
                callback()
        self._paused.wait()

    def request_attention(
        self,
        message: str,
        *,
        command_id: str,
        step_id: str,
        allowed_resolutions: tuple[AttentionResolution, ...] | None = None,
    ) -> AttentionResolution:
        """Request a human decision without making handlers depend on any UI."""
        request = AttentionRequest(
            message,
            command_id,
            step_id,
            allowed_resolutions=allowed_resolutions
            or (
                AttentionResolution.RETRY,
                AttentionResolution.CONTINUE,
                AttentionResolution.STOP,
            ),
        )
        callback = getattr(self.listener, "on_attention_required", None)
        if not callable(callback):
            return AttentionResolution.STOP
        callback(request)
        resolution = request.wait(stopped=self._control.stopped)
        resolved_callback = getattr(self.listener, "on_attention_resolved", None)
        if callable(resolved_callback):
            resolved_callback(resolution)
        return resolution

    def interruptible_sleep(
        self,
        seconds: float,
        *,
        poll_interval: float = 0.1,
    ) -> bool:
        """Sleep cooperatively and return false when execution was stopped."""
        duration = max(0.0, float(seconds))
        interval = max(0.001, float(poll_interval))
        remaining = duration
        while not self.is_stopped and remaining > 0:
            self.wait_if_paused()
            if self.is_stopped:
                break
            started = time.monotonic()
            time.sleep(min(interval, remaining))
            remaining -= time.monotonic() - started
        if remaining <= 0:
            return True
        return False

    def enable_step_through(self) -> None:
        """Enable step-through mode: executor will re-pause after each command."""
        self._step_through = True
        self.pause()  # Ensure wait_if_paused() blocks on next command

    def disable_step_through(self) -> None:
        """Disable step-through mode: execution resumes at normal speed."""
        self._step_through = False

    # ── Min-wait-time gating ──────────────────────────────────────────

    def record_run_time(self, step_id: str) -> None:
        self.command_last_run_times[step_id] = time.monotonic()

    def should_skip_min_wait(self, step_id: str, min_wait_time: int) -> bool:
        """Return True if the step ran too recently to run again.

        ``min_wait_time`` is in milliseconds.
        """
        if min_wait_time <= 0:
            return False
        last = self.command_last_run_times.get(step_id)
        if last is None:
            return False
        elapsed_ms = (time.monotonic() - last) * 1000
        return elapsed_ms < min_wait_time

    # ── Elapsed block gating ──────────────────────────────────────────

    def record_elapsed_block_entry(self, step_id: str) -> None:
        self.elapsed_block_last_enter_times[step_id] = time.monotonic()

    def elapsed_block_remaining_ms(self, step_id: str, min_wait_time: int) -> int:
        """Return remaining milliseconds before an elapsed block may run."""
        if min_wait_time <= 0:
            return 0
        last = self.elapsed_block_last_enter_times.get(step_id)
        if last is None:
            return 0
        elapsed_ms = (time.monotonic() - last) * 1000
        remaining_ms = min_wait_time - elapsed_ms
        if remaining_ms <= 0:
            return 0
        return max(1, int(remaining_ms))
