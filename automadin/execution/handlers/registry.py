from __future__ import annotations

import copy
import json
from importlib import resources
from typing import Any

from automadin.models.locator import is_locator_value
from automadin.models.builtin_scope import BUILTIN_VALUE_TYPES
from automadin.models.typed_value import ValueType
from automadin.services.screen_text_options import hydrate_screen_text_command_metadata

_PARAM_TYPES = {
    "bool",
    "double",
    "enum",
    "flow_reference",
    "image_list",
    "hook_list",
    "int",
    "key_list",
    "locator",
    "multiline",
    "string",
    "string_list",
    "value_reference",
    "variable_target",
    "visual_rule",
}
_VALUE_ROLES = {"output_target"}
_HOOK_POINT_EVIDENCE = {
    "visual_image": "screenshot",
    "browser_element": "browser_dom",
}
_HOOK_PHASES = {"before", "after"}
_VISUAL_HOOK_CAPTURE_MODES = {"host", "boundary"}
# These two fields are injected by the executor for every command.  Keep their
# value contract centralized so every command gets the same typed Duration
# editor/preflight behavior without duplicating metadata in dozens of entries.
_GLOBAL_DURATION_REFERENCE_PARAMS = frozenset({"pause_ms", "min_wait_time"})
_IMAGE_SEARCH_REGION_COMMANDS = frozenset(
    {
        "click_image",
        "click_dynamic_target",
        "find_image",
        "if_image",
        "while",
        "win_click_image",
        "win_drag_image",
        "win_if_image",
        "win_while",
    }
)
_WINDOWS_IMAGE_COMMANDS = frozenset(
    {"win_click_image", "win_drag_image", "win_if_image", "win_while"}
)
_WINDOWS_IMAGE_OPTION_LABELS = {
    "button": {
        "left": "Left",
        "right": "Right",
        "middle": "Middle",
    },
    "click": {"single": "Single", "double": "Double"},
    "condition": {
        "found": "Image is found",
        "not_found": "Image is not found",
    },
    "mode": {
        "message": "Background message",
        "input": "Physical input",
        "cursor": "Cursor fallback",
    },
    "offset_unit": {"pixels": "Pixels", "percent": "Percent"},
    "start_offset_unit": {"pixels": "Pixels", "percent": "Percent"},
}
_IMAGE_NOT_FOUND_POLICY_COMMANDS = frozenset(
    {"click_image", "find_image", "win_click_image", "win_drag_image"}
)
_IMAGE_NOT_FOUND_POLICY_PARAMS = (
    {
        "name": "not_found_behavior",
        "type": "enum",
        "label": "When Not Found",
        "options": ["normal", "fail", "attention"],
        "default": "normal",
        "group": "When Not Found",
    },
    {
        "name": "not_found_message",
        "type": "string",
        "label": "Attention Message",
        "optional": True,
        "group": "When Not Found",
    },
)
_IMAGE_NOT_FOUND_OPTION_LABELS = {
    "find_image": {
        "normal": 'Continue with "not found"',
        "fail": "Fail the flow",
        "attention": "Request attention",
    },
    "win_click_image": {
        "normal": "Use On Error setting",
        "fail": "Fail the flow",
        "attention": "Request attention",
    },
    "win_drag_image": {
        "normal": "Use On Error setting",
        "fail": "Fail the flow",
        "attention": "Request attention",
    },
}
_IMAGE_CONDITION_POLICY_COMMANDS = frozenset(
    {"if_image", "while", "win_if_image", "win_while"}
)
_IMAGE_CONDITION_POLICY_PARAMS = (
    {
        "name": "condition_unmet_behavior",
        "type": "enum",
        "label": "When Condition Is Unmet",
        "options": ["normal", "fail", "attention"],
        "default": "normal",
        "group": "When Condition Is Unmet",
    },
    {
        "name": "condition_unmet_message",
        "type": "string",
        "label": "Attention Message",
        "optional": True,
        "group": "When Condition Is Unmet",
    },
)
_IMAGE_CONDITION_OPTION_LABELS = {
    "if_image": {
        "normal": "Run Else branch",
        "fail": "Fail the flow",
        "attention": "Request attention",
    },
    "while": {
        "normal": "End the loop",
        "fail": "Fail the flow",
        "attention": "Request attention",
    },
    "win_if_image": {
        "normal": "Run Else branch",
        "fail": "Fail the flow",
        "attention": "Request attention",
    },
    "win_while": {
        "normal": "End the loop",
        "fail": "Fail the flow",
        "attention": "Request attention",
    },
}
_IMAGE_SEARCH_REGION_PARAMS = (
    {
        "name": "search_region_mode",
        "type": "enum",
        "label": "Search Region",
        "options": ["full", "fixed", "image_anchor"],
        "default": "full",
        "group": "Search Region",
    },
    {
        "name": "search_coordinate_unit",
        "type": "enum",
        "label": "Search Coordinates",
        "options": ["pixels", "percent"],
        "default": "pixels",
        "group": "Search Region",
    },
    *(
        {
            "name": name,
            "type": "double",
            "label": label,
            "supportsValueReference": True,
            "allowed_value_types": ["number"],
            "optional": True,
            "group": "Search Region",
        }
        for name, label in (
            ("search_start_x", "Search Start X"),
            ("search_start_y", "Search Start Y"),
            ("search_end_x", "Search End X"),
            ("search_end_y", "Search End Y"),
        )
    ),
    {
        "name": "search_anchor_images",
        "type": "image_list",
        "label": "Search Anchor Images",
        "optional": True,
        "group": "Image Anchor",
    },
    *(
        {
            "name": name,
            "type": param_type,
            "label": label,
            "supportsValueReference": True,
            "allowed_value_types": [value_type],
            "default": default,
            "group": "Image Anchor",
        }
        for name, param_type, label, value_type, default in (
            ("search_anchor_threshold", "double", "Anchor Threshold", "number", 0.9),
            ("search_anchor_color_match", "bool", "Anchor Color Match", "boolean", False),
            (
                "search_anchor_color_tolerance",
                "double",
                "Anchor Color Tolerance",
                "number",
                0.3,
            ),
        )
    ),
    {
        "name": "search_anchor",
        "type": "enum",
        "label": "Anchor Point",
        "options": ["top_left", "top_right", "bottom_left", "bottom_right", "center"],
        "default": "top_left",
        "group": "Image Anchor",
    },
    *(
        {
            "name": name,
            "type": "double",
            "label": label,
            "supportsValueReference": True,
            "allowed_value_types": ["number"],
            "optional": True,
            "group": "Image Anchor",
        }
        for name, label in (
            ("search_offset_x", "Offset X"),
            ("search_offset_y", "Offset Y"),
            ("search_region_width", "Region Width"),
            ("search_region_height", "Region Height"),
        )
    ),
)


