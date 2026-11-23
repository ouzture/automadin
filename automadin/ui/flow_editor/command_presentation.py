"""Declarative presentation metadata for command parameter editors.

This module controls only editor layout. Runtime command definitions remain the
authoritative source for parameter types, defaults, and execution behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


FieldWidth = Literal["compact", "short", "medium", "full"]


@dataclass(frozen=True)
class VisibilityRule:
    parameter: str | None = None
    equals: object = None
    matches_any: bool = False
    all_of: tuple["VisibilityRule", ...] = ()
    any_of: tuple["VisibilityRule", ...] = ()


@dataclass(frozen=True)
class FieldPresentation:
    name: str
    width: FieldWidth = "medium"
    label: str = ""
    visible_when: VisibilityRule | None = None


@dataclass(frozen=True)
class PresentationRow:
    fields: tuple[FieldPresentation, ...]
    label: str = ""
    visible_when: VisibilityRule | None = None


@dataclass(frozen=True)
class PresentationSection:
    key: str
    label: str
    rows: tuple[PresentationRow, ...]
    default_expanded: bool = False
    summary: str = ""
    description: str = ""
    visible_when: VisibilityRule | None = None
    include_error_policy: bool = False


@dataclass(frozen=True)
class CommandPresentation:
    layout: Literal["guided", "compact"]
    rows: tuple[PresentationRow, ...] = ()
    sections: tuple[PresentationSection, ...] = ()


def field(
    name: str,
    width: FieldWidth = "medium",
    *,
    label: str = "",
    when: tuple[str, object] | None = None,
    when_any: tuple[str, tuple[object, ...]] | None = None,
    when_rule: VisibilityRule | None = None,
) -> FieldPresentation:
    supplied_rules = sum(
        item is not None for item in (when, when_any, when_rule)
    )
    if supplied_rules > 1:
        raise ValueError("A field can use only one visibility condition")
    visibility = when_rule
    if when is not None:
        visibility = VisibilityRule(*when)
    elif when_any is not None:
        visibility = VisibilityRule(when_any[0], when_any[1], matches_any=True)
    return FieldPresentation(
        name=name,
        width=width,
        label=label,
        visible_when=visibility,
    )


def visible_when_any(parameter: str, *values: object) -> VisibilityRule:
    return VisibilityRule(parameter, values, matches_any=True)


def all_visible_rules(*rules: VisibilityRule) -> VisibilityRule:
    return VisibilityRule(all_of=rules)


def any_visible_rule(*rules: VisibilityRule) -> VisibilityRule:
    return VisibilityRule(any_of=rules)


def equals_rule(parameter: str, value: object) -> VisibilityRule:
    return VisibilityRule(parameter, value)


IMAGE_TARGET_SECTION = PresentationSection(
    key="target",
    label="Target",
    default_expanded=True,
    summary="Images and matching rules",
    rows=(
        PresentationRow((field("images", "full"),)),
        PresentationRow(
            (
                field("threshold", "short", label="Confidence (0–1)"),
                field("color_match", "medium"),
                field(
                    "color_tolerance",
                    "short",
                    label="Tolerance (0–1)",
                    when=("color_match", True),
                ),
            ),
            label="Matching",
        ),
    ),
)


IMAGE_SCOPE_SECTION = PresentationSection(
    key="scope",
    label="Scope",
    summary="Full screen",
    rows=(
        PresentationRow((field("search_region_mode", "short"),)),
        PresentationRow(
            (
                field(
                    "search_coordinate_unit",
                    "short",
                    when=("search_region_mode", "fixed"),
                ),
                field(
                    "search_start_x",
                    "medium",
                    when=("search_region_mode", "fixed"),
                ),
                field(
                    "search_start_y",
                    "medium",
                    when=("search_region_mode", "fixed"),
                ),
            ),
            label="Fixed region start",
        ),
        PresentationRow(
            (
                field(
                    "search_end_x",
                    "medium",
                    when=("search_region_mode", "fixed"),
                ),
                field(
                    "search_end_y",
                    "medium",
                    when=("search_region_mode", "fixed"),
                ),
            ),
            label="Fixed region end",
        ),
        PresentationRow(
            (
                field(
                    "search_anchor_images",
                    "full",
                    when=("search_region_mode", "image_anchor"),
                ),
            ),
            label="Anchor target",
            visible_when=VisibilityRule("search_region_mode", "image_anchor"),
        ),
        PresentationRow(
            (
                field(
                    "search_anchor_threshold",
                    "short",
                    when=("search_region_mode", "image_anchor"),
                ),
                field(
                    "search_anchor_color_match",
                    "medium",
                    when=("search_region_mode", "image_anchor"),
                ),
                field(
                    "search_anchor_color_tolerance",
                    "short",
                    when=("search_anchor_color_match", True),
                ),
            ),
            label="Anchor matching",
            visible_when=VisibilityRule("search_region_mode", "image_anchor"),
        ),
        PresentationRow(
            (
                field(
                    "search_anchor",
                    "short",
                    when=("search_region_mode", "image_anchor"),
                ),
                field(
                    "search_offset_x",
                    "medium",
                    when=("search_region_mode", "image_anchor"),
                ),
                field(
                    "search_offset_y",
                    "medium",
                    when=("search_region_mode", "image_anchor"),
                ),
            ),
            label="Anchor placement",
            visible_when=VisibilityRule("search_region_mode", "image_anchor"),
        ),
        PresentationRow(
            (
                field(
                    "search_region_width",
                    "medium",
                    when=("search_region_mode", "image_anchor"),
                ),
                field(
                    "search_region_height",
                    "medium",
                    when=("search_region_mode", "image_anchor"),
                ),
            ),
            label="Anchored region size",
            visible_when=VisibilityRule("search_region_mode", "image_anchor"),
        ),
    ),
)


_BEST_TARGET_SOURCE_SCORING = visible_when_any(
    "scoring_mode",
    "closest_to_bonus_path",
    "nearest",
)
_BEST_TARGET_FIXED_SOURCE = all_visible_rules(
    _BEST_TARGET_SOURCE_SCORING,
    any_visible_rule(
        equals_rule("source_mode", "fixed_position"),
        all_visible_rules(
            equals_rule("source_mode", "last_clicked_target"),
            equals_rule("source_fallback", "fixed_position"),
        ),
    ),
)


def _timing_section(
    *names: str,
    default_expanded: bool = False,
) -> PresentationSection:
    """Build the shared compact timing section used by browser commands."""
    labels = {
        "timeout": "Wait up to",
        "pause_ms": "Pause Before",
        "min_wait_time": "Minimum Wait",
        "min_delay": "Minimum Key Delay",
        "max_delay": "Maximum Key Delay",
        "delay_ms": "Character Delay",
    }
    fields = tuple(
        field(name, "compact", label=labels.get(name, "")) for name in names
    )
    return PresentationSection(
        key="timing",
        label="Timing",
        default_expanded=default_expanded,
        summary="Default timing",
        rows=(PresentationRow(fields),),
    )


def _compact_command(*names: str) -> CommandPresentation:
    return CommandPresentation(
        layout="compact",
        rows=(
            PresentationRow(
                tuple(
                    field(
                        name,
                        "full" if name in {"url", "text"} else "compact",
                    )
                    for name in names
                )
            ),
        ),
    )


_PAGE_MATCH_STRATEGIES = (
    "url_exact",
    "url_contains",
    "title_exact",
    "title_contains",
)


_COMPARISONS_WITH_RIGHT = (
    "equals",
    "not_equals",
    "greater_than",
    "greater_or_equal",
    "less_than",
    "less_or_equal",
    "contains",
    "not_contains",
    "starts_with",
    "not_starts_with",
    "ends_with",
    "not_ends_with",
)
_TEXT_CASE_COMPARISONS = (
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "starts_with",
    "not_starts_with",
    "ends_with",
    "not_ends_with",
)
_VARIABLE_SOURCE_OPERATIONS = (
    "set",
    "add",
    "subtract",
    "multiply",
    "divide",
    "append",
    "prepend",
)


def _value_condition_section(*, include_left: bool) -> PresentationSection:
    first_row = (
        PresentationRow(
            (
                field("left", "full"),
                field("operator", "medium"),
            )
        )
        if include_left
        else PresentationRow((field("operator", "medium"),))
    )
    return PresentationSection(
        key="condition",
        label="Condition",
        default_expanded=True,
        summary="Typed comparison",
        description=(
            "Comparison choices are restricted by the selected value types. "
            "Text comparisons are case-sensitive unless Ignore Case is enabled."
        ),
        rows=(
            first_row,
            PresentationRow(
                (
                    field(
                        "right",
                        "full",
                        when_any=("operator", _COMPARISONS_WITH_RIGHT),
                    ),
                )
            ),
            PresentationRow(
                (
                    field(
                        "ignore_case",
                        "medium",
                        when_any=("operator", _TEXT_CASE_COMPARISONS),
                    ),
                )
            ),
        ),
    )


def _text_input_command() -> CommandPresentation:
    return CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="text",
                label="Text",
                default_expanded=True,
                summary="Text to type",
                rows=(PresentationRow((field("text", "full"),)),),
            ),
            PresentationSection(
                key="pacing",
                label="Typing Pace",
                summary="Default pacing",
                description=(
                    "Set Fixed Delay, or use Minimum and Maximum Delay for "
                    "randomized character pacing."
                ),
                rows=(
                    PresentationRow((field("delay_ms", "compact"),)),
                    PresentationRow(
                        (
                            field("min_delay", "compact"),
                            field("max_delay", "compact"),
                        )
                    ),
                ),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    )


def _key_input_command() -> CommandPresentation:
    return CommandPresentation(
        layout="compact",
        rows=(
            PresentationRow(
                (
                    field("keys", "full"),
                    field("mode", "medium"),
                )
            ),
            PresentationRow(
                (
                    field("pause_ms", "compact"),
                    field("min_wait_time", "compact"),
                )
            ),
        ),
    )


def _windows_key_input_command() -> CommandPresentation:
    return CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="keys",
                label="Keys",
                default_expanded=True,
                summary="Key sequence and delivery",
                rows=(
                    PresentationRow((field("keys", "full"),)),
                    PresentationRow(
                        (
                            field("mode", "medium"),
                            field("delivery_mode", "medium"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Default timing",
                rows=(
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        )
                    ),
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("delivery_mode", ("input",)),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("delivery_mode", ("input",)),
                            ),
                        )
                    ),
                ),
            ),
        ),
    )


def _windows_text_input_command() -> CommandPresentation:
    return CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="text",
                label="Text",
                default_expanded=True,
                summary="Text and delivery",
                rows=(
                    PresentationRow((field("text", "full"),)),
                    PresentationRow((field("delivery_mode", "medium"),)),
                ),
            ),
            PresentationSection(
                key="pacing",
                label="Typing Pace",
                summary="Default pacing",
                description=(
                    "Set Fixed Delay, or use Minimum and Maximum Delay for "
                    "randomized character pacing."
                ),
                rows=(
                    PresentationRow((field("delay_ms", "compact"),)),
                    PresentationRow(
                        (
                            field("min_delay", "compact"),
                            field("max_delay", "compact"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Default timing",
                rows=(
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        )
                    ),
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("delivery_mode", ("input",)),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("delivery_mode", ("input",)),
                            ),
                        )
                    ),
                ),
            ),
        ),
    )


_PHYSICAL_WINDOWS_INPUT_MODES = ("cursor", "input")


COMMAND_PRESENTATIONS: dict[str, CommandPresentation] = {
    "check_hooks": _compact_command("hooks"),
    "section": _compact_command("text"),
    "group": _compact_command("text"),
    "user_checkpoint": CommandPresentation(
        layout="compact",
        rows=(PresentationRow((field("message", "full"),)),),
    ),
    "send_notification": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="notification",
                label="Notification",
                default_expanded=True,
                summary="Message and urgency",
                description=(
                    "Sends through the selected destination configured under Tools."
                ),
                rows=(
                    PresentationRow((field("channel", "medium"),)),
                    PresentationRow(
                        (
                            field("title", "medium"),
                            field(
                                "priority",
                                "full",
                                when=("channel", "pushover"),
                            ),
                        ),
                    ),
                    PresentationRow((field("message", "full"),)),
                ),
            ),
            PresentationSection(
                key="emergency",
                label="Emergency Repeats",
                summary="Repeat until acknowledged",
                description=(
                    "Pushover repeats an emergency alert until it is acknowledged "
                    "on a device or the expiry time is reached."
                ),
                visible_when=all_visible_rules(
                    equals_rule("channel", "pushover"),
                    equals_rule("priority", "emergency"),
                ),
                rows=(
                    PresentationRow(
                        (
                            field("retry_seconds", "compact"),
                            field("expire_seconds", "compact"),
                        )
                    ),
                ),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "click_position": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="position",
                label="Position",
                default_expanded=True,
                summary="Screen coordinates",
                rows=(
                    PresentationRow((field("coordinate_unit", "short"),)),
                    PresentationRow(
                        (field("x", "compact"), field("y", "compact")),
                        label="Coordinates",
                    ),
                ),
            ),
            PresentationSection(
                key="action",
                label="Click",
                default_expanded=True,
                summary="Single click",
                rows=(PresentationRow((field("click", "medium"),)),),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "type_text": _text_input_command(),
    "clear_text_field": _compact_command(
        "selector", "pause_ms", "min_wait_time"
    ),
    "swipe": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="path",
                label="Swipe Path",
                default_expanded=True,
                summary="Start and end coordinates",
                rows=(
                    PresentationRow((field("coordinate_unit", "short"),)),
                    PresentationRow(
                        (
                            field("start_x", "compact"),
                            field("start_y", "compact"),
                        ),
                        label="Start",
                    ),
                    PresentationRow(
                        (
                            field("end_x", "compact"),
                            field("end_y", "compact"),
                        ),
                        label="End",
                    ),
                ),
            ),
            _timing_section("duration_ms", "pause_ms", "min_wait_time"),
        ),
    ),
    "press_keycode": _key_input_command(),
    "screenshot": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="capture",
                label="Capture",
                default_expanded=True,
                summary="Capture current device",
                rows=(PresentationRow((field("save_to_disk", "medium"),)),),
            ),
            PresentationSection(
                key="save",
                label="Save",
                summary="Save directory",
                visible_when=VisibilityRule("save_to_disk", True),
                rows=(PresentationRow((field("save_path", "full"),)),),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "terminate_flow": CommandPresentation(layout="compact"),
    "restart_flow": CommandPresentation(layout="compact"),
    "hibernate_computer": CommandPresentation(layout="compact"),
    "random_wait": CommandPresentation(
        layout="compact",
        rows=(
            PresentationRow(
                (
                    field("min_ms", "compact", label="Minimum"),
                    field("max_ms", "compact", label="Maximum"),
                    field("pause_ms", "compact"),
                )
            ),
        ),
    ),
    "android_install_apk": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="package",
                label="APK",
                default_expanded=True,
                summary="Local package file",
                rows=(PresentationRow((field("apk_path", "full"),)),),
            ),
            PresentationSection(
                key="install",
                label="Install Options",
                default_expanded=True,
                summary="Replace existing app",
                rows=(
                    PresentationRow(
                        (
                            field("replace_existing", "medium"),
                            field("grant_permissions", "medium"),
                        )
                    ),
                ),
            ),
            _timing_section("timeout"),
        ),
    ),
    "android_launch_app": _compact_command("package_name", "timeout"),
    "android_force_stop_app": _compact_command("package_name"),
    "android_restart_app": _compact_command("package_name", "timeout"),
    "android_start_activity": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="activity",
                label="Activity",
                default_expanded=True,
                summary="Package and activity class",
                rows=(
                    PresentationRow((field("package_name", "full"),)),
                    PresentationRow((field("activity_name", "full"),)),
                ),
            ),
            _timing_section("timeout"),
        ),
    ),
    "android_open_deep_link": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="link",
                label="Deep Link",
                default_expanded=True,
                summary="URI and optional package",
                rows=(
                    PresentationRow((field("uri", "full"),)),
                    PresentationRow((field("package_name", "full"),)),
                ),
            ),
            _timing_section("timeout"),
        ),
    ),
    "win_ensure_app": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="application",
                label="Application",
                default_expanded=True,
                summary="Saved target or executable override",
                rows=(
                    PresentationRow((field("executable_path", "full"),)),
                    PresentationRow((field("arguments", "full"),)),
                    PresentationRow((field("window_title", "full"),)),
                ),
            ),
            PresentationSection(
                key="readiness",
                label="Readiness",
                default_expanded=True,
                summary="Restore and focus",
                rows=(
                    PresentationRow(
                        (
                            field("focus", "medium"),
                            field("timeout", "compact"),
                        )
                    ),
                ),
            ),
        ),
    ),
    "win_click": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="position",
                label="Position",
                default_expanded=True,
                summary="Window coordinates",
                rows=(
                    PresentationRow((field("coordinate_unit", "short"),)),
                    PresentationRow(
                        (field("x", "compact"), field("y", "compact")),
                        label="Coordinates",
                    ),
                ),
            ),
            PresentationSection(
                key="action",
                label="Click",
                default_expanded=True,
                summary="Background left click",
                rows=(
                    PresentationRow(
                        (
                            field("button", "short"),
                            field("click", "short"),
                            field("mode", "medium"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Input timing",
                rows=(
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("mode", _PHYSICAL_WINDOWS_INPUT_MODES),
                            ),
                            field(
                                "click_duration_ms",
                                "compact",
                                when_any=("mode", _PHYSICAL_WINDOWS_INPUT_MODES),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("mode", _PHYSICAL_WINDOWS_INPUT_MODES),
                            ),
                        )
                    ),
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        )
                    ),
                ),
            ),
        ),
    ),
    "win_drag": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="start",
                label="Start",
                default_expanded=True,
                summary="Window coordinates",
                rows=(
                    PresentationRow((field("coordinate_unit", "short"),)),
                    PresentationRow(
                        (field("x", "compact"), field("y", "compact")),
                        label="Coordinates",
                    ),
                ),
            ),
            PresentationSection(
                key="drag",
                label="Drag",
                default_expanded=True,
                summary="Signed offset",
                rows=(
                    PresentationRow(
                        (
                            field("offset_unit", "short"),
                            field("xoffset", "compact"),
                            field("yoffset", "compact"),
                        )
                    ),
                    PresentationRow(
                        (field("button", "short"), field("mode", "medium"))
                    ),
                ),
            ),
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Drag timing",
                rows=(
                    PresentationRow(
                        (
                            field("drag_duration_ms", "compact"),
                            field("drag_steps", "compact"),
                        )
                    ),
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("mode", _PHYSICAL_WINDOWS_INPUT_MODES),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("mode", _PHYSICAL_WINDOWS_INPUT_MODES),
                            ),
                        )
                    ),
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        )
                    ),
                ),
            ),
        ),
    ),
    "win_key": _windows_key_input_command(),
    "win_type": _windows_text_input_command(),
    "win_scroll": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="scroll",
                label="Scroll",
                default_expanded=True,
                summary="Position and amount",
                rows=(
                    PresentationRow(
                        (
                            field("coordinate_unit", "short"),
                            field("amount", "compact"),
                        )
                    ),
                    PresentationRow(
                        (field("x", "compact"), field("y", "compact")),
                        label="Position",
                    ),
                    PresentationRow((field("delivery_mode", "medium"),)),
                ),
            ),
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Default timing",
                rows=(
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        )
                    ),
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("delivery_mode", ("input",)),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("delivery_mode", ("input",)),
                            ),
                        )
                    ),
                ),
            ),
        ),
    ),
    "win_focus": _compact_command("pause_ms", "min_wait_time"),
    "for": CommandPresentation(
        layout="compact",
        rows=(
            PresentationRow(
                (
                    field("count", "compact", label="Repeat Count"),
                    field("pause_ms", "compact"),
                    field("min_wait_time", "compact"),
                )
            ),
        ),
    ),
    "if_value": CommandPresentation(
        layout="guided",
        sections=(
            _value_condition_section(include_left=True),
            PresentationSection(
                key="diagnostics",
                label="Diagnostics",
                summary="Values hidden by default",
                description=(
                    "Enable only when comparison values are safe to include in "
                    "execution logs."
                ),
                rows=(PresentationRow((field("log_values", "full"),)),),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "while_value": CommandPresentation(
        layout="guided",
        sections=(
            _value_condition_section(include_left=True),
            _timing_section("timeout", "pause_ms", "min_wait_time"),
        ),
    ),
    "switch_value": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="source",
                label="Source",
                default_expanded=True,
                summary="Evaluate once",
                description=(
                    "Cases are checked from top to bottom. The first match runs; "
                    "Default runs only when no Case matches."
                ),
                rows=(PresentationRow((field("source", "full"),)),),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "switch_case": CommandPresentation(
        layout="compact",
        rows=_value_condition_section(include_left=False).rows,
    ),
    "switch_default": CommandPresentation(layout="compact"),
    "if_elapsed": CommandPresentation(
        layout="compact",
        rows=(
            PresentationRow(
                (
                    field("min_wait_time", "compact", label="Required Elapsed Time"),
                    field("pause_ms", "compact"),
                )
            ),
        ),
    ),
    "for_each": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="values",
                label="Values",
                default_expanded=True,
                summary="Ordered value list",
                description=(
                    "Each iteration exposes the current value as item and its "
                    "zero-based position as index."
                ),
                rows=(PresentationRow((field("values", "full"),)),),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "run_flow": CommandPresentation(
        layout="compact",
        rows=(
            PresentationRow(
                (
                    field("flow_name", "full"),
                    field("pause_ms", "compact"),
                    field("min_wait_time", "compact"),
                )
            ),
        ),
    ),
    "update_variable": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="update",
                label="Variable Update",
                default_expanded=True,
                summary="Target and operation",
                rows=(
                    PresentationRow((field("target_variable_id", "full"),)),
                    PresentationRow((field("operation", "medium"),)),
                    PresentationRow(
                        (field("log_values", "full"),),
                        label="Diagnostics",
                    ),
                ),
            ),
            PresentationSection(
                key="value",
                label="Value",
                default_expanded=True,
                summary="Operation source",
                visible_when=visible_when_any(
                    "operation", *_VARIABLE_SOURCE_OPERATIONS
                ),
                rows=(PresentationRow((field("source", "full"),)),),
            ),
            PresentationSection(
                key="replace",
                label="Replace Text",
                default_expanded=True,
                summary="Find and replace",
                visible_when=VisibilityRule("operation", "replace"),
                rows=(
                    PresentationRow((field("find", "full"),)),
                    PresentationRow((field("replacement", "full"),)),
                ),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "for_each_browser_element": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="collection",
                label="Element Collection",
                default_expanded=True,
                summary="Collection and optional container",
                description=(
                    "The collection is snapshotted when the loop starts. "
                    "Use the optional container to limit matching to one part of the page."
                ),
                rows=(
                    PresentationRow((field("locator", "full"),)),
                    PresentationRow((field("container_locator", "full"),)),
                ),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "navigate_to": _compact_command("url", "timeout", "pause_ms", "min_wait_time"),
    "go_back": _compact_command("timeout", "pause_ms", "min_wait_time"),
    "go_forward": _compact_command("timeout", "pause_ms", "min_wait_time"),
    "reload_page": _compact_command("timeout", "pause_ms", "min_wait_time"),
    "open_new_tab": _compact_command("url", "timeout", "pause_ms", "min_wait_time"),
    "close_tab": _compact_command("pause_ms", "min_wait_time"),
    "close_all_tabs": _compact_command("pause_ms", "min_wait_time"),
    "switch_tab": _compact_command("index", "pause_ms", "min_wait_time"),
    "select_browser_page": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="selection",
                label="Page Selection",
                default_expanded=True,
                summary="Durable page rule",
                description=(
                    "Choose a live page by relationship, recency, URL, or title. "
                    "This avoids saving a session-local tab identifier."
                ),
                rows=(
                    PresentationRow((field("strategy", "medium"),)),
                    PresentationRow(
                        (
                            field(
                                "value",
                                "full",
                                when_any=("strategy", _PAGE_MATCH_STRATEGIES),
                            ),
                        )
                    ),
                    PresentationRow(
                        (
                            field(
                                "match_case",
                                "medium",
                                when_any=("strategy", _PAGE_MATCH_STRATEGIES),
                            ),
                        )
                    ),
                ),
            ),
        ),
    ),
    "ensure_browser_page": _compact_command("url", "timeout"),
    "click_element": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="target",
                label="Target",
                default_expanded=True,
                summary="Locator or current loop element",
                description=(
                    "Current Element is available inside For Each Browser Element. "
                    "Use Web Inspector to author and test an ordinary locator."
                ),
                rows=(
                    PresentationRow((field("target", "medium"),)),
                    PresentationRow(
                        (field("locator", "full", when=("target", "locator")),)
                    ),
                ),
            ),
            PresentationSection(
                key="action",
                label="Click",
                default_expanded=True,
                summary="Centre click",
                rows=(
                    PresentationRow(
                        (
                            field("click_anchor", "medium"),
                            field("offset_unit", "short"),
                        )
                    ),
                    PresentationRow(
                        (
                            field("offset_x", "compact"),
                            field("offset_y", "compact"),
                        ),
                        label="Signed offset",
                    ),
                    PresentationRow((field("require_new_child_tab", "full"),)),
                ),
            ),
            _timing_section("timeout"),
        ),
    ),
    "wait_for_selector": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="target",
                label="Element",
                default_expanded=True,
                summary="Resilient locator",
                description="Use Web Inspector to author and test the locator.",
                rows=(PresentationRow((field("locator", "full"),)),),
            ),
            _timing_section(
                "timeout", "pause_ms", "min_wait_time", default_expanded=True
            ),
        ),
    ),
    "scroll_browser": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="target",
                label="Target",
                default_expanded=True,
                summary="Page or element",
                description=(
                    "Scroll by pixels on the page, or use Web Inspector to bring "
                    "a specific element into view. Current Element is available "
                    "inside For Each Browser Element."
                ),
                rows=(
                    PresentationRow((field("target", "medium"),)),
                    PresentationRow(
                        (field("locator", "full", when=("target", "locator")),)
                    ),
                    PresentationRow(
                        (
                            field("delta_x", "compact", when=("target", "page")),
                            field("delta_y", "compact", when=("target", "page")),
                        ),
                        label="Pixel delta",
                    ),
                ),
            ),
            _timing_section(
                "timeout", "pause_ms", "min_wait_time", default_expanded=True
            ),
        ),
    ),
    "wait_for_navigation": _compact_command(
        "timeout", "pause_ms", "min_wait_time"
    ),
    "input_fill": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="target",
                label="Field",
                default_expanded=True,
                summary="Resilient locator",
                description="Use Web Inspector to author and test the locator.",
                rows=(PresentationRow((field("locator", "full"),)),),
            ),
            PresentationSection(
                key="input",
                label="Input",
                default_expanded=True,
                summary="Text value",
                rows=(PresentationRow((field("value", "full"),)),),
            ),
            PresentationSection(
                key="pacing",
                label="Typing Pace",
                summary="Browser default",
                description=(
                    "Set Character Delay for fixed pacing, or Minimum and Maximum "
                    "Key Delay for randomized pacing."
                ),
                rows=(
                    PresentationRow((field("delay_ms", "compact"),)),
                    PresentationRow(
                        (
                            field("min_delay", "compact"),
                            field("max_delay", "compact"),
                        )
                    ),
                ),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "extract_text": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="target",
                label="Target",
                default_expanded=True,
                summary="Locator or current loop element",
                rows=(
                    PresentationRow((field("target", "medium"),)),
                    PresentationRow(
                        (field("locator", "full", when=("target", "locator")),)
                    ),
                    PresentationRow(
                        (
                            field(
                                "child_selector",
                                "full",
                                when=("target", "current_element"),
                            ),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="result",
                label="Result",
                default_expanded=True,
                summary="Write extracted text",
                rows=(
                    PresentationRow((field("variable", "full"),)),
                    PresentationRow(
                        (field("log_values", "full"),),
                        label="Diagnostics",
                    ),
                ),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "evaluate_js": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="script",
                label="JavaScript",
                default_expanded=True,
                summary="Page script",
                rows=(PresentationRow((field("script", "full"),)),),
            ),
            PresentationSection(
                key="result",
                label="Result",
                default_expanded=True,
                summary="Optional output variable",
                rows=(
                    PresentationRow((field("variable", "full"),)),
                    PresentationRow(
                        (field("log_values", "full"),),
                        label="Diagnostics",
                    ),
                ),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "assert_visible": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="target",
                label="Expected Element",
                default_expanded=True,
                summary="Must be visible",
                description="Use Web Inspector to author and test the locator.",
                rows=(PresentationRow((field("locator", "full"),)),),
            ),
            _timing_section("pause_ms", "min_wait_time"),
        ),
    ),
    "assert_url_contains": _compact_command(
        "text", "pause_ms", "min_wait_time"
    ),
    "click_best_target": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="target",
                label="Target",
                default_expanded=True,
                summary="Target and optional bonus rules",
                rows=(
                    PresentationRow(
                        (
                            field("target_rule", "medium"),
                            field(
                                "bonus_rule",
                                "medium",
                                when=("scoring_mode", "closest_to_bonus_path"),
                            ),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="selection",
                label="Selection",
                default_expanded=True,
                summary="Choose the best matching target",
                rows=(
                    PresentationRow(
                        (
                            field("scoring_mode", "medium"),
                            field(
                                "fallback_mode",
                                "medium",
                                when=("scoring_mode", "closest_to_bonus_path"),
                            ),
                        )
                    ),
                    PresentationRow(
                        (
                            field(
                                "source_mode",
                                "medium",
                                when_rule=_BEST_TARGET_SOURCE_SCORING,
                            ),
                            field(
                                "source_fallback",
                                "medium",
                                when_rule=all_visible_rules(
                                    _BEST_TARGET_SOURCE_SCORING,
                                    equals_rule(
                                        "source_mode", "last_clicked_target"
                                    ),
                                ),
                            ),
                        ),
                        label="Source",
                    ),
                    PresentationRow(
                        (
                            field(
                                "source_x",
                                "compact",
                                when_rule=_BEST_TARGET_FIXED_SOURCE,
                            ),
                            field(
                                "source_y",
                                "compact",
                                when_rule=_BEST_TARGET_FIXED_SOURCE,
                            ),
                        ),
                        label="Fixed source",
                    ),
                    PresentationRow(
                        (
                            field(
                                "path_tolerance_px",
                                "compact",
                                label="Path Tolerance (px)",
                                when=("scoring_mode", "closest_to_bonus_path"),
                            ),
                        ),
                        label="Bonus path",
                    ),
                ),
            ),
            PresentationSection(
                key="action",
                label="Action",
                default_expanded=True,
                summary="Click or preview the chosen target",
                rows=(
                    PresentationRow(
                        (
                            field("click", "short"),
                            field("hold_duration_ms", "compact"),
                            field("preview_only", "medium"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="timing",
                label="Timing",
                summary="No added wait",
                rows=(
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="diagnostics",
                label="Diagnostics",
                summary="Timing log off",
                description=(
                    "Stores chosen coordinates and candidate counts in _visual_* "
                    "variables. Timing details can also be written to the run log."
                ),
                rows=(PresentationRow((field("log_timing", "medium"),)),),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="Use On Error setting",
                rows=(),
                include_error_policy=True,
            ),
        ),
    ),
    "click_dynamic_target": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="target",
                label="Target",
                default_expanded=True,
                summary="Recorded poses, images, or a visual rule",
                description=(
                    "Recorded poses match learned appearances. Colour / shape rule "
                    "detects moving blobs by the reusable visual rule you select."
                ),
                rows=(
                    PresentationRow((field("target_source", "medium"),)),
                    PresentationRow(
                        (
                            field("images", "full", when=("target_source", "images")),
                        ),
                        visible_when=equals_rule("target_source", "images"),
                    ),
                    PresentationRow(
                        (
                            field("target_rule", "medium", when=("target_source", "visual_rule")),
                        ),
                        visible_when=equals_rule("target_source", "visual_rule"),
                    ),
                    PresentationRow(
                        (
                            field("threshold", "short"),
                            field("color_match", "medium"),
                            field("color_tolerance", "short", when=("color_match", True)),
                        ),
                        label="Matching",
                        visible_when=equals_rule("target_source", "images"),
                    ),
                ),
            ),
            PresentationSection(
                key="action",
                label="Action",
                default_expanded=True,
                summary="Click the current, entry, or predicted position",
                rows=(
                    PresentationRow((field("strategy", "medium"),)),
                    PresentationRow(
                        (
                            field("target_anchor", "medium"),
                            field("offset_x_percent", "short"),
                            field("offset_y_percent", "short"),
                        ),
                        label="Target click point",
                    ),
                    PresentationRow(
                        (
                            field("trigger_coordinate_unit", "short"),
                            field("trigger_start_x", "short"),
                            field("trigger_start_y", "short"),
                        ),
                        label="Trigger area start",
                        visible_when=equals_rule("strategy", "intercept"),
                    ),
                    PresentationRow(
                        (
                            field("trigger_end_x", "short"),
                            field("trigger_end_y", "short"),
                            field("intercept_click", "medium"),
                        ),
                        label="Trigger area end",
                        visible_when=equals_rule("strategy", "intercept"),
                    ),
                    PresentationRow(
                        (
                            field("fixed_click_x", "short"),
                            field("fixed_click_y", "short"),
                        ),
                        label="Fixed click point",
                        visible_when=all_visible_rules(
                            equals_rule("strategy", "intercept"),
                            equals_rule("intercept_click", "fixed"),
                        ),
                    ),
                    PresentationRow(
                        (
                            field("click", "short"),
                            field("hold_duration_ms", "compact"),
                            field("preview_only", "medium"),
                        ),
                    ),
                ),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Wait and observation pace",
                rows=(
                    PresentationRow(
                        (
                            field("timeout_ms", "compact"),
                            field("frame_interval_ms", "compact"),
                        )
                    ),
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="advanced",
                label="Advanced",
                summary="Tracking safeguards",
                description=(
                    "These values are normally learned or safe to leave at their defaults."
                ),
                rows=(
                    PresentationRow(
                        (
                            field("confirmation_frames", "compact"),
                            field("missed_frame_tolerance", "compact"),
                            field("max_movement_px", "medium"),
                        ),
                        label="Tracking",
                    ),
                    PresentationRow(
                        (
                            field("observation_count", "compact"),
                            field("lead_time_ms", "compact"),
                            field("minimum_speed_px_s", "medium"),
                        ),
                        label="Prediction",
                        visible_when=equals_rule("strategy", "predict"),
                    ),
                    PresentationRow(
                        (
                            field("maximum_residual_px", "medium"),
                            field("max_prediction_distance_px", "medium"),
                            field("prediction_fallback", "medium"),
                        ),
                        label="Prediction safety",
                        visible_when=equals_rule("strategy", "predict"),
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="Use On Error setting",
                rows=(),
                include_error_policy=True,
            ),
        ),
    ),
    "click_image": CommandPresentation(
        layout="guided",
        sections=(
            IMAGE_TARGET_SECTION,
            PresentationSection(
                key="action",
                label="Action",
                default_expanded=True,
                summary="Click behavior and offset",
                rows=(
                    PresentationRow(
                        (
                            field("click", "short"),
                            field(
                                "hold_duration_ms",
                                "medium",
                                label="Hold Duration (ms)",
                            ),
                        )
                    ),
                    PresentationRow(
                        (
                            field("offset_unit", "compact"),
                            field("xmargin", "compact", label="Offset X"),
                            field("ymargin", "compact", label="Offset Y"),
                        ),
                        label="Click offset",
                    ),
                ),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Wait and pause controls",
                rows=(
                    PresentationRow(
                        (
                            field("pause_ms", "medium"),
                            field("min_wait_time", "medium"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="Normal error handling",
                rows=(
                    PresentationRow(
                        (
                            field("not_found_behavior", "short"),
                            field(
                                "not_found_message",
                                "full",
                                when=("not_found_behavior", "attention"),
                            ),
                        )
                    ),
                ),
                include_error_policy=True,
            ),
        ),
    ),
    "find_image": CommandPresentation(
        layout="guided",
        sections=(
            IMAGE_TARGET_SECTION,
            PresentationSection(
                key="result",
                label="Result",
                default_expanded=True,
                summary="4 output variables",
                description=(
                    "Stores the match in _image_found, _match_x, _match_y and "
                    "_match_confidence for later commands."
                ),
                rows=(),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="No added wait",
                rows=(
                    PresentationRow(
                        (
                            field("pause_ms", "medium"),
                            field("min_wait_time", "medium"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary='Continue with "not found"',
                rows=(
                    PresentationRow(
                        (
                            field("not_found_behavior", "short"),
                            field(
                                "not_found_message",
                                "full",
                                when=("not_found_behavior", "attention"),
                            ),
                        )
                    ),
                ),
                include_error_policy=True,
            ),
        ),
    ),
    "if_image": CommandPresentation(
        layout="guided",
        sections=(
            IMAGE_TARGET_SECTION,
            PresentationSection(
                key="condition",
                label="Condition",
                default_expanded=True,
                summary="Image is found",
                rows=(PresentationRow((field("condition", "medium"),)),),
            ),
            PresentationSection(
                key="action",
                label="Action",
                summary="No click",
                visible_when=VisibilityRule("condition", "found"),
                rows=(
                    PresentationRow((field("clickIfFound", "short"),)),
                    PresentationRow(
                        (
                            field(
                                "hold_duration_ms",
                                "compact",
                                label="Hold Duration (ms)",
                                when=("clickIfFound", True),
                            ),
                            field(
                                "offset_unit",
                                "compact",
                                when=("clickIfFound", True),
                            ),
                            field(
                                "xmargin",
                                "compact",
                                label="Offset X",
                                when=("clickIfFound", True),
                            ),
                            field(
                                "ymargin",
                                "compact",
                                label="Offset Y",
                                when=("clickIfFound", True),
                            ),
                        ),
                        label="Click details",
                    ),
                ),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Check once",
                rows=(
                    PresentationRow(
                        (
                            field("timeout", "medium"),
                            field("pause_ms", "medium"),
                            field("min_wait_time", "medium"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="Run Else branch",
                rows=(
                    PresentationRow(
                        (
                            field("condition_unmet_behavior", "short"),
                            field(
                                "condition_unmet_message",
                                "full",
                                when=("condition_unmet_behavior", "attention"),
                            ),
                        )
                    ),
                ),
                include_error_policy=True,
            ),
        ),
    ),
    "while": CommandPresentation(
        layout="guided",
        sections=(
            IMAGE_TARGET_SECTION,
            PresentationSection(
                key="condition",
                label="Condition",
                default_expanded=True,
                summary="Image is found",
                rows=(PresentationRow((field("condition", "medium"),)),),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Stop after 30 s",
                rows=(
                    PresentationRow(
                        (
                            field("timeout", "medium"),
                            field("pause_ms", "medium"),
                            field("min_wait_time", "medium"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="End the loop",
                rows=(
                    PresentationRow(
                        (
                            field("condition_unmet_behavior", "short"),
                            field(
                                "condition_unmet_message",
                                "full",
                                when=("condition_unmet_behavior", "attention"),
                            ),
                        )
                    ),
                ),
                include_error_policy=True,
            ),
        ),
    ),
    "win_click_image": CommandPresentation(
        layout="guided",
        sections=(
            IMAGE_TARGET_SECTION,
            PresentationSection(
                key="action",
                label="Action",
                default_expanded=True,
                summary="Single left background click",
                rows=(
                    PresentationRow(
                        (
                            field("click", "short"),
                            field("button", "short", label="Mouse Button"),
                            field("mode", "medium"),
                        )
                    ),
                    PresentationRow(
                        (
                            field("offset_unit", "compact"),
                            field("xmargin", "compact", label="Offset X"),
                            field("ymargin", "compact", label="Offset Y"),
                        ),
                        label="Click offset",
                    ),
                ),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Immediate background click",
                rows=(
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                            field(
                                "click_duration_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                        ),
                        label="Physical input",
                    ),
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        ),
                        label="Command timing",
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="Use On Error setting",
                rows=(
                    PresentationRow(
                        (
                            field("not_found_behavior", "short"),
                            field(
                                "not_found_message",
                                "full",
                                when=("not_found_behavior", "attention"),
                            ),
                        )
                    ),
                ),
                include_error_policy=True,
            ),
        ),
    ),
    "win_drag_image": CommandPresentation(
        layout="guided",
        sections=(
            IMAGE_TARGET_SECTION,
            PresentationSection(
                key="action",
                label="Action",
                default_expanded=True,
                summary="Left-button physical drag",
                rows=(
                    PresentationRow(
                        (
                            field("button", "short", label="Mouse Button"),
                            field("mode", "medium"),
                        )
                    ),
                    PresentationRow(
                        (
                            field("start_offset_unit", "compact"),
                            field("xmargin", "compact", label="Start X"),
                            field("ymargin", "compact", label="Start Y"),
                        ),
                        label="Start offset",
                    ),
                    PresentationRow(
                        (
                            field("offset_unit", "compact"),
                            field("xoffset", "compact", label="Drag X"),
                            field("yoffset", "compact", label="Drag Y"),
                        ),
                        label="Drag offset",
                    ),
                ),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="300 ms drag · 12 steps",
                rows=(
                    PresentationRow(
                        (
                            field("drag_duration_ms", "compact"),
                            field("drag_steps", "compact"),
                        ),
                        label="Drag motion",
                    ),
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                        ),
                        label="Physical input",
                    ),
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        ),
                        label="Command timing",
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="Use On Error setting",
                rows=(
                    PresentationRow(
                        (
                            field("not_found_behavior", "short"),
                            field(
                                "not_found_message",
                                "full",
                                when=("not_found_behavior", "attention"),
                            ),
                        )
                    ),
                ),
                include_error_policy=True,
            ),
        ),
    ),
    "win_if_image": CommandPresentation(
        layout="guided",
        sections=(
            IMAGE_TARGET_SECTION,
            PresentationSection(
                key="condition",
                label="Condition",
                default_expanded=True,
                summary="Image is found",
                rows=(PresentationRow((field("condition", "medium"),)),),
            ),
            PresentationSection(
                key="action",
                label="Action",
                summary="No click",
                visible_when=VisibilityRule("condition", "found"),
                rows=(
                    PresentationRow(
                        (field("click_if_found", "short", label="Click when found"),)
                    ),
                    PresentationRow(
                        (
                            field("button", "short", label="Mouse Button"),
                            field("mode", "medium"),
                        ),
                        visible_when=VisibilityRule("click_if_found", True),
                    ),
                    PresentationRow(
                        (
                            field("offset_unit", "compact"),
                            field("xmargin", "compact", label="Offset X"),
                            field("ymargin", "compact", label="Offset Y"),
                        ),
                        label="Click offset",
                        visible_when=VisibilityRule("click_if_found", True),
                    ),
                ),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Check once",
                rows=(
                    PresentationRow(
                        (field("timeout", "compact"),),
                        label="Condition timing",
                    ),
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        ),
                        label="Command timing",
                    ),
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                            field(
                                "click_duration_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                        ),
                        label="Physical click",
                        visible_when=VisibilityRule("click_if_found", True),
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="Run Else branch",
                rows=(
                    PresentationRow(
                        (
                            field("condition_unmet_behavior", "short"),
                            field(
                                "condition_unmet_message",
                                "full",
                                when=("condition_unmet_behavior", "attention"),
                            ),
                        )
                    ),
                ),
                include_error_policy=True,
            ),
        ),
    ),
    "win_while": CommandPresentation(
        layout="guided",
        sections=(
            IMAGE_TARGET_SECTION,
            PresentationSection(
                key="condition",
                label="Condition",
                default_expanded=True,
                summary="Image is found",
                rows=(PresentationRow((field("condition", "medium"),)),),
            ),
            PresentationSection(
                key="action",
                label="Action",
                summary="No click",
                rows=(
                    PresentationRow(
                        (
                            field(
                                "click_if_found",
                                "short",
                                label="Click matching image",
                            ),
                        )
                    ),
                    PresentationRow(
                        (
                            field("button", "short", label="Mouse Button"),
                            field("mode", "medium"),
                        ),
                        visible_when=VisibilityRule("click_if_found", True),
                    ),
                    PresentationRow(
                        (
                            field("offset_unit", "compact"),
                            field("xmargin", "compact", label="Offset X"),
                            field("ymargin", "compact", label="Offset Y"),
                        ),
                        label="Click offset",
                        visible_when=VisibilityRule("click_if_found", True),
                    ),
                ),
            ),
            IMAGE_SCOPE_SECTION,
            PresentationSection(
                key="timing",
                label="Timing",
                summary="Stop after 30 s · every 1 s",
                rows=(
                    PresentationRow(
                        (
                            field("timeout", "compact"),
                            field("interval_ms", "compact"),
                        ),
                        label="Loop timing",
                    ),
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        ),
                        label="Command timing",
                    ),
                    PresentationRow(
                        (
                            field(
                                "focus_delay_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                            field(
                                "click_duration_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                            field(
                                "restore_delay_ms",
                                "compact",
                                when_any=("mode", ("cursor", "input")),
                            ),
                        ),
                        label="Physical click",
                        visible_when=VisibilityRule("click_if_found", True),
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="End the loop",
                rows=(
                    PresentationRow(
                        (
                            field("condition_unmet_behavior", "short"),
                            field(
                                "condition_unmet_message",
                                "full",
                                when=("condition_unmet_behavior", "attention"),
                            ),
                        )
                    ),
                ),
                include_error_policy=True,
            ),
        ),
    ),
    "read_screen_text": CommandPresentation(
        layout="guided",
        sections=(
            PresentationSection(
                key="capture_area",
                label="Capture Area",
                default_expanded=True,
                summary="Fixed area",
                rows=(
                    PresentationRow(
                        (
                            field("region_mode", "medium"),
                            field("coordinate_unit", "compact"),
                        )
                    ),
                    PresentationRow(
                        (
                            field(
                                "start_x",
                                "compact",
                                when=("region_mode", "fixed"),
                            ),
                            field(
                                "start_y",
                                "compact",
                                when=("region_mode", "fixed"),
                            ),
                        ),
                        label="Fixed start",
                        visible_when=VisibilityRule("region_mode", "fixed"),
                    ),
                    PresentationRow(
                        (
                            field(
                                "end_x",
                                "compact",
                                when=("region_mode", "fixed"),
                            ),
                            field(
                                "end_y",
                                "compact",
                                when=("region_mode", "fixed"),
                            ),
                        ),
                        label="Fixed end",
                        visible_when=VisibilityRule("region_mode", "fixed"),
                    ),
                    PresentationRow(
                        (field("images", "full"),),
                        visible_when=VisibilityRule("region_mode", "image_anchor"),
                    ),
                    PresentationRow(
                        (
                            field("threshold", "short", label="Confidence (0–1)"),
                            field("color_match", "medium"),
                            field(
                                "color_tolerance",
                                "short",
                                label="Tolerance (0–1)",
                                when=("color_match", True),
                            ),
                        ),
                        label="Matching",
                        visible_when=VisibilityRule("region_mode", "image_anchor"),
                    ),
                    PresentationRow(
                        (
                            field("anchor", "compact"),
                            field("offset_x", "compact"),
                            field("offset_y", "compact"),
                        ),
                        label="Placement",
                        visible_when=VisibilityRule("region_mode", "image_anchor"),
                    ),
                    PresentationRow(
                        (
                            field("region_width", "compact"),
                            field("region_height", "compact"),
                        ),
                        label="Area size",
                        visible_when=VisibilityRule("region_mode", "image_anchor"),
                    ),
                ),
            ),
            PresentationSection(
                key="result",
                label="Result",
                default_expanded=True,
                summary="Text output",
                rows=(
                    PresentationRow(
                        (
                            field("result_type", "medium"),
                            field("variable", "medium", label="Output Variable"),
                        )
                    ),
                    PresentationRow(
                        (
                            field("recognition_mode", "medium"),
                            field("preprocessing", "medium"),
                            field("value_match_mode", "medium"),
                        ),
                        label="Recognition",
                    ),
                    PresentationRow(
                        (
                            field(
                                "allow_empty",
                                "medium",
                                label="Allow empty text",
                                when=("result_type", "text"),
                            ),
                            field(
                                "decimal_separator",
                                "medium",
                                when=("result_type", "number"),
                            ),
                            field(
                                "duration_format",
                                "medium",
                                when=("result_type", "duration_ms"),
                            ),
                        ),
                        label="Parsing",
                    ),
                    PresentationRow(
                        (field("log_values", "full"),),
                        label="Diagnostics",
                    ),
                ),
            ),
            PresentationSection(
                key="timing",
                label="Timing",
                summary="No added wait",
                rows=(
                    PresentationRow(
                        (
                            field("pause_ms", "compact"),
                            field("min_wait_time", "compact"),
                        )
                    ),
                ),
            ),
            PresentationSection(
                key="failure",
                label="Failure",
                summary="On Error: stop",
                rows=(),
                include_error_policy=True,
            ),
        ),
    ),
    "wait": CommandPresentation(
        layout="compact",
        rows=(
            PresentationRow(
                (
                    field("ms", "medium"),
                    field("pause_ms", "medium"),
                )
            ),
        ),
    ),
}


def command_presentation(command_id: str) -> CommandPresentation | None:
    return COMMAND_PRESENTATIONS.get(command_id)


def literal_parameter_value(value: Any) -> Any:
    """Return a literal reference's value while leaving other values intact."""
    if isinstance(value, dict) and value.get("kind") == "literal":
        return value.get("value")
    return value


