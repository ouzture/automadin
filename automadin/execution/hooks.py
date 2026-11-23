"""Qt-independent typed conditional-hook dispatch."""

from __future__ import annotations

import copy
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from automadin.capabilities.browser.port import browser_automation_port
from automadin.execution.context import FlowContext
from automadin.execution.flow_values import RuntimeTypedValue
from automadin.execution.handlers.detection import (
    _find_any_image,
    _take_screenshot_for_device,
)
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.hook import (
    AutomationHook,
    BrowserElementHookCondition,
    HookAutomaticScope,
    HookMatch,
    HookPhase,
    HookRetriggerPolicy,
    HookType,
    VisualImageHookCondition,
)
from automadin.models.hook_compatibility import (
    command_supports_hook,
    hook_points_for_command,
)
from automadin.models.locator import normalize_locator
from automadin.models.typed_value import ValueType
from automadin.execution.handlers.registry import load_command_types

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HookDispatchResult:
    action_executed: bool = False
    hook_id: str = ""


@dataclass(frozen=True, slots=True)
class HookProbeResult:
    """One evaluated hook condition plus any ephemeral match evidence."""

    matched: bool
    matched_image: str = ""
    match_x: int | None = None
    match_y: int | None = None
    match_confidence: float | None = None

    @property
    def has_image_match(self) -> bool:
        return self.match_x is not None and self.match_y is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "matched": self.matched,
            "matched_image": self.matched_image,
            "match_x": self.match_x,
            "match_y": self.match_y,
            "match_confidence": self.match_confidence,
        }


