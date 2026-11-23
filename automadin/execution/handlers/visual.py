"""Smart visual-target command handlers."""

from __future__ import annotations

import math
import random
from time import monotonic, perf_counter

from automadin.execution.context import FlowContext
from automadin.execution.handlers.actions import _perform_click_at
from automadin.execution.handlers.detection import (
    _find_any_image,
    _resolve_image_names,
    _take_screenshot_for_device,
)
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType
from automadin.models.visual_rule import VisualRule
from automadin.services.dynamic_target import (
    TargetObservation,
    anchor_point,
    choose_associated,
    choose_initial,
    estimate_motion,
    point_in_box,
    predict_point,
)
from automadin.services.find_image import FindImageService
from automadin.services.image_search_region import prepare_search_region
from automadin.services.screen_text_region import resolve_fixed_box
from automadin.services.visual_detection import (
    VisualBlob,
    decode_screenshot,
    detect_blobs,
    detect_blobs_in_scope,
)


def _distance_point_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    nearest_x = x1 + t * dx
    nearest_y = y1 + t * dy
    return math.hypot(px - nearest_x, py - nearest_y)


def _load_rule(ctx: FlowContext, name: str, label: str) -> VisualRule | None:
    if not name:
        return None
    db = ctx._executor._db
    if db is None:
        raise RuntimeError("database not available")
    rule = db.load_visual_rule(name)
    if rule is None:
        raise RuntimeError(f"{label} visual rule not found: {name}")
    return rule


def _resolve_scalar(
    cmd: FlowCommand,
    ctx: FlowContext,
    name: str,
    expected_type: ValueType,
    default: object,
) -> object:
    raw_value = cmd.params.get(name, default)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    return resolver(raw_value, expected_type) if callable(resolver) else raw_value


def _source_point(cmd: FlowCommand, ctx: FlowContext, image) -> tuple[int, int]:
    mode = str(cmd.params.get("source_mode", "last_clicked_target"))
    height, width = image.shape[:2]
    if mode == "last_clicked_target":
        last_x = ctx.get_variable("_visual_last_target_x")
        last_y = ctx.get_variable("_visual_last_target_y")
        if last_x and last_y:
            return int(float(last_x)), int(float(last_y))
        fallback = str(cmd.params.get("source_fallback", "screen_center"))
        if fallback != "fixed_position":
            return width // 2, height // 2
    if mode == "screen_center":
        return width // 2, height // 2
    source_x = _resolve_scalar(
        cmd, ctx, "source_x", ValueType.NUMBER, width // 2
    )
    source_y = _resolve_scalar(
        cmd, ctx, "source_y", ValueType.NUMBER, height // 2
    )
    return int(float(source_x)), int(float(source_y))


def _choose_fallback(
    targets: list[VisualBlob],
    source: tuple[int, int],
    mode: str,
) -> VisualBlob | None:
    if mode == "none":
        return None
    if mode == "random":
        return random.choice(targets)
    if mode == "largest":
        return max(targets, key=lambda blob: blob.area)
    return min(
        targets,
        key=lambda blob: math.hypot(blob.center_x - source[0], blob.center_y - source[1]),
    )


def _choose_target(
    targets: list[VisualBlob],
    bonuses: list[VisualBlob],
    source: tuple[int, int],
    scoring_mode: str,
    fallback_mode: str,
    path_tolerance: float,
) -> tuple[VisualBlob | None, VisualBlob | None, float]:
    if not targets:
        raise RuntimeError("No target blobs found")
    if scoring_mode in {"nearest", "largest", "random"}:
        return _choose_fallback(targets, source, scoring_mode), None, 0.0
    if scoring_mode != "closest_to_bonus_path" or not bonuses:
        return _choose_fallback(targets, source, fallback_mode), None, 0.0

    best: tuple[VisualBlob, VisualBlob | None, float, float] | None = None
    for target in targets:
        end = (target.center_x, target.center_y)
        nearest_bonus: VisualBlob | None = None
        nearest_distance = float("inf")
        for bonus in bonuses:
            dist = _distance_point_to_segment(
                (bonus.center_x, bonus.center_y),
                source,
                end,
            )
            if dist < nearest_distance:
                nearest_distance = dist
                nearest_bonus = bonus
        if nearest_bonus is None:
            continue
        score = path_tolerance - nearest_distance
        if best is None or score > best[2]:
            best = (target, nearest_bonus, score, nearest_distance)

    if best is not None and best[2] >= 0:
        return best[0], best[1], best[3]
    return _choose_fallback(targets, source, fallback_mode), None, 0.0


