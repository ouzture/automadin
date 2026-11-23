from __future__ import annotations

from dataclasses import dataclass
import time

from automadin.execution.attention import (
    AttentionResolution,
    resolve_condition_unmet_policy,
)
from automadin.execution.coordinates import resolve_offset
from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowRestart, FlowTerminate
from automadin.execution.flow_values import (
    FlowInvocationValues,
    FlowRuntimeValueError,
    RuntimeTypedValue,
    resolve_flow_inputs,
)
from automadin.execution.image_match_scope import image_match_child_scope
from automadin.execution.runtime_diagnostics import (
    format_runtime_value,
    value_reference_label,
)
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, FlowCommand
from automadin.models.run_flow_binding import RunFlowBinding, RunFlowBindingError
from automadin.models.value_condition import ValueCondition, ValueConditionError
from automadin.models.value_reference import ValueReferenceError, value_reference_from_dict
from automadin.models.typed_value import (
    ValueCoercionError,
    ValueType,
    coerce_typed_value,
)

_IMAGE_CONDITION_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class _ValueConditionEvaluation:
    condition: ValueCondition
    left: RuntimeTypedValue
    right: RuntimeTypedValue | None
    matched: bool


def handle_section(_cmd: FlowCommand, _ctx: FlowContext) -> None:
    """Deliberately do nothing; sections exist only to annotate a flow."""
    title = _ctx.interpolate(_cmd.params.get("text", "")).strip()
    if title and _ctx.listener:
        _ctx.listener.on_log(f"Section — {title}")