def visibility_rule_matches(
    rule: VisibilityRule,
    params: dict[str, Any],
) -> bool:
    if rule.all_of:
        return all(visibility_rule_matches(item, params) for item in rule.all_of)
    if rule.any_of:
        return any(visibility_rule_matches(item, params) for item in rule.any_of)
    if rule.parameter is None:
        raise ValueError("A leaf visibility rule requires a parameter")
    value = literal_parameter_value(params.get(rule.parameter))
    if rule.matches_any:
        return value in rule.equals
    return value == rule.equals


def visibility_rule_parameters(rule: VisibilityRule) -> tuple[str, ...]:
    nested = rule.all_of + rule.any_of
    if nested:
        return tuple(
            parameter
            for item in nested
            for parameter in visibility_rule_parameters(item)
        )
    return (rule.parameter,) if rule.parameter is not None else ()


def field_is_visible(
    presentation: FieldPresentation,
    params: dict[str, Any],
) -> bool:
    rule = presentation.visible_when
    if rule is None:
        return True
    return visibility_rule_matches(rule, params)


def row_is_visible(row: PresentationRow, params: dict[str, Any]) -> bool:
    rule = row.visible_when
    if rule is None:
        return True
    return visibility_rule_matches(rule, params)


def section_is_visible(
    section: PresentationSection,
    params: dict[str, Any],
) -> bool:
    rule = section.visible_when
    if rule is None:
        return True
    return visibility_rule_matches(rule, params)