def _clamp(value: float, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(round(value))))


def _dynamic_candidates(
    cmd: FlowCommand,
    ctx: FlowContext,
    screenshot: bytes,
    timestamp: float,
) -> tuple[list[TargetObservation], tuple[int, int]]:
    image = decode_screenshot(screenshot)
    height, width = image.shape[:2]
    source = str(cmd.params.get("target_source", "images"))
    if source == "images":
        images = _resolve_image_names(cmd, ctx)
        if not images:
            raise RuntimeError("click_dynamic_target: add or record target poses")
        threshold = float(
            _resolve_scalar(cmd, ctx, "threshold", ValueType.NUMBER, 0.78)
        )
        color_match = bool(
            _resolve_scalar(cmd, ctx, "color_match", ValueType.BOOLEAN, False)
        )
        color_tolerance = float(
            _resolve_scalar(cmd, ctx, "color_tolerance", ValueType.NUMBER, 0.3)
        )
        matched_name, result = _find_any_image(
            ctx,
            images,
            screenshot,
            threshold,
            color_match,
            color_tolerance,
            choose_best=True,
        )
        if result is None:
            return [], (width, height)
        return [
            TargetObservation(
                timestamp,
                int(result.x),
                int(result.y),
                int(result.width),
                int(result.height),
                float(result.confidence),
                str(matched_name or ""),
            )
        ], (width, height)

    if source != "visual_rule":
        raise RuntimeError("click_dynamic_target: target source is invalid")
    rule_name = str(cmd.params.get("target_rule", "")).strip()
    rule = _load_rule(ctx, rule_name, "Target")
    if rule is None:
        raise RuntimeError("click_dynamic_target: choose a visual rule")
    db = ctx._executor._db
    if db is None:
        raise RuntimeError("database not available")

    def resolve(name: str, default: object, value_type: ValueType) -> object:
        return _resolve_scalar(cmd, ctx, name, value_type, default)

    matcher = FindImageService()

    def match_anchor(name: str, png: bytes):
        template = db.load_image(name)
        if template is None:
            return None
        return matcher.find(
            png,
            template,
            float(resolve("search_anchor_threshold", 0.9, ValueType.NUMBER)),
            bool(resolve("search_anchor_color_match", False, ValueType.BOOLEAN)),
            float(resolve("search_anchor_color_tolerance", 0.3, ValueType.NUMBER)),
        )

    region = prepare_search_region(
        screenshot,
        cmd.params,
        db,
        resolve_value=resolve,
        anchor_image_names=[
            ctx.interpolate(name)
            for name in cmd.params.get("search_anchor_images", [])
            if isinstance(name, str) and name
        ],
        match_image=match_anchor,
    )
    blobs = detect_blobs_in_scope(
        image,
        rule,
        (region.box.left, region.box.top, region.box.right, region.box.bottom),
    )
    observations = [
        TargetObservation(
            timestamp,
            blob.rect[0],
            blob.rect[1],
            blob.rect[2],
            blob.rect[3],
            min(1.0, max(0.0, blob.roundness)),
            rule.name,
        )
        for blob in blobs
    ]
    return observations, (width, height)


def _dynamic_trigger_box(
    cmd: FlowCommand,
    ctx: FlowContext,
    surface_size: tuple[int, int],
) -> tuple[float, float, float, float]:
    width, height = surface_size
    unit = str(cmd.params.get("trigger_coordinate_unit", "percent"))
    box = resolve_fixed_box(
        float(_resolve_scalar(cmd, ctx, "trigger_start_x", ValueType.NUMBER, 40)),
        float(_resolve_scalar(cmd, ctx, "trigger_start_y", ValueType.NUMBER, 40)),
        float(_resolve_scalar(cmd, ctx, "trigger_end_x", ValueType.NUMBER, 60)),
        float(_resolve_scalar(cmd, ctx, "trigger_end_y", ValueType.NUMBER, 60)),
        unit=unit,
        surface_size=(width, height),
    )
    return float(box.left), float(box.top), float(box.right), float(box.bottom)