def _parameter_value_matches(param_type: str, value: object) -> bool:
    if param_type == "locator":
        return is_locator_value(value, allow_empty=True)
    if param_type == "value_reference":
        return isinstance(value, dict)
    if param_type == "variable_target":
        return isinstance(value, str)
    if param_type in {"string", "multiline", "flow_reference", "visual_rule"}:
        return isinstance(value, str)
    if param_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if param_type == "double":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if param_type == "bool":
        return isinstance(value, bool)
    if param_type == "enum":
        return isinstance(value, str)
    if param_type in {"hook_list", "image_list", "key_list", "string_list"}:
        return isinstance(value, list) and all(
            isinstance(item, str) for item in value
        )
    return False


def validate_command_types(entries: object) -> list[dict[str, Any]]:
    """Validate command metadata before the UI or executor consumes it."""
    if not isinstance(entries, list):
        raise ValueError("Command metadata must be a JSON array")
    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Command entry {index} must be an object")
        command_id = entry.get("id")
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError(f"Command entry {index} has no valid id")
        if command_id in seen_ids:
            raise ValueError(f"Duplicate command id: {command_id}")
        seen_ids.add(command_id)
        child_builtins = entry.get("childBuiltins", [])
        if (
            not isinstance(child_builtins, list)
            or any(
                not isinstance(name, str) or not name.strip()
                for name in child_builtins
            )
            or len(set(child_builtins)) != len(child_builtins)
        ):
            raise ValueError(
                f"Command '{command_id}' childBuiltins must be unique text names"
            )
        unknown_builtins = sorted(set(child_builtins) - set(BUILTIN_VALUE_TYPES))
        if unknown_builtins:
            raise ValueError(
                f"Command '{command_id}' has unknown child Built-ins: "
                f"{', '.join(unknown_builtins)}"
            )
        hook_points = entry.get("hookPoints", [])
        if not isinstance(hook_points, list):
            raise ValueError(f"Command '{command_id}' hookPoints must be an array")
        seen_hook_types: set[str] = set()
        for point in hook_points:
            if not isinstance(point, dict):
                raise ValueError(f"Command '{command_id}' has an invalid hook point")
            hook_type = point.get("type")
            evidence = point.get("evidence")
            phases = point.get("phases")
            if hook_type not in _HOOK_POINT_EVIDENCE:
                raise ValueError(f"Command '{command_id}' has an unknown hook type")
            if hook_type in seen_hook_types:
                raise ValueError(
                    f"Command '{command_id}' duplicates hook type '{hook_type}'"
                )
            seen_hook_types.add(hook_type)
            if evidence != _HOOK_POINT_EVIDENCE[hook_type]:
                raise ValueError(
                    f"Command '{command_id}' hook type '{hook_type}' requires "
                    f"{_HOOK_POINT_EVIDENCE[hook_type]} evidence"
                )
            capture = point.get("capture", "host")
            if (
                hook_type == "visual_image"
                and capture not in _VISUAL_HOOK_CAPTURE_MODES
            ):
                raise ValueError(
                    f"Command '{command_id}' visual hook capture must be "
                    "'host' or 'boundary'"
                )
            if hook_type != "visual_image" and "capture" in point:
                raise ValueError(
                    f"Command '{command_id}' hook type '{hook_type}' cannot "
                    "declare screenshot capture"
                )
            if (
                not isinstance(phases, list)
                or not phases
                or any(phase not in _HOOK_PHASES for phase in phases)
                or len(set(phases)) != len(phases)
            ):
                raise ValueError(
                    f"Command '{command_id}' hook phases must be unique before/after values"
                )
        params = entry.get("params", [])
        if not isinstance(params, list):
            raise ValueError(f"Command '{command_id}' params must be an array")
        param_names: set[str] = set()
        for param in params:
            if not isinstance(param, dict) or not isinstance(param.get("name"), str):
                raise ValueError(f"Command '{command_id}' contains an invalid parameter")
            name = param["name"]
            if name in param_names:
                raise ValueError(
                    f"Command '{command_id}' has duplicate parameter or alias '{name}'"
                )
            param_names.add(name)
            aliases = param.get("aliases", [])
            if not isinstance(aliases, list) or any(
                not isinstance(alias, str) or not alias.strip()
                for alias in aliases
            ):
                raise ValueError(
                    f"Command '{command_id}' parameter '{name}' aliases must be text"
                )
            for alias in aliases:
                if alias in param_names:
                    raise ValueError(
                        f"Command '{command_id}' has duplicate parameter or alias '{alias}'"
                    )
                param_names.add(alias)
            param_type = param.get("type", "string")
            if param_type not in _PARAM_TYPES:
                raise ValueError(
                    f"Command '{command_id}' parameter '{name}' has unknown type '{param_type}'"
                )
            value_role = param.get("valueRole")
            if value_role is not None and value_role not in _VALUE_ROLES:
                raise ValueError(
                    f"Command '{command_id}' parameter '{name}' has unknown "
                    f"valueRole '{value_role}'"
                )
            allowed_value_types = param.get("allowed_value_types")
            if allowed_value_types is not None and (
                not isinstance(allowed_value_types, list)
                or not allowed_value_types
                or any(
                    not isinstance(item, str)
                    or item not in {value_type.value for value_type in ValueType}
                    for item in allowed_value_types
                )
                or len(set(allowed_value_types)) != len(allowed_value_types)
            ):
                raise ValueError(
                    f"Command '{command_id}' parameter '{name}' "
                    "allowed_value_types must contain unique flow value types"
                )
            supports_value_reference = param.get("supportsValueReference")
            if supports_value_reference is not None and not isinstance(
                supports_value_reference, bool
            ):
                raise ValueError(
                    f"Command '{command_id}' parameter '{name}' "
                    "supportsValueReference must be a boolean"
                )
            allows_literal_braces = param.get("allowsLiteralBraces")
            if allows_literal_braces is not None and not isinstance(
                allows_literal_braces, bool
            ):
                raise ValueError(
                    f"Command '{command_id}' parameter '{name}' "
                    "allowsLiteralBraces must be a boolean"
                )
            if allows_literal_braces and param_type not in {"string", "multiline"}:
                raise ValueError(
                    f"Command '{command_id}' parameter '{name}' "
                    "allowsLiteralBraces requires a string or multiline parameter"
                )
            global_duration_reference = (
                name in _GLOBAL_DURATION_REFERENCE_PARAMS and param_type == "int"
            )
            reference_enabled = supports_value_reference is True or (
                supports_value_reference is None and global_duration_reference
            )
            if reference_enabled:
                if param_type not in {"int", "double", "bool"}:
                    raise ValueError(
                        f"Command '{command_id}' parameter '{name}' "
                        "supportsValueReference requires an int, double, or bool "
                        "parameter"
                    )
                if allowed_value_types is None:
                    if not global_duration_reference:
                        raise ValueError(
                            f"Command '{command_id}' parameter '{name}' "
                            "supportsValueReference requires allowed_value_types"
                        )
            if param_type == "enum":
                options = param.get("options", [])
                if (
                    not isinstance(options, list)
                    or any(not isinstance(option, str) for option in options)
                ):
                    raise ValueError(
                        f"Command '{command_id}' enum parameter '{name}' "
                        "options must be a text array"
                    )
            if "default" in param:
                default = param["default"]
                if not _parameter_value_matches(param_type, default):
                    raise ValueError(
                        f"Command '{command_id}' parameter '{name}' has an invalid default"
                    )
                if param_type == "enum" and default not in param.get("options", []):
                    raise ValueError(
                        f"Command '{command_id}' enum parameter '{name}' "
                        "default is not an option"
                    )
                if (
                    isinstance(default, (int, float))
                    and not isinstance(default, bool)
                    and (
                        ("min" in param and default < param["min"])
                        or ("max" in param and default > param["max"])
                    )
                ):
                    raise ValueError(
                        f"Command '{command_id}' parameter '{name}' default is out of range"
                    )
        normalized_entry = dict(entry)
        normalized_params: list[dict[str, Any]] = []
        for param in params:
            normalized_param = dict(param)
            if (
                normalized_param.get("name") in _GLOBAL_DURATION_REFERENCE_PARAMS
                and normalized_param.get("type") == "int"
            ):
                normalized_param.setdefault("supportsValueReference", True)
                normalized_param.setdefault("allowed_value_types", ["duration_ms"])
            if command_id in _WINDOWS_IMAGE_COMMANDS:
                option_labels = _WINDOWS_IMAGE_OPTION_LABELS.get(
                    str(normalized_param.get("name", ""))
                )
                if option_labels is not None:
                    normalized_param.setdefault(
                        "option_labels", copy.deepcopy(option_labels)
                    )
            normalized_params.append(normalized_param)
        if command_id in _IMAGE_SEARCH_REGION_COMMANDS:
            present_names = {str(param.get("name", "")) for param in normalized_params}
            normalized_params.extend(
                copy.deepcopy(param)
                for param in _IMAGE_SEARCH_REGION_PARAMS
                if param["name"] not in present_names
            )
        if command_id in _IMAGE_NOT_FOUND_POLICY_COMMANDS:
            present_names = {str(param.get("name", "")) for param in normalized_params}
            for param in _IMAGE_NOT_FOUND_POLICY_PARAMS:
                if param["name"] in present_names:
                    continue
                normalized_param = copy.deepcopy(param)
                if (
                    param["name"] == "not_found_behavior"
                    and command_id in _IMAGE_NOT_FOUND_OPTION_LABELS
                ):
                    normalized_param["option_labels"] = copy.deepcopy(
                        _IMAGE_NOT_FOUND_OPTION_LABELS[command_id]
                    )
                normalized_params.append(normalized_param)
        if command_id in _IMAGE_CONDITION_POLICY_COMMANDS:
            present_names = {str(param.get("name", "")) for param in normalized_params}
            for param in _IMAGE_CONDITION_POLICY_PARAMS:
                if param["name"] in present_names:
                    continue
                normalized_param = copy.deepcopy(param)
                if (
                    param["name"] == "condition_unmet_behavior"
                    and command_id in _IMAGE_CONDITION_OPTION_LABELS
                ):
                    normalized_param["option_labels"] = copy.deepcopy(
                        _IMAGE_CONDITION_OPTION_LABELS[command_id]
                    )
                normalized_params.append(normalized_param)
        normalized_entry["params"] = normalized_params
        validated.append(normalized_entry)
    return validated