def handle_group(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Execute an organizational group's children once, in order."""
    title = ctx.interpolate(cmd.params.get("text", "")).strip()
    if ctx.listener:
        summary = f"Group — {title}" if title else "Group"
        step_count = len(cmd.children)
        ctx.listener.on_log(
            f"{summary} ({step_count} step{'s' if step_count != 1 else ''})"
        )
    ctx._executor._execute_children(cmd.children, ctx)


def handle_user_checkpoint(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Pause an interactive run until the user explicitly continues or stops."""
    message = ctx.interpolate(cmd.params.get("message", "")).strip()
    if not message:
        message = "Complete the required manual step, then continue the flow."
    resolution = ctx.request_attention(
        message,
        command_id=cmd.id,
        step_id=cmd.step_id,
        allowed_resolutions=(AttentionResolution.CONTINUE, AttentionResolution.STOP),
    )
    if resolution is AttentionResolution.STOP:
        ctx.stop()
        return
    if ctx.listener:
        ctx.listener.on_log("User checkpoint completed; continuing flow")


def handle_for(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Repeat children N times. Sets ``{{index}}`` on each iteration."""
    raw_count = cmd.params.get("count", 3)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    count = int(
        resolver(raw_count, ValueType.INTEGER)
        if callable(resolver)
        else raw_count
    )
    executor = ctx._executor
    for i in range(count):
        if ctx.is_stopped:
            break
        ctx.log_trace(f"For loop: iteration {i + 1}/{count}; index={i}")
        if ctx.flow_values is None:
            ctx.set_variable("index", str(i))
            executor._execute_children(cmd.children, ctx)
        else:
            with ctx.flow_values.builtin_scope(
                {"index": RuntimeTypedValue(ValueType.INTEGER, i)}
            ):
                executor._execute_children(cmd.children, ctx)
    if ctx.listener:
        ctx.listener.on_log(f"For loop completed ({count} iterations)")


def _check_image_condition(
    ctx: FlowContext,
    image_names: list[str],
    condition: str,
    threshold: float,
    color_match: bool = False,
    color_tolerance: float = 0.3,
) -> bool:
    """Take a screenshot, run find_image against a list of templates, and return
    whether the condition is met.

    Args:
        image_names: list of template image names to try (first match wins).
        condition: "found" or "not_found"

    Returns:
        True if the condition is satisfied.
    """
    from automadin.execution.handlers.detection import (
        _find_any_image,
        _take_screenshot_for_device,
    )

    screenshot = _take_screenshot_for_device(ctx)
    if screenshot is None:
        return False
    ctx._last_screenshot = screenshot

    matched_name, result = _find_any_image(
        ctx,
        image_names,
        screenshot,
        threshold,
        color_match,
        color_tolerance,
    )
    found = result is not None

    # Update context variables so downstream handlers can use them
    if found:
        ctx.set_variable("_image_found", "true")
        ctx.set_variable("_match_x", str(result.center_x))
        ctx.set_variable("_match_y", str(result.center_y))
        ctx.set_variable("_match_confidence", f"{result.confidence:.4f}")
        ctx.set_variable("_matched_image", matched_name or "")
    else:
        ctx.set_variable("_image_found", "false")
        ctx.set_variable("_match_x", "0")
        ctx.set_variable("_match_y", "0")
        ctx.set_variable("_match_confidence", "0")
        ctx.set_variable("_matched_image", "")

    if condition == "not_found":
        return not found
    return found  # default: "found"


def _wait_for_image_condition(
    ctx: FlowContext,
    image_names: list[str],
    condition: str,
    threshold: float,
    color_match: bool,
    color_tolerance: float,
    timeout_ms: int,
) -> bool:
    """Poll an image condition once, without deciding how an unmet result is handled."""
    started = time.perf_counter()
    checks = 0

    def finish(met: bool) -> bool:
        elapsed_ms = (time.perf_counter() - started) * 1000
        ctx.log_detailed(
            f"Image condition '{condition}' {'met' if met else 'not met'} after "
            f"{checks} check{'s' if checks != 1 else ''} over {elapsed_ms:.1f}ms"
        )
        return met

    deadline = time.monotonic() + timeout_ms / 1000.0 if timeout_ms > 0 else None
    while not ctx.is_stopped:
        checks += 1
        if _check_image_condition(
            ctx,
            image_names,
            condition,
            threshold,
            color_match,
            color_tolerance,
        ):
            return finish(True)
        if deadline is None:
            return finish(False)
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return finish(False)
        time.sleep(min(_IMAGE_CONDITION_POLL_INTERVAL_SECONDS, remaining_seconds))
        if ctx.is_stopped or time.monotonic() >= deadline:
            return finish(False)
    return False


def _resolve_boolean(cmd: FlowCommand, ctx: FlowContext, name: str, default: bool) -> bool:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_value
    )
    return bool(value)


def _resolve_duration_ms(cmd: FlowCommand, ctx: FlowContext, name: str, default: int) -> int:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    value = (
        resolver(raw_value, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_value
    )
    return int(value)


def handle_while(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Repeat children while an image condition is met.

    Params:
        images: list of template image names (Java format).
        image_name: single template image name.
        condition: "found" or "not_found" (default: "found").
        threshold: match confidence 0.0-1.0 (default: 0.90).
        timeout: max loop duration in milliseconds (default: 30000).
    """
    from automadin.execution.handlers.detection import _resolve_image_names

    image_names = _resolve_image_names(cmd, ctx)
    condition = cmd.params.get("condition", "found")
    raw_threshold = cmd.params.get("threshold", 0.90)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    threshold = float(
        resolver(raw_threshold, ValueType.NUMBER)
        if callable(resolver)
        else raw_threshold
    )
    raw_timeout = cmd.params.get("timeout", 30000)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout_ms = int(
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    color_match = _resolve_boolean(cmd, ctx, "color_match", False)
    raw_color_tolerance = cmd.params.get("color_tolerance", 0.3)
    color_tolerance = float(
        resolver(raw_color_tolerance, ValueType.NUMBER)
        if callable(resolver)
        else raw_color_tolerance
    )

    if not image_names:
        raise RuntimeError("while: 'images' or 'image_name' parameter is required")

    executor = ctx._executor
    start = time.monotonic()
    iterations = 0
    retry_after_attention = False

    while not ctx.is_stopped:
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms >= timeout_ms and not retry_after_attention:
            if ctx.listener:
                ctx.listener.on_log(
                    f"While loop timed out after {timeout_ms}ms ({iterations} iterations)"
                )
            break
        retry_after_attention = False

        if not _check_image_condition(
            ctx,
            image_names,
            condition,
            threshold,
            color_match,
            color_tolerance,
        ):
            resolution = resolve_condition_unmet_policy(
                ctx,
                cmd,
                (
                    f"Expected image condition '{condition}' for {', '.join(image_names)} "
                    "was not met. Make the required state visible, then choose Retry."
                ),
            )
            if resolution is AttentionResolution.RETRY and not ctx.is_stopped:
                # Retry is a deliberate fresh check, even if the user took long
                # enough at the checkpoint for the loop's ordinary timeout to pass.
                retry_after_attention = True
                continue
            if resolution is AttentionResolution.STOP:
                ctx.stop()
                break
            if ctx.listener:
                ctx.listener.on_log(
                    f"While condition no longer met after {iterations} iterations"
                )
            break

        coordinates = None
        if condition == "found":
            coordinates = (
                int(ctx.get_variable("_match_x") or "0"),
                int(ctx.get_variable("_match_y") or "0"),
            )
        with image_match_child_scope(ctx, coordinates):
            executor._execute_children(cmd.children, ctx)
        iterations += 1

    if ctx.listener:
        ctx.listener.on_log(f"While loop completed ({iterations} iterations)")


def handle_if_image(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Conditional branch based on image detection.

    Params:
        images: list of template image names (Java format).
        image_name: single template image name.
        condition: "found" or "not_found" (default: "found").
        threshold: match confidence 0.0-1.0 (default: 0.90).
        clickIfFound: if true, click the matched image center (default: false).
        timeout: maximum time to wait for the condition in milliseconds.
        hold_duration_ms: hold press duration in milliseconds (default: 0).
        xmargin: X offset added to click position (default: 0).
        ymargin: Y offset added to click position (default: 0).

    Executes children if condition is met, else_children otherwise.
    """
    from automadin.execution.handlers.detection import _resolve_image_names

    image_names = _resolve_image_names(cmd, ctx)
    condition = cmd.params.get("condition", "found")
    raw_threshold = cmd.params.get("threshold", 0.90)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    threshold = float(
        resolver(raw_threshold, ValueType.NUMBER)
        if callable(resolver)
        else raw_threshold
    )
    raw_timeout = cmd.params.get("timeout", 0)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    timeout_ms = int(
        resolver(raw_timeout, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_timeout
    )
    raw_click_if_found = cmd.params.get("clickIfFound", False)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    click_if_found = bool(
        resolver(raw_click_if_found, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_click_if_found
    )
    hold_duration_ms = _resolve_duration_ms(cmd, ctx, "hold_duration_ms", 0)
    xmargin, ymargin = resolve_offset(cmd, ctx, value_type=ValueType.INTEGER)
    color_match = _resolve_boolean(cmd, ctx, "color_match", False)
    raw_color_tolerance = cmd.params.get("color_tolerance", 0.3)
    color_tolerance = float(
        resolver(raw_color_tolerance, ValueType.NUMBER)
        if callable(resolver)
        else raw_color_tolerance
    )

    if not image_names:
        raise RuntimeError("if_image: 'images' or 'image_name' parameter is required")

    executor = ctx._executor
    while not ctx.is_stopped:
        matched = _wait_for_image_condition(
            ctx,
            image_names,
            condition,
            threshold,
            color_match,
            color_tolerance,
            timeout_ms,
        )
        if ctx.is_stopped:
            return
        if matched:
            label = f"{image_names}"
            if ctx.listener:
                ctx.listener.on_log(f"if_image: condition '{condition}' met for {label}")

            # Click the matched image if requested
            if click_if_found and condition == "found":
                _do_click_if_found(ctx, xmargin, ymargin, hold_duration_ms)

            coordinates = None
            if condition == "found":
                coordinates = (
                    int(ctx.get_variable("_match_x") or "0"),
                    int(ctx.get_variable("_match_y") or "0"),
                )
            with image_match_child_scope(ctx, coordinates):
                executor._execute_children(cmd.children, ctx)
            return

        resolution = resolve_condition_unmet_policy(
            ctx,
            cmd,
            (
                f"Expected image condition '{condition}' for {', '.join(image_names)} "
                "was not met. Make the required state visible, then choose Retry."
            ),
        )
        if resolution is AttentionResolution.RETRY:
            continue
        if resolution is AttentionResolution.STOP:
            ctx.stop()
            return

        label = f"{image_names}"
        if ctx.listener:
            ctx.listener.on_log(f"if_image: condition '{condition}' NOT met for {label}")
        if cmd.else_children:
            executor._execute_children(cmd.else_children, ctx)
        return


def _evaluate_value_condition(
    cmd: FlowCommand,
    ctx: FlowContext,
    *,
    action_label: str,
) -> _ValueConditionEvaluation:
    """Resolve and evaluate the shared typed comparison command contract."""
    if ctx.flow_values is None:
        raise FlowRuntimeValueError(f"{action_label} is available only in version-1 flows.")
    condition_params = {
        name: value
        for name, value in cmd.params.items()
        if name not in {"pause_ms", "min_wait_time", "timeout", "log_values"}
    }
    try:
        condition = ValueCondition.from_params(condition_params)
        left = ctx.flow_values.resolve_typed(condition.left)
        right = (
            ctx.flow_values.resolve_typed(condition.right)
            if condition.right is not None
            else None
        )
        condition.validate_types(left.type, right.type if right is not None else None)
        matched = condition.evaluate(
            left.value,
            right.value if right is not None else None,
        )
        return _ValueConditionEvaluation(condition, left, right, matched)
    except (ValueConditionError, FlowRuntimeValueError) as exc:
        raise FlowRuntimeValueError(f"{action_label}: {exc}") from exc


def handle_if_value(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Execute the matching branch of a typed version-1 value comparison."""
    evaluation = _evaluate_value_condition(cmd, ctx, action_label="If Value")

    branch = cmd.children if evaluation.matched else cmd.else_children
    if ctx.listener:
        if cmd.params.get("log_values", False) is True:
            condition = evaluation.condition
            left_label = value_reference_label(condition.left, ctx.flow_values.catalog)
            left_detail = (
                f"{left_label} ({evaluation.left.type.value}) = "
                f"{format_runtime_value(evaluation.left.value)}"
            )
            if evaluation.right is None:
                comparison = (
                    f"{left_detail} {condition.operator.value.replace('_', ' ')}"
                )
            else:
                assert condition.right is not None
                right_label = value_reference_label(
                    condition.right,
                    ctx.flow_values.catalog,
                )
                comparison = (
                    f"{left_detail}; {condition.operator.value.replace('_', ' ')}; "
                    f"{right_label} ({evaluation.right.type.value}) = "
                    f"{format_runtime_value(evaluation.right.value)}"
                )
            outcome = "MATCHED" if evaluation.matched else "DID NOT MATCH"
            ignore_case = " (ignoring case)" if condition.ignore_case else ""
            ctx.listener.on_log(f"If Value: {comparison}{ignore_case} → {outcome}")
        else:
            outcome = "matched" if evaluation.matched else "did not match"
            ctx.listener.on_log(f"If Value: comparison {outcome}; values hidden")
    ctx._executor._execute_children(branch, ctx)


def handle_switch_value(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Execute the first matching typed Case, or the final Default branch."""
    if ctx.flow_values is None:
        raise FlowRuntimeValueError("Switch Value is available only in version-1 flows.")

    cases = [branch for branch in cmd.children if branch.id == "switch_case"]
    defaults = [branch for branch in cmd.children if branch.id == "switch_default"]
    if not cases:
        raise FlowRuntimeValueError("Switch Value requires at least one Case.")
    if len(defaults) != 1:
        raise FlowRuntimeValueError("Switch Value requires exactly one Default branch.")
    if cmd.children[-1].id != "switch_default" or len(cases) + 1 != len(cmd.children):
        raise FlowRuntimeValueError(
            "Switch Value children must contain only Cases followed by Default."
        )

    try:
        source_reference = value_reference_from_dict(cmd.params.get("source"))
        source = ctx.flow_values.resolve_typed(source_reference)
    except (ValueReferenceError, FlowRuntimeValueError) as exc:
        raise FlowRuntimeValueError(f"Switch Value: {exc}") from exc

    for index, case in enumerate(cases, start=1):
        if "left" in case.params:
            raise FlowRuntimeValueError(
                f"Switch Value Case {index}: Case cannot define its own source."
            )
        condition_params = {**case.params, "left": cmd.params.get("source")}
        try:
            condition = ValueCondition.from_params(condition_params)
            right = (
                ctx.flow_values.resolve_typed(condition.right)
                if condition.right is not None
                else None
            )
            condition.validate_types(source.type, right.type if right is not None else None)
            matched = condition.evaluate(
                source.value,
                right.value if right is not None else None,
            )
        except (ValueConditionError, FlowRuntimeValueError) as exc:
            raise FlowRuntimeValueError(f"Switch Value Case {index}: {exc}") from exc
        if not matched:
            continue
        if ctx.listener:
            ctx.listener.on_log(f"Switch Value: Case {index} matched")
        ctx._executor._execute_children(case.children, ctx)
        return

    if ctx.listener:
        ctx.listener.on_log("Switch Value: no Case matched — running Default")
    ctx._executor._execute_children(defaults[0].children, ctx)


def handle_switch_branch_outside(_cmd: FlowCommand, _ctx: FlowContext) -> None:
    """Fail closed if a structural Switch branch reaches normal execution."""
    raise FlowRuntimeValueError(
        "Case and Default branches may execute only inside Switch Value."
    )


def handle_while_value(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Repeat children while a typed version-1 value comparison remains true."""
    timeout_ms = _resolve_duration_ms(cmd, ctx, "timeout", 30000)
    start = time.monotonic()
    iterations = 0
    while not ctx.is_stopped:
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms >= timeout_ms:
            if ctx.listener:
                ctx.listener.on_log(
                    f"While Value timed out after {timeout_ms}ms ({iterations} iterations)"
                )
            break
        evaluation = _evaluate_value_condition(cmd, ctx, action_label="While Value")
        if not evaluation.matched:
            break
        ctx._executor._execute_children(cmd.children, ctx)
        iterations += 1
    if ctx.listener:
        ctx.listener.on_log(f"While Value completed ({iterations} iterations)")


def handle_if_elapsed(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Run children if enough time has passed since this block was entered."""
    raw_min_wait = cmd.params.get("min_wait_time", 0)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    min_wait = int(
        resolver(raw_min_wait, ValueType.DURATION_MS)
        if callable(resolver)
        else raw_min_wait
    ) or cmd.min_wait_time
    executor = ctx._executor

    remaining_ms = ctx.elapsed_block_remaining_ms(cmd.step_id, min_wait)
    if ctx._step_through or remaining_ms == 0:
        ctx.record_elapsed_block_entry(cmd.step_id)
        if ctx.listener:
            if ctx._step_through:
                ctx.listener.on_log("if_elapsed: step-through bypass, running children")
            else:
                ctx.listener.on_log(f"if_elapsed: elapsed, running children ({min_wait}ms)")
        executor._execute_children(cmd.children, ctx)
        return

    if ctx.listener:
        ctx.listener.on_log(
            f"if_elapsed: waiting for min wait ({remaining_ms}ms remaining)"
        )
    if cmd.else_children:
        executor._execute_children(cmd.else_children, ctx)


def _do_click_if_found(
    ctx: FlowContext,
    xmargin: int = 0,
    ymargin: int = 0,
    hold_duration_ms: int = 0,
) -> None:
    """Click at the last matched image position (from context variables)."""
    from automadin.execution.handlers.actions import _perform_click_at

    match_x = int(ctx.get_variable("_match_x") or "0") + xmargin
    match_y = int(ctx.get_variable("_match_y") or "0") + ymargin

    if match_x == 0 and match_y == 0:
        return

    _perform_click_at(ctx, match_x, match_y, hold_duration_ms=hold_duration_ms)

    if ctx.listener:
        if hold_duration_ms > 0:
            ctx.listener.on_log(
                f"  clickIfFound: held at ({match_x}, {match_y}) for {hold_duration_ms}ms"
            )
        else:
            ctx.listener.on_log(f"  clickIfFound: clicked at ({match_x}, {match_y})")


def _parse_for_each_items(raw_items: object) -> list[str]:
    """Return ordered for-each items from legacy strings or new string lists."""
    if isinstance(raw_items, list):
        return [
            item
            for item in (str(value).strip() for value in raw_items if value is not None)
            if item
        ]

    items_str = str(raw_items or "")
    if "\n" in items_str:
        return [s.strip() for s in items_str.split("\n") if s.strip()]
    return [s.strip() for s in items_str.split(",") if s.strip()]


def handle_for_each(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Iterate over items. Sets ``{{item}}`` and ``{{index}}``."""
    raw_items = cmd.params.get("values", cmd.params.get("items", ""))
    if ctx.flow_values is None:
        if isinstance(raw_items, list):
            raw_items = [
                ctx.interpolate(value) if value is not None else None
                for value in raw_items
            ]
        elif isinstance(raw_items, str):
            raw_items = ctx.interpolate(raw_items)
    items = _parse_for_each_items(raw_items)
    executor = ctx._executor
    for i, item in enumerate(items):
        if ctx.is_stopped:
            break
        ctx.log_trace(
            f"For-each: iteration {i + 1}/{len(items)}; index={i}; value hidden"
        )
        if ctx.flow_values is None:
            ctx.set_variable("item", item)
            ctx.set_variable("index", str(i))
            executor._execute_children(cmd.children, ctx)
        else:
            with ctx.flow_values.builtin_scope(
                {
                    "item": RuntimeTypedValue(ValueType.TEXT, item),
                    "index": RuntimeTypedValue(ValueType.INTEGER, i),
                }
            ):
                executor._execute_children(cmd.children, ctx)
    if ctx.listener:
        ctx.listener.on_log(f"For-each completed ({len(items)} items)")


def handle_terminate_flow(cmd: FlowCommand, ctx: FlowContext) -> None:
    """End the current flow execution without treating it as an error."""
    if ctx.listener:
        ctx.listener.on_log("Terminate flow requested")
    raise FlowTerminate()


def handle_restart_flow(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Restart the current flow execution from command one."""
    if ctx.listener:
        ctx.listener.on_log("Restart flow requested")
    raise FlowRestart()


def handle_run_flow(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Run another saved flow as a subroutine."""
    db = ctx._executor._db
    if db is None:
        raise RuntimeError("run_flow: database not available on executor")
    if ctx.flow_values is not None:
        binding_params = {
            name: value
            for name, value in cmd.params.items()
            if name not in {"pause_ms", "min_wait_time"}
        }
        try:
            binding = RunFlowBinding.from_params(binding_params)
            flow = db.load_flow_by_id(binding.flow_id)
            if flow is None:
                raise FlowRuntimeValueError(
                    f"Child Flow '{binding.flow_id}' does not exist."
                )
            if flow.value_model_version != CURRENT_FLOW_VALUE_MODEL_VERSION:
                raise FlowRuntimeValueError(
                    "Version-1 Run Flow can only call a version-1 child Flow."
                )
            mapped_inputs = {
                input_id: ctx.flow_values.resolve(reference)
                for input_id, reference in binding.input_mappings.items()
            }
            child_inputs = resolve_flow_inputs(
                flow.value_catalog.inputs,
                mapped_inputs,
            )
            child_values = FlowInvocationValues(
                flow.flow_id,
                flow.value_catalog,
                child_inputs,
            )
        except (RunFlowBindingError, FlowRuntimeValueError) as exc:
            raise RuntimeError(f"run_flow: {exc}") from exc

        child_ctx = ctx.fork_child(child_values)
        if ctx.listener:
            ctx.listener.on_log(f"Running sub-flow '{flow.name}'")
        with child_ctx.bind_listener_context():
            ctx._executor.execute(flow, child_ctx)
        if child_ctx.is_stopped:
            return

        pending_outputs: list[tuple[str, object]] = []
        try:
            for output_id, parent_variable_id in binding.output_mappings.items():
                output = flow.value_catalog.output_by_id(output_id)
                if output is None:
                    raise FlowRuntimeValueError(
                        f"Child Output mapping '{output_id}' is stale."
                    )
                parent_variable = ctx.flow_values.catalog.variable_by_id(
                    parent_variable_id
                )
                if parent_variable is None:
                    raise FlowRuntimeValueError(
                        f"Parent Variable mapping '{parent_variable_id}' is stale."
                    )
                child_value = child_values.variables.get(output.source_variable_id)
                normalized = coerce_typed_value(
                    parent_variable.type,
                    child_value,
                    subject=f"Parent Variable '{parent_variable.name}'",
                )
                pending_outputs.append((parent_variable.id, normalized))
        except (FlowRuntimeValueError, ValueCoercionError) as exc:
            raise RuntimeError(f"run_flow: {exc}") from exc
        for parent_variable_id, value in pending_outputs:
            ctx.flow_values.variables.set(parent_variable_id, value)
        return

    flow_name = ctx.interpolate(cmd.params.get("flow_name", ""))
    flow = db.load_flow(flow_name)
    if flow is None:
        raise RuntimeError(f"Flow '{flow_name}' not found")
    if flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION:
        raise RuntimeError(
            "Version-1 Run Flow requires explicit Input/Output mappings and an "
            "isolated child context; that execution path is not available yet."
        )
    if ctx.listener:
        ctx.listener.on_log(f"Running sub-flow '{flow_name}'")
    ctx._executor.execute(flow, ctx)