def _hook_seed_observation(
    cmd: FlowCommand,
    ctx: FlowContext,
    timestamp: float,
) -> TargetObservation | None:
    """Reuse a matching hook pose only while its scoped evidence is active."""
    if str(cmd.params.get("target_source", "images")) != "images":
        return None
    images = _resolve_image_names(cmd, ctx)
    values = ctx.flow_values
    if values is not None:
        matched = values.current_builtin("_matched_image")
        match_x = values.current_builtin("_match_x")
        match_y = values.current_builtin("_match_y")
        confidence = values.current_builtin("_match_confidence")
        if not all((matched, match_x, match_y, confidence)):
            return None
        image_name = str(matched.value)
        center_x = int(match_x.value)
        center_y = int(match_y.value)
        score = float(confidence.value)
    else:
        image_name = str(ctx.get_variable("_matched_image") or "")
        if not image_name:
            return None
        try:
            center_x = int(float(ctx.get_variable("_match_x") or ""))
            center_y = int(float(ctx.get_variable("_match_y") or ""))
            score = float(ctx.get_variable("_match_confidence") or "")
        except ValueError:
            return None
    if image_name not in images:
        return None
    db = ctx._executor._db
    asset = db.load_image_asset(image_name) if db is not None else None
    if asset is None or asset.width < 2 or asset.height < 2:
        return None
    return TargetObservation(
        timestamp,
        center_x - asset.width // 2,
        center_y - asset.height // 2,
        asset.width,
        asset.height,
        score,
        image_name,
    )