def load_command_types() -> dict[str, dict[str, Any]]:
    """Load ``command_types.json`` from the automadin.resources package.

    Returns a dict keyed by command ``id``.
    """
    ref = resources.files("automadin.resources").joinpath("command_types.json")
    raw = ref.read_text(encoding="utf-8")
    raw_entries = json.loads(raw)
    hydrate_screen_text_command_metadata(raw_entries)
    entries = validate_command_types(raw_entries)
    return {entry["id"]: entry for entry in entries}


def get_default_params(command_type: dict[str, Any]) -> dict[str, Any]:
    """Generate a dict of default parameter values for a command type.

    Explicit metadata defaults are authoritative. Optional parameters without an
    explicit default are omitted so execution-handler fallbacks remain effective.
    Required parameters without an explicit default use these heuristics:

    Heuristics per param type:
      int   - timeout / timeoutMs -> 5000, ms -> 500, count -> 3,
              min_ms -> 500, max_ms -> 2000, index -> 0,
              delay_ms -> 0, else -> 0
      double - threshold -> 0.90, x -> 0.5, y -> 0.5, else -> 0.0
      string / multiline - ""
      bool   - False
      enum   - "found" if available, otherwise first option
      image_list / key_list / string_list - []
    """
    defaults: dict[str, Any] = {}

    for param in command_type.get("params", []):
        name: str = param["name"]
        ptype: str = param.get("type", "string")

        if "default" in param:
            defaults[name] = copy.deepcopy(param["default"])
        elif param.get("optional", False):
            continue
        elif ptype == "int":
            if name in ("timeout", "timeoutMs"):
                defaults[name] = 5000
            elif name == "ms":
                defaults[name] = 500
            elif name == "count":
                defaults[name] = 3
            elif name == "min_ms":
                defaults[name] = 500
            elif name == "max_ms":
                defaults[name] = 2000
            elif name == "index":
                defaults[name] = 0
            elif name == "delay_ms":
                defaults[name] = 0
            else:
                defaults[name] = 0

        elif ptype == "double":
            if name == "threshold":
                defaults[name] = 0.90
            elif name == "x":
                defaults[name] = 0.5
            elif name == "y":
                defaults[name] = 0.5
            else:
                defaults[name] = 0.0

        elif ptype in ("string", "multiline", "flow_reference"):
            defaults[name] = ""

        elif ptype == "locator":
            defaults[name] = ""

        elif ptype == "bool":
            defaults[name] = False

        elif ptype == "enum":
            options = param.get("options", [])
            if "found" in options:
                defaults[name] = "found"
            elif options:
                defaults[name] = options[0]
            else:
                defaults[name] = ""

        elif ptype in ("hook_list", "image_list", "key_list", "string_list"):
            defaults[name] = []

        else:
            defaults[name] = ""

    return defaults