def presentation_parameter_names(
    presentation: CommandPresentation,
) -> tuple[str, ...]:
    rows = presentation.rows + tuple(
        row for section in presentation.sections for row in section.rows
    )
    return tuple(field.name for row in rows for field in row.fields)


def presentation_visibility_parameters(
    presentation: CommandPresentation,
) -> frozenset[str]:
    """Return parameters whose changes can alter the rendered field tree."""
    names: set[str] = set()
    for section in presentation.sections:
        if section.visible_when is not None:
            names.update(visibility_rule_parameters(section.visible_when))
    rows = presentation.rows + tuple(
        row for section in presentation.sections for row in section.rows
    )
    for row in rows:
        if row.visible_when is not None:
            names.update(visibility_rule_parameters(row.visible_when))
        for item in row.fields:
            if item.visible_when is not None:
                names.update(visibility_rule_parameters(item.visible_when))
    return frozenset(names)


def validate_command_presentations(
    command_types: dict[str, dict[str, Any]],
) -> None:
    """Fail fast when presentation metadata drifts from runtime definitions."""
    for command_id, presentation in COMMAND_PRESENTATIONS.items():
        if command_id not in command_types:
            raise ValueError(f"Presentation references unknown command: {command_id}")
        runtime_names = {
            str(definition["name"])
            for definition in command_types[command_id].get("params", [])
        }
        presented_names = presentation_parameter_names(presentation)
        duplicate_names = sorted(
            name for name in set(presented_names) if presented_names.count(name) > 1
        )
        if duplicate_names:
            raise ValueError(
                f"Presentation for {command_id} repeats parameters: "
                + ", ".join(duplicate_names)
            )
        unknown_names = sorted(set(presented_names) - runtime_names)
        missing_names = sorted(runtime_names - set(presented_names))
        if unknown_names or missing_names:
            details: list[str] = []
            if unknown_names:
                details.append("unknown: " + ", ".join(unknown_names))
            if missing_names:
                details.append("missing: " + ", ".join(missing_names))
            raise ValueError(
                f"Presentation for {command_id} does not match runtime parameters "
                f"({'; '.join(details)})"
            )
        valid_conditions = runtime_names
        for section in presentation.sections:
            unknown_conditions = (
                set(visibility_rule_parameters(section.visible_when))
                - valid_conditions
                if section.visible_when is not None
                else set()
            )
            if unknown_conditions:
                raise ValueError(
                    f"Presentation for {command_id} has an unknown section "
                    "visibility parameter: " + ", ".join(sorted(unknown_conditions))
                )
        for row in presentation.rows + tuple(
            row for section in presentation.sections for row in section.rows
        ):
            unknown_conditions = (
                set(visibility_rule_parameters(row.visible_when)) - valid_conditions
                if row.visible_when is not None
                else set()
            )
            if unknown_conditions:
                raise ValueError(
                    f"Presentation for {command_id} has an unknown row visibility "
                    "parameter: " + ", ".join(sorted(unknown_conditions))
                )
            for item in row.fields:
                rule = item.visible_when
                unknown_conditions = (
                    set(visibility_rule_parameters(rule)) - valid_conditions
                    if rule is not None
                    else set()
                )
                if unknown_conditions:
                    raise ValueError(
                        f"Presentation for {command_id} has an unknown visibility "
                        "parameter: " + ", ".join(sorted(unknown_conditions))
                    )