def handle_click_dynamic_target(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Acquire an animated target, then click its latest, entry, or predicted point."""
    started = perf_counter()
    strategy = str(cmd.params.get("strategy", "latest"))
    timeout_ms = max(
        100,
        int(_resolve_scalar(cmd, ctx, "timeout_ms", ValueType.DURATION_MS, 5000)),
    )
    interval_ms = max(
        20,
        int(_resolve_scalar(cmd, ctx, "frame_interval_ms", ValueType.DURATION_MS, 100)),
    )
    confirmation_frames = max(
        1,
        min(
            10,
            int(_resolve_scalar(cmd, ctx, "confirmation_frames", ValueType.INTEGER, 2)),
        ),
    )
    observation_count = max(
        2,
        min(
            10,
            int(_resolve_scalar(cmd, ctx, "observation_count", ValueType.INTEGER, 3)),
        ),
    )
    missed_tolerance = max(
        0,
        min(
            10,
            int(_resolve_scalar(cmd, ctx, "missed_frame_tolerance", ValueType.INTEGER, 1)),
        ),
    )
    max_movement = max(
        1.0,
        float(_resolve_scalar(cmd, ctx, "max_movement_px", ValueType.NUMBER, 250.0)),
    )
    preview_only = bool(
        _resolve_scalar(cmd, ctx, "preview_only", ValueType.BOOLEAN, False)
    )
    anchor = str(cmd.params.get("target_anchor", "center"))
    offset_x = float(
        _resolve_scalar(cmd, ctx, "offset_x_percent", ValueType.NUMBER, 0.0)
    )
    offset_y = float(
        _resolve_scalar(cmd, ctx, "offset_y_percent", ValueType.NUMBER, 0.0)
    )

    observations: list[TargetObservation] = []
    previous = _hook_seed_observation(cmd, ctx, monotonic())
    if previous is not None:
        observations.append(previous)
        if ctx.listener:
            ctx.listener.on_log(
                f"Dynamic target reused hook match '{previous.identity}' as its first observation"
            )
    misses = 0
    surface_size = (0, 0)
    click_point: tuple[float, float] | None = None
    motion = None
    deadline = monotonic() + timeout_ms / 1000.0

    while not ctx.is_stopped and monotonic() < deadline:
        frame_started = monotonic()
        screenshot = _take_screenshot_for_device(ctx)
        if screenshot is None:
            raise RuntimeError("click_dynamic_target: failed to take screenshot")
        ctx._last_screenshot = screenshot
        candidates, surface_size = _dynamic_candidates(
            cmd, ctx, screenshot, frame_started
        )
        chosen = (
            choose_initial(candidates)
            if previous is None
            else choose_associated(
                candidates,
                previous,
                max_movement_px=max_movement,
            )
        )
        if chosen is None:
            misses += 1
            if misses > missed_tolerance:
                previous = None
                observations.clear()
            remaining = interval_ms / 1000.0 - (monotonic() - frame_started)
            if remaining > 0 and not ctx.interruptible_sleep(remaining):
                break
            continue

        misses = 0
        previous = chosen
        observations.append(chosen)
        observations = observations[-10:]
        latest_point = anchor_point(
            chosen,
            anchor,
            offset_x_percent=offset_x,
            offset_y_percent=offset_y,
        )

        if strategy == "latest" and len(observations) >= confirmation_frames:
            click_point = latest_point
        elif strategy == "intercept" and len(observations) >= confirmation_frames:
            trigger_box = _dynamic_trigger_box(cmd, ctx, surface_size)
            if point_in_box(latest_point, trigger_box):
                if str(cmd.params.get("intercept_click", "target")) == "fixed":
                    fixed_x = float(
                        _resolve_scalar(
                            cmd, ctx, "fixed_click_x", ValueType.NUMBER, latest_point[0]
                        )
                    )
                    fixed_y = float(
                        _resolve_scalar(
                            cmd, ctx, "fixed_click_y", ValueType.NUMBER, latest_point[1]
                        )
                    )
                    if str(cmd.params.get("trigger_coordinate_unit", "percent")) == "percent":
                        fixed_x = fixed_x / 100.0 * surface_size[0]
                        fixed_y = fixed_y / 100.0 * surface_size[1]
                    click_point = fixed_x, fixed_y
                else:
                    click_point = latest_point
        elif strategy == "predict" and len(observations) >= observation_count:
            motion = estimate_motion(observations[-observation_count:])
            minimum_speed = float(
                _resolve_scalar(cmd, ctx, "minimum_speed_px_s", ValueType.NUMBER, 20.0)
            )
            max_residual = float(
                _resolve_scalar(cmd, ctx, "maximum_residual_px", ValueType.NUMBER, 18.0)
            )
            if (
                motion is not None
                and motion.speed >= minimum_speed
                and motion.residual_px <= max_residual
            ):
                predicted_center = predict_point(
                    chosen,
                    motion,
                    lead_time_ms=float(
                        _resolve_scalar(
                            cmd, ctx, "lead_time_ms", ValueType.DURATION_MS, 150.0
                        )
                    ),
                    max_distance_px=float(
                        _resolve_scalar(
                            cmd,
                            ctx,
                            "max_prediction_distance_px",
                            ValueType.NUMBER,
                            200.0,
                        )
                    ),
                )
                click_point = (
                    predicted_center[0] + latest_point[0] - chosen.center_x,
                    predicted_center[1] + latest_point[1] - chosen.center_y,
                )
            elif str(cmd.params.get("prediction_fallback", "no_click")) == "latest":
                click_point = latest_point
        else:
            if strategy not in {"latest", "intercept", "predict"}:
                raise RuntimeError("click_dynamic_target: click strategy is invalid")

        if click_point is not None:
            break
        remaining = interval_ms / 1000.0 - (monotonic() - frame_started)
        if remaining > 0 and not ctx.interruptible_sleep(remaining):
            break

    if click_point is None or previous is None:
        raise RuntimeError(
            "click_dynamic_target: target was not confirmed before the timeout"
        )

    width, height = surface_size
    click_x = _clamp(click_point[0], 0, width - 1)
    click_y = _clamp(click_point[1], 0, height - 1)
    if not preview_only:
        _perform_click_at(
            ctx,
            click_x,
            click_y,
            click_type=str(cmd.params.get("click", "single")),
            hold_duration_ms=int(
                _resolve_scalar(cmd, ctx, "hold_duration_ms", ValueType.DURATION_MS, 0)
            ),
        )

    ctx.set_variable("_dynamic_target_x", str(click_x))
    ctx.set_variable("_dynamic_target_y", str(click_y))
    ctx.set_variable("_dynamic_target_confidence", f"{previous.confidence:.4f}")
    ctx.set_variable("_dynamic_target_frames", str(len(observations)))
    ctx.set_variable("_dynamic_target_identity", previous.identity)
    if motion is not None:
        ctx.set_variable("_dynamic_target_velocity_x_px_s", f"{motion.velocity_x:.1f}")
        ctx.set_variable("_dynamic_target_velocity_y_px_s", f"{motion.velocity_y:.1f}")
    total_ms = (perf_counter() - started) * 1000.0
    ctx.set_variable("_dynamic_target_timing_total_ms", f"{total_ms:.1f}")
    if ctx.listener:
        verb = "Previewed" if preview_only else "Clicked"
        ctx.listener.on_log(
            f"{verb} dynamic target at ({click_x}, {click_y}); "
            f"strategy={strategy}, frames={len(observations)}, "
            f"confidence={previous.confidence:.3f}"
        )


def handle_click_best_target(cmd: FlowCommand, ctx: FlowContext) -> None:
    started_at = perf_counter()
    target_rule_name = str(cmd.params.get("target_rule", "")).strip()
    bonus_rule_name = str(cmd.params.get("bonus_rule", "")).strip()
    scoring_mode = str(cmd.params.get("scoring_mode", "closest_to_bonus_path"))
    # Keep an omitted parameter aligned with the metadata's safety-first
    # default: do not click an unrelated target when no bonus-path match exists.
    fallback_mode = str(cmd.params.get("fallback_mode", "none"))
    path_tolerance = float(
        _resolve_scalar(cmd, ctx, "path_tolerance_px", ValueType.NUMBER, 30.0)
    )
    preview_only = bool(
        _resolve_scalar(cmd, ctx, "preview_only", ValueType.BOOLEAN, False)
    )
    log_timing = bool(
        _resolve_scalar(cmd, ctx, "log_timing", ValueType.BOOLEAN, False)
    )
    click_type = str(cmd.params.get("click", "single"))
    hold_duration_ms = int(
        _resolve_scalar(cmd, ctx, "hold_duration_ms", ValueType.DURATION_MS, 0)
    )

    target_rule = _load_rule(ctx, target_rule_name, "Target")
    if target_rule is None:
        raise RuntimeError("click_best_target: target_rule is required")
    bonus_rule = _load_rule(ctx, bonus_rule_name, "Bonus")

    screenshot_started_at = perf_counter()
    screenshot = _take_screenshot_for_device(ctx)
    screenshot_ms = (perf_counter() - screenshot_started_at) * 1000.0
    if screenshot is None:
        raise RuntimeError("click_best_target: failed to take screenshot")
    ctx._last_screenshot = screenshot

    decode_started_at = perf_counter()
    image = decode_screenshot(screenshot)
    decode_ms = (perf_counter() - decode_started_at) * 1000.0
    source = _source_point(cmd, ctx, image)

    target_detect_started_at = perf_counter()
    targets = detect_blobs(image, target_rule)
    target_detect_ms = (perf_counter() - target_detect_started_at) * 1000.0

    bonus_detect_started_at = perf_counter()
    bonuses = detect_blobs(image, bonus_rule) if bonus_rule is not None else []
    bonus_detect_ms = (perf_counter() - bonus_detect_started_at) * 1000.0

    if not targets:
        total_ms = (perf_counter() - started_at) * 1000.0
        ctx.set_variable("_visual_target_count", "0")
        ctx.set_variable("_visual_bonus_count", str(len(bonuses)))
        ctx.set_variable("_visual_timing_screenshot_ms", f"{screenshot_ms:.1f}")
        ctx.set_variable("_visual_timing_decode_ms", f"{decode_ms:.1f}")
        ctx.set_variable("_visual_timing_target_detect_ms", f"{target_detect_ms:.1f}")
        ctx.set_variable("_visual_timing_bonus_detect_ms", f"{bonus_detect_ms:.1f}")
        ctx.set_variable("_visual_timing_choose_ms", "0.0")
        ctx.set_variable("_visual_timing_click_ms", "0.0")
        ctx.set_variable("_visual_timing_total_ms", f"{total_ms:.1f}")
        if log_timing and ctx.listener:
            ctx.listener.on_log(
                "click_best_target timing before no-target error: "
                f"screenshot={screenshot_ms:.1f}ms, "
                f"decode={decode_ms:.1f}ms, "
                f"target_detect={target_detect_ms:.1f}ms, "
                f"bonus_detect={bonus_detect_ms:.1f}ms, "
                f"total={total_ms:.1f}ms"
            )
        raise RuntimeError("No target blobs found")

    choose_started_at = perf_counter()
    target, bonus, distance = _choose_target(
        targets,
        bonuses,
        source,
        scoring_mode,
        fallback_mode,
        path_tolerance,
    )
    choose_ms = (perf_counter() - choose_started_at) * 1000.0

    if target is None:
        total_ms = (perf_counter() - started_at) * 1000.0
        ctx.set_variable("_visual_target_count", str(len(targets)))
        ctx.set_variable("_visual_bonus_count", str(len(bonuses)))
        ctx.set_variable("_visual_chosen_x", "")
        ctx.set_variable("_visual_chosen_y", "")
        ctx.set_variable("_visual_bonus_x", "")
        ctx.set_variable("_visual_bonus_y", "")
        ctx.set_variable("_visual_timing_screenshot_ms", f"{screenshot_ms:.1f}")
        ctx.set_variable("_visual_timing_decode_ms", f"{decode_ms:.1f}")
        ctx.set_variable("_visual_timing_target_detect_ms", f"{target_detect_ms:.1f}")
        ctx.set_variable("_visual_timing_bonus_detect_ms", f"{bonus_detect_ms:.1f}")
        ctx.set_variable("_visual_timing_choose_ms", f"{choose_ms:.1f}")
        ctx.set_variable("_visual_timing_click_ms", "0.0")
        ctx.set_variable("_visual_timing_total_ms", f"{total_ms:.1f}")
        if ctx.listener:
            ctx.listener.on_log(
                "Skipped best target click; no bonus path matched and fallback is none "
                f"(targets={len(targets)}, bonuses={len(bonuses)})"
            )
            if log_timing:
                ctx.listener.on_log(
                    "click_best_target timing: "
                    f"screenshot={screenshot_ms:.1f}ms, "
                    f"decode={decode_ms:.1f}ms, "
                    f"target_detect={target_detect_ms:.1f}ms, "
                    f"bonus_detect={bonus_detect_ms:.1f}ms, "
                    f"choose={choose_ms:.1f}ms, "
                    "click=0.0ms, "
                    f"total={total_ms:.1f}ms"
                )
        return

    click_ms = 0.0
    if not preview_only:
        click_started_at = perf_counter()
        _perform_click_at(
            ctx,
            target.center_x,
            target.center_y,
            click_type=click_type,
            hold_duration_ms=hold_duration_ms,
        )
        click_ms = (perf_counter() - click_started_at) * 1000.0
    total_ms = (perf_counter() - started_at) * 1000.0

    ctx.set_variable("_visual_last_target_x", str(target.center_x))
    ctx.set_variable("_visual_last_target_y", str(target.center_y))
    ctx.set_variable("_visual_target_count", str(len(targets)))
    ctx.set_variable("_visual_bonus_count", str(len(bonuses)))
    ctx.set_variable("_visual_chosen_x", str(target.center_x))
    ctx.set_variable("_visual_chosen_y", str(target.center_y))
    ctx.set_variable("_visual_bonus_x", str(bonus.center_x) if bonus is not None else "")
    ctx.set_variable("_visual_bonus_y", str(bonus.center_y) if bonus is not None else "")
    ctx.set_variable("_visual_timing_screenshot_ms", f"{screenshot_ms:.1f}")
    ctx.set_variable("_visual_timing_decode_ms", f"{decode_ms:.1f}")
    ctx.set_variable("_visual_timing_target_detect_ms", f"{target_detect_ms:.1f}")
    ctx.set_variable("_visual_timing_bonus_detect_ms", f"{bonus_detect_ms:.1f}")
    ctx.set_variable("_visual_timing_choose_ms", f"{choose_ms:.1f}")
    ctx.set_variable("_visual_timing_click_ms", f"{click_ms:.1f}")
    ctx.set_variable("_visual_timing_total_ms", f"{total_ms:.1f}")

    if ctx.listener:
        bonus_text = (
            f", bonus path distance {distance:.1f}px" if bonus is not None else ""
        )
        verb = "Previewed" if preview_only else "Clicked"
        ctx.listener.on_log(
            f"{verb} best target at ({target.center_x}, {target.center_y}); "
            f"targets={len(targets)}, bonuses={len(bonuses)}{bonus_text}"
        )
        if log_timing:
            ctx.listener.on_log(
                "click_best_target timing: "
                f"screenshot={screenshot_ms:.1f}ms, "
                f"decode={decode_ms:.1f}ms, "
                f"target_detect={target_detect_ms:.1f}ms, "
                f"bonus_detect={bonus_detect_ms:.1f}ms, "
                f"choose={choose_ms:.1f}ms, "
                f"click={click_ms:.1f}ms, "
                f"total={total_ms:.1f}ms"
            )