class HookDispatcher:
    """Resolve, evaluate, and execute one flow's hooks in visible order."""

    def __init__(self, executor: Any) -> None:
        self._executor = executor
        self._command_types = load_command_types()

    def supports(
        self,
        command_id: str,
        hook_type: HookType,
        phase: HookPhase,
    ) -> bool:
        return command_supports_hook(
            self._command_types.get(command_id),
            hook_type,
            phase,
        )

    @staticmethod
    def _browser_evidence_available(ctx: FlowContext) -> bool:
        data = getattr(ctx.session, "data", None)
        browser = data.get("browser_data") if isinstance(data, dict) else None
        adapted = browser_automation_port(browser)
        return callable(getattr(adapted, "is_locator_visible", None))

    def dispatch_host_before(self, command: FlowCommand, ctx: FlowContext) -> None:
        """Run hook types whose before boundary needs no host-produced evidence."""
        flow = ctx.active_flow
        visual_hooks = (
            self._automatic_hooks(
                flow,
                command,
                HookPhase.BEFORE,
                HookType.VISUAL_IMAGE,
            )
            if isinstance(flow, AutomationFlow)
            else ()
        )
        if visual_hooks and self._visual_capture_mode(command.id) == "boundary":
            self._dispatch_visual_boundary(command, ctx, HookPhase.BEFORE)
        if self._browser_evidence_available(ctx) and self.supports(
            command.id,
            HookType.BROWSER_ELEMENT,
            HookPhase.BEFORE,
        ):
            dispatch_browser_hooks(command, ctx, HookPhase.BEFORE)

    def _visual_capture_mode(self, command_id: str) -> str:
        definition = self._command_types.get(command_id)
        point = next(
            (
                item
                for item in hook_points_for_command(definition)
                if item.get("type") == HookType.VISUAL_IMAGE.value
            ),
            None,
        )
        return str(point.get("capture", "host")) if point is not None else ""

    def _dispatch_visual_boundary(
        self,
        command: FlowCommand,
        ctx: FlowContext,
        phase: HookPhase,
    ) -> None:
        """Evaluate visual hooks with fresh evidence at a structural boundary."""
        flow = ctx.active_flow
        if not isinstance(flow, AutomationFlow):
            return
        activations = 0
        while True:
            screenshot = self._capture(ctx)
            result = self.dispatch_automatic(
                flow,
                command,
                phase,
                HookType.VISUAL_IMAGE,
                ctx,
                screenshot=screenshot,
            )
            if not result.action_executed:
                return
            activations += 1
            if activations > 100:
                raise RuntimeError(
                    "Visual hooks exceeded 100 consecutive activations; "
                    "hook actions may be oscillating."
                )

    def dispatch_host_after(self, command: FlowCommand, ctx: FlowContext) -> None:
        """Run successful after boundaries using fresh evidence."""
        flow = ctx.active_flow
        visual_hooks = (
            self._automatic_hooks(
                flow,
                command,
                HookPhase.AFTER,
                HookType.VISUAL_IMAGE,
            )
            if isinstance(flow, AutomationFlow)
            else ()
        )
        if (
            visual_hooks
            and self.supports(command.id, HookType.VISUAL_IMAGE, HookPhase.AFTER)
        ):
            activations = 0
            while True:
                screenshot = self._capture(ctx)
                result = self.dispatch_automatic(
                    ctx.active_flow,
                    command,
                    HookPhase.AFTER,
                    HookType.VISUAL_IMAGE,
                    ctx,
                    screenshot=screenshot,
                )
                if not result.action_executed:
                    break
                activations += 1
                if activations > 100:
                    raise RuntimeError(
                        "Visual hooks exceeded 100 consecutive activations; "
                        "hook actions may be oscillating."
                    )
        if self._browser_evidence_available(ctx) and self.supports(
            command.id,
            HookType.BROWSER_ELEMENT,
            HookPhase.AFTER,
        ):
            dispatch_browser_hooks(command, ctx, HookPhase.AFTER)

    def dispatch_host_failure(self, command: FlowCommand, ctx: FlowContext) -> None:
        """Re-probe before browser hooks after the host command itself fails.

        A browser can replace its DOM while an element action is waiting.  The
        ordinary before probe may therefore miss an interruption page that is
        visible by the time the action raises.  Reusing the configured before
        boundary gives those hooks one fresh observation without treating an
        unsuccessful host command as a completed after boundary.
        """
        if self._browser_evidence_available(ctx) and self.supports(
            command.id,
            HookType.BROWSER_ELEMENT,
            HookPhase.BEFORE,
        ):
            dispatch_browser_hooks(command, ctx, HookPhase.BEFORE)

    def dispatch_automatic(
        self,
        flow: AutomationFlow,
        host: FlowCommand,
        phase: HookPhase,
        hook_type: HookType,
        ctx: FlowContext,
        *,
        screenshot: bytes | None = None,
    ) -> HookDispatchResult:
        if ctx.hooks_suspended:
            return HookDispatchResult()
        hooks = self._automatic_hooks(flow, host, phase, hook_type)
        return self._dispatch_once(
            flow,
            host,
            phase,
            hooks,
            ctx,
            screenshot=screenshot,
        )

    def dispatch_explicit(
        self,
        flow: AutomationFlow,
        host: FlowCommand,
        hook_ids: Iterable[str],
        ctx: FlowContext,
    ) -> None:
        if ctx.hooks_suspended:
            raise RuntimeError("Check Hooks cannot run inside a hook action body")
        by_id = {hook.hook_id: hook for hook in flow.hooks}
        ordered: list[AutomationHook] = []
        seen: set[str] = set()
        for hook_id in hook_ids:
            if hook_id in seen:
                raise RuntimeError(f"Check Hooks contains duplicate hook ID: {hook_id}")
            seen.add(hook_id)
            hook = by_id.get(hook_id)
            if hook is None:
                raise RuntimeError(f"Check Hooks references a missing hook: {hook_id}")
            if hook.enabled:
                ordered.append(hook)

        screenshot: bytes | None = None
        activations = 0
        while True:
            for hook in ordered:
                if hook.type is HookType.VISUAL_IMAGE and screenshot is None:
                    screenshot = self._capture(ctx)
                result = self._dispatch_once(
                    flow,
                    host,
                    HookPhase.BEFORE,
                    (hook,),
                    ctx,
                    screenshot=screenshot,
                )
                if result.action_executed:
                    activations += 1
                    if activations > 100:
                        raise RuntimeError(
                            "Check Hooks exceeded 100 consecutive activations; "
                            "hook actions may be oscillating."
                        )
                    screenshot = None
                    break
            else:
                return

    def probe_explicit(
        self,
        flow: AutomationFlow,
        hook_ids: Iterable[str],
        ctx: FlowContext,
    ) -> dict[str, HookProbeResult]:
        """Evaluate conditions once without actions, latches, or re-trigger state."""
        by_id = {hook.hook_id: hook for hook in flow.hooks}
        screenshot: bytes | None = None
        results: dict[str, HookProbeResult] = {}
        for hook_id in hook_ids:
            hook = by_id.get(hook_id)
            if hook is None:
                raise RuntimeError(f"Hook condition test references a missing hook: {hook_id}")
            if hook.type is HookType.VISUAL_IMAGE and screenshot is None:
                screenshot = self._capture(ctx)
            results[hook_id] = self._probe(
                hook,
                ctx,
                screenshot=screenshot,
                source="condition-test",
            )
        return results

    def _automatic_hooks(
        self,
        flow: AutomationFlow,
        host: FlowCommand,
        phase: HookPhase,
        hook_type: HookType,
    ) -> tuple[AutomationHook, ...]:
        selected = {
            binding.hook_id
            for binding in host.hook_bindings
            if phase in binding.phases
        }
        return tuple(
            hook
            for hook in flow.hooks
            if hook.enabled
            and hook.type is hook_type
            and (
                (
                    hook.automatic_scope
                    is HookAutomaticScope.ALL_COMPATIBLE_COMMANDS
                    and phase in hook.automatic_phases
                )
                or (
                    hook.automatic_scope is HookAutomaticScope.SELECTED_COMMANDS
                    and hook.hook_id in selected
                )
            )
        )

    def _dispatch_once(
        self,
        flow: AutomationFlow,
        host: FlowCommand,
        phase: HookPhase,
        hooks: Iterable[AutomationHook],
        ctx: FlowContext,
        *,
        screenshot: bytes | None,
    ) -> HookDispatchResult:
        for hook in hooks:
            probe = self._probe(
                hook,
                ctx,
                screenshot=screenshot,
                source=f"{phase.value}:{host.id}",
            )
            latched = ctx.hook_is_latched(flow.flow_id, hook.hook_id)
            if not probe.matched:
                if (
                    latched
                    and hook.retrigger is HookRetriggerPolicy.REARM_AFTER_ABSENT
                ):
                    ctx.set_hook_latched(flow.flow_id, hook.hook_id, False)
                    logger.debug(
                        "Hook '%s' [%s] re-armed after a non-match",
                        hook.name,
                        hook.hook_id,
                    )
                continue
            if latched:
                logger.debug(
                    "Hook '%s' [%s] matched but remained latched",
                    hook.name,
                    hook.hook_id,
                )
                continue

            if ctx.listener:
                evidence = (
                    f"; image '{probe.matched_image}' at "
                    f"({probe.match_x}, {probe.match_y}), "
                    f"confidence={probe.match_confidence:.4f}"
                    if probe.has_image_match
                    and probe.match_confidence is not None
                    else ""
                )
                ctx.listener.on_log(
                    f"Hook '{hook.name}' matched {phase.value} "
                    f"{host.id} [{host.short_id}]{evidence}"
                )
            with ctx.suspend_hooks(), self._match_scope(ctx, probe):
                self._executor._execute_children(hook.actions, ctx)
            ctx.set_hook_latched(flow.flow_id, hook.hook_id, True)
            logger.debug(
                "Hook '%s' [%s] action completed; latch set",
                hook.name,
                hook.hook_id,
            )
            return HookDispatchResult(True, hook.hook_id)
        return HookDispatchResult()

    def _probe(
        self,
        hook: AutomationHook,
        ctx: FlowContext,
        *,
        screenshot: bytes | None,
        source: str,
    ) -> HookProbeResult:
        started = time.perf_counter()
        try:
            result = self._evaluate(hook, ctx, screenshot=screenshot)
        except BaseException:
            logger.exception(
                "Hook probe failed hook=%s name=%r type=%s source=%s",
                hook.hook_id,
                hook.name,
                hook.type.value,
                source,
            )
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        logger.debug(
            "Hook probe hook=%s name=%r type=%s source=%s matched=%s "
            "latency_ms=%.1f",
            hook.hook_id,
            hook.name,
            hook.type.value,
            source,
            result.matched,
            latency_ms,
        )
        ctx.log_trace(
            f"Hook probe '{hook.name}': type={hook.type.value}; source={source}; "
            f"matched={'yes' if result.matched else 'no'}; duration={latency_ms:.1f}ms"
        )
        return result

    def _evaluate(
        self,
        hook: AutomationHook,
        ctx: FlowContext,
        *,
        screenshot: bytes | None,
    ) -> HookProbeResult:
        condition = hook.condition
        if isinstance(condition, VisualImageHookCondition):
            if screenshot is None:
                raise RuntimeError(
                    f"Visual hook '{hook.name}' requires screenshot evidence"
                )
            params = condition.match_params()
            image_names = [ctx.interpolate(name) for name in condition.images]
            db = getattr(self._executor, "_db", None)
            if db is None:
                raise RuntimeError("database not available")
            for image_name in image_names:
                if db.load_image(image_name) is None:
                    raise RuntimeError(
                        f"Visual hook '{hook.name}' image is missing: {image_name}"
                    )
            threshold = self._scalar(ctx, condition.threshold, ValueType.NUMBER)
            color_match = self._scalar(
                ctx, condition.color_match, ValueType.BOOLEAN
            )
            color_tolerance = self._scalar(
                ctx, condition.color_tolerance, ValueType.NUMBER
            )
            matched_name, result = _find_any_image(
                ctx,
                image_names,
                screenshot,
                float(threshold),
                bool(color_match),
                float(color_tolerance),
                match_params=params,
                log_match=False,
            )
            found = result is not None
            condition_satisfied = (
                found if condition.match is HookMatch.FOUND else not found
            )
            return HookProbeResult(
                condition_satisfied,
                matched_image=matched_name or "",
                match_x=int(result.center_x) if result is not None else None,
                match_y=int(result.center_y) if result is not None else None,
                match_confidence=(
                    float(result.confidence) if result is not None else None
                ),
            )

        if isinstance(condition, BrowserElementHookCondition):
            locator = normalize_locator(self._interpolate(condition.locator, ctx))
            browser = browser_automation_port(ctx.session.data["browser_data"])
            visible = browser.is_locator_visible(locator)
            return HookProbeResult(
                visible if condition.match is HookMatch.FOUND else not visible
            )
        raise RuntimeError(f"Unsupported hook condition: {type(condition).__name__}")

    @staticmethod
    @contextmanager
    def _match_scope(
        ctx: FlowContext,
        result: HookProbeResult,
    ) -> Iterator[None]:
        """Expose image evidence only for the dynamic hook action lifetime."""
        if not result.has_image_match:
            yield
            return
        values = {
            "_match_x": RuntimeTypedValue(ValueType.INTEGER, result.match_x),
            "_match_y": RuntimeTypedValue(ValueType.INTEGER, result.match_y),
            "_match_confidence": RuntimeTypedValue(
                ValueType.NUMBER,
                result.match_confidence,
            ),
            "_matched_image": RuntimeTypedValue(
                ValueType.TEXT,
                result.matched_image,
            ),
        }
        if ctx.flow_values is not None:
            with ctx.flow_values.builtin_scope(values):
                yield
            return

        previous = {
            name: ctx.variables.get(name)
            for name in values
        }
        try:
            for name, value in values.items():
                ctx.set_variable(name, str(value.value))
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    ctx.variables.pop(name, None)
                else:
                    ctx.variables[name] = value

    @staticmethod
    def _capture(ctx: FlowContext) -> bytes:
        screenshot = _take_screenshot_for_device(ctx, dispatch_hooks=False)
        if screenshot is None:
            raise RuntimeError("Check Hooks failed to take a screenshot")
        ctx._last_screenshot = screenshot
        return screenshot

    @staticmethod
    def _scalar(ctx: FlowContext, value: object, value_type: ValueType) -> object:
        resolver = getattr(ctx, "resolve_command_scalar", None)
        return resolver(value, value_type) if callable(resolver) else value

    @classmethod
    def _interpolate(cls, value: object, ctx: FlowContext) -> object:
        if isinstance(value, str):
            return ctx.interpolate(value)
        if isinstance(value, dict):
            return {key: cls._interpolate(item, ctx) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._interpolate(item, ctx) for item in value]
        return copy.deepcopy(value)


