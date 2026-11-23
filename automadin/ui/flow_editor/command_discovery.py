"""Declarative discovery metadata for the Add Command dialog.

Runtime command definitions describe execution.  This module describes how a
person finds those commands: their purpose, supported targets, and common
search terms.  Keeping the concerns separate avoids turning runtime metadata
into UI layout configuration while validation prevents the catalogs drifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiscoveryDimension:
    key: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class CommandDiscovery:
    purpose: str
    targets: tuple[str, ...]
    keywords: tuple[str, ...] = ()


TARGETS = (
    DiscoveryDimension("general", "General", "Does not depend on an automation target"),
    DiscoveryDimension("windows", "Windows", "Native Windows applications"),
    DiscoveryDimension("browser", "Browser", "Web pages and browser tabs"),
    DiscoveryDimension("android", "Android", "Connected Android devices"),
)

PURPOSES = (
    DiscoveryDimension("interaction", "Interact", "Click, type, scroll, and drag"),
    DiscoveryDimension("read_detect", "Read & detect", "Capture, find, and read"),
    DiscoveryDimension("navigation", "Browser navigation", "Open and manage browser pages"),
    DiscoveryDimension("conditions", "Conditions", "Branch and assert"),
    DiscoveryDimension("loops", "Loops", "Repeat commands"),
    DiscoveryDimension("data", "Variables & data", "Read, calculate, and store values"),
    DiscoveryDimension("apps_devices", "Apps & devices", "Start and manage applications"),
    DiscoveryDimension("timing", "Timing", "Control when commands run"),
    DiscoveryDimension("flow", "Flow control", "Coordinate and end flows"),
    DiscoveryDimension("organization", "Organization", "Structure the editor"),
)


_PURPOSE_COMMANDS: dict[str, tuple[str, ...]] = {
    "interaction": (
        "click_position", "click_image", "type_text", "clear_text_field",
        "swipe", "press_keycode", "click_best_target", "click_dynamic_target",
        "click_element", "scroll_browser", "input_fill",
        "win_click", "win_drag", "win_key", "win_type", "win_scroll",
        "win_focus", "win_click_image", "win_drag_image",
    ),
    "read_detect": (
        "read_screen_text", "screenshot", "find_image", "wait_for_selector",
    ),
    "navigation": (
        "navigate_to", "go_back", "go_forward", "reload_page", "open_new_tab",
        "close_tab", "close_all_tabs", "switch_tab", "select_browser_page",
        "ensure_browser_page", "wait_for_navigation",
    ),
    "conditions": (
        "if_image", "if_value", "switch_value", "switch_case", "switch_default",
        "if_elapsed", "assert_visible", "assert_url_contains", "win_if_image",
    ),
    "loops": (
        "for", "while", "while_value", "for_each",
        "for_each_browser_element", "win_while",
    ),
    "data": ("extract_text", "evaluate_js", "update_variable"),
    "apps_devices": (
        "android_install_apk", "android_launch_app", "android_force_stop_app",
        "android_restart_app", "android_start_activity", "android_open_deep_link",
        "win_ensure_app", "hibernate_computer",
    ),
    "timing": ("wait", "random_wait"),
    "flow": (
        "send_notification", "user_checkpoint", "run_flow", "terminate_flow",
        "restart_flow", "check_hooks",
    ),
    "organization": ("section", "group"),
}

_GENERAL_COMMANDS = {
    "section", "group", "send_notification", "user_checkpoint", "for",
    "if_value", "while_value", "switch_value", "switch_case", "switch_default",
    "if_elapsed", "for_each", "run_flow", "update_variable", "terminate_flow",
    "restart_flow", "check_hooks", "wait", "random_wait",
    "hibernate_computer",
}

_ALL_TARGET_COMMANDS = {
    "click_position", "click_image", "type_text", "click_best_target",
    "click_dynamic_target", "read_screen_text", "screenshot", "find_image",
    "if_image", "while",
}

_BROWSER_ANDROID_COMMANDS = {"clear_text_field", "swipe", "press_keycode"}

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "click_position": ("tap", "coordinates", "screen"),
    "click_image": ("tap", "picture", "template", "visual"),
    "type_text": ("keyboard", "write", "input"),
    "clear_text_field": ("erase", "empty", "input"),
    "swipe": ("drag", "gesture"),
    "press_keycode": ("keyboard", "shortcut", "hotkey", "key"),
    "click_best_target": ("game", "blob", "score", "visual"),
    "click_dynamic_target": (
        "game", "animated", "moving", "morphing", "track", "intercept", "predict", "visual"
    ),
    "read_screen_text": ("ocr", "recognize", "number", "capture"),
    "screenshot": ("capture", "screen", "image"),
    "find_image": ("picture", "template", "visual", "match"),
    "navigate_to": ("website", "url", "open", "page"),
    "click_element": ("website", "web", "locator", "selector"),
    "wait_for_selector": ("element", "locator", "selector", "appear"),
    "input_fill": ("website", "form", "field", "type"),
    "extract_text": ("read", "element", "variable", "website"),
    "evaluate_js": ("javascript", "script", "website"),
    "for": ("repeat", "count", "loop"),
    "while": ("repeat", "loop", "image"),
    "if_image": ("condition", "branch", "picture", "visual"),
    "if_value": ("condition", "branch", "compare"),
    "switch_value": ("condition", "branch", "case", "compare"),
    "for_each": ("repeat", "list", "loop", "each"),
    "run_flow": ("subflow", "subroutine", "call"),
    "update_variable": ("set", "change", "value", "calculate"),
    "android_install_apk": ("package", "application", "device"),
    "android_launch_app": ("open", "package", "application", "device"),
    "android_force_stop_app": ("close", "kill", "package", "application"),
    "win_ensure_app": ("launch", "open", "window", "application", "desktop"),
    "hibernate_computer": ("power", "sleep", "suspend", "shutdown", "computer"),
    "win_click": ("desktop", "application", "coordinates"),
    "win_click_image": ("desktop", "application", "picture", "visual"),
    "wait": ("delay", "sleep", "pause"),
    "random_wait": ("delay", "sleep", "pause", "range"),
    "send_notification": ("phone", "pushover", "telegram", "alert", "message"),
}


def _command_targets(command_id: str) -> tuple[str, ...]:
    if command_id in _GENERAL_COMMANDS:
        return ("general",)
    if command_id in _ALL_TARGET_COMMANDS:
        return ("windows", "browser", "android")
    if command_id in _BROWSER_ANDROID_COMMANDS:
        return ("browser", "android")
    if command_id.startswith("win_"):
        return ("windows",)
    if command_id.startswith("android_"):
        return ("android",)
    if command_id == "for_each_browser_element" or command_id in {
        command
        for command in _PURPOSE_COMMANDS["navigation"]
    } or command_id in {
        "click_element", "scroll_browser", "wait_for_selector", "input_fill",
        "extract_text", "evaluate_js", "assert_visible", "assert_url_contains",
    }:
        return ("browser",)
    raise ValueError(f"No target applicability defined for command: {command_id}")


COMMAND_DISCOVERY: dict[str, CommandDiscovery] = {
    command_id: CommandDiscovery(
        purpose=purpose,
        targets=_command_targets(command_id),
        keywords=_KEYWORDS.get(command_id, ()),
    )
    for purpose, command_ids in _PURPOSE_COMMANDS.items()
    for command_id in command_ids
}


def validate_command_discovery(
    command_types: dict[str, dict[str, Any]],
    *,
    require_complete: bool = True,
) -> None:
    """Fail fast when discovery metadata and runtime definitions diverge.

    Subset dialogs, such as hook command selection, validate only the commands
    they expose.  The global catalog uses the default complete coverage check.
    """
    runtime_ids = set(command_types)
    discovery_ids = set(COMMAND_DISCOVERY)
    missing = sorted(runtime_ids - discovery_ids)
    unknown = sorted(discovery_ids - runtime_ids) if require_complete else []
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unknown:
            details.append("unknown: " + ", ".join(unknown))
        raise ValueError("Command discovery metadata mismatch (" + "; ".join(details) + ")")

    purpose_keys = {purpose.key for purpose in PURPOSES}
    target_keys = {target.key for target in TARGETS}
    for command_id, discovery in COMMAND_DISCOVERY.items():
        if discovery.purpose not in purpose_keys:
            raise ValueError(f"Command '{command_id}' has an unknown discovery purpose")
        if not discovery.targets or not set(discovery.targets) <= target_keys:
            raise ValueError(f"Command '{command_id}' has invalid discovery targets")


def target_summary(targets: tuple[str, ...]) -> str:
    """Return a compact, human-readable applicability label."""
    if targets == ("general",):
        return "GENERAL"
    if set(targets) == {"windows", "browser", "android"}:
        return "ALL TARGETS"
    labels = {target.key: target.label.upper() for target in TARGETS}
    return " · ".join(labels[target] for target in targets)


def target_explanation(targets: tuple[str, ...]) -> str:
    """Explain command applicability without relying on badge color."""
    if targets == ("general",):
        return "Works in every flow and does not depend on the selected target."
    if set(targets) == {"windows", "browser", "android"}:
        return "Works with Windows applications, browser pages, and Android devices."
    labels = {target.key: target.label for target in TARGETS}
    named = [labels[target] for target in targets]
    if len(named) == 1:
        return f"Works with {named[0]} automation only."
    return "Works with " + " and ".join(named) + " automation."