def dispatch_visual_hooks(
    cmd: FlowCommand,
    ctx: FlowContext,
    phase: HookPhase,
    screenshot: bytes,
) -> bool:
    flow = ctx.active_flow
    if not isinstance(flow, AutomationFlow) or ctx.hooks_suspended:
        return False
    return ctx._executor.hook_dispatcher.dispatch_automatic(
        flow,
        cmd,
        phase,
        HookType.VISUAL_IMAGE,
        ctx,
        screenshot=screenshot,
    ).action_executed


def dispatch_browser_hooks(
    cmd: FlowCommand,
    ctx: FlowContext,
    phase: HookPhase,
) -> None:
    flow = ctx.active_flow
    if not isinstance(flow, AutomationFlow) or ctx.hooks_suspended:
        return
    activations = 0
    while ctx._executor.hook_dispatcher.dispatch_automatic(
        flow,
        cmd,
        phase,
        HookType.BROWSER_ELEMENT,
        ctx,
    ).action_executed:
        activations += 1
        if activations > 100:
            raise RuntimeError(
                "Browser hooks exceeded 100 consecutive activations; "
                "hook actions may be oscillating."
            )


def handle_check_hooks(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Evaluate an ordered explicit list using fresh typed evidence."""
    flow = ctx.active_flow
    if not isinstance(flow, AutomationFlow):
        raise RuntimeError("Check Hooks requires an active flow")
    hook_ids = cmd.params.get("hooks", [])
    if not isinstance(hook_ids, list) or any(
        not isinstance(item, str) for item in hook_ids
    ):
        raise RuntimeError("Check Hooks requires an ordered hook list")
    if ctx.hook_probe_only:
        details = ctx._executor.hook_dispatcher.probe_explicit(
            flow,
            hook_ids,
            ctx,
        )
        ctx.hook_probe_results = {
            hook_id: result.matched for hook_id, result in details.items()
        }
        ctx.hook_probe_details = {
            hook_id: result.to_dict() for hook_id, result in details.items()
        }
        return
    ctx._executor.hook_dispatcher.dispatch_explicit(flow, cmd, hook_ids, ctx)


__all__ = [
    "HookDispatchResult",
    "HookDispatcher",
    "HookProbeResult",
    "dispatch_browser_hooks",
    "dispatch_visual_hooks",
    "handle_check_hooks",
]
