"""Qt-free semantic validation for automation flows.

The preflight service deliberately consumes the same command metadata as the
editor and executor.  It is read-only: database collaborators are queried only
for resource names and called flows.
"""

from __future__ import annotations

import os
import re
import uuid
import math
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from automadin.execution.handlers.registry import load_command_types
from automadin.models.device import AndroidDevice, BrowserDevice, DeviceType, WindowsDevice
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION
from automadin.models.hook import (
    AutomationHook,
    BrowserElementHookCondition,
    HookAutomaticScope,
    HookBinding,
    HookMatch,
    HookType,
    VisualImageHookCondition,
)
from automadin.models.hook_compatibility import hook_points_for_command
from automadin.models.flow_value import (
    FlowValueCatalog,
    FlowValueDeclarationError,
)
from automadin.models.locator import LocatorSpec, validate_locator
from automadin.models.builtin_scope import (
    IMAGE_MATCH_BUILTIN_TYPES,
    extend_builtin_types,
)
from automadin.models.run_flow_binding import RunFlowBinding, RunFlowBindingError
from automadin.models.typed_value import ValueType, is_value_type_assignable
from automadin.models.value_reference import (
    FlowValueTemplate,
    LiteralValueReference,
    TemplateTextSegment,
    ValueReferenceError,
    resolve_value_reference_type,
    value_reference_from_dict,
)
from automadin.models.variable_update import (
    VariableUpdate,
    VariableUpdateError,
    VariableUpdateOperation,
    variable_update_allowed_operations,
    variable_update_allowed_source_types,
)
from automadin.models.value_condition import ValueCondition, ValueConditionError
from automadin.services.windows_desktop import (
    windows_command_requires_interactive_desktop,
)


class PreflightSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class PreflightMode(str, Enum):
    DOCUMENT = "document"
    EXECUTION = "execution"


class TargetKind(str, Enum):
    BROWSER = "browser"
    ANDROID = "android"
    WINDOWS = "windows"


CommandPath = tuple[int | str, ...]


@dataclass(frozen=True)
class PreflightLocation:
    flow_name: str
    command_path: CommandPath
    step_id: str
    command_id: str
    parameter: str = ""
    scope: str = ""

    @property
    def display_path(self) -> str:
        """Canonical path suitable for logs and user interfaces."""
        return _format_path(self.command_path)

    @property
    def display_text(self) -> str:
        """Describe either a flow step or an external execution concern."""
        if self.scope:
            return self.scope
        parameter = f" · {self.parameter}" if self.parameter else ""
        command = (
            f" · {self.command_id} [{self.step_id[:4]}]"
            if self.command_id
            else ""
        )
        return f"{self.flow_name} · {self.display_path}{command}{parameter}"


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    severity: PreflightSeverity
    message: str
    location: PreflightLocation
    remediation: str = ""


@dataclass(frozen=True)
class PreflightReport:
    issues: tuple[PreflightIssue, ...]
    visited_flows: tuple[str, ...]
    requires_interactive_desktop: bool = False

    @property
    def errors(self) -> tuple[PreflightIssue, ...]:
        return tuple(i for i in self.issues if i.severity is PreflightSeverity.ERROR)

    @property
    def warnings(self) -> tuple[PreflightIssue, ...]:
        return tuple(i for i in self.issues if i.severity is PreflightSeverity.WARNING)

    @property
    def infos(self) -> tuple[PreflightIssue, ...]:
        return tuple(i for i in self.issues if i.severity is PreflightSeverity.INFO)

    @property
    def can_execute(self) -> bool:
        return not self.errors

    def format_text(self) -> str:
        """Return a compact, deterministic, human-readable report."""
        if not self.issues:
            flows = len(self.visited_flows)
            return f"Preflight passed ({flows} flow{'s' if flows != 1 else ''} checked)."
        lines: list[str] = []
        for issue in self.issues:
            loc = issue.location
            lines.append(
                f"[{issue.severity.value.upper()}] "
                f"{loc.display_text} {issue.message}"
            )
        return "\n".join(lines)

    @property
    def formatted_text(self) -> str:
        return self.format_text()

    def __str__(self) -> str:
        return self.format_text()


class FlowPreflightError(ValueError):
    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        super().__init__(report.format_text())


_ALL_TARGETS = frozenset(TargetKind)
_BROWSER_ONLY = frozenset({TargetKind.BROWSER})
_ANDROID_ONLY = frozenset({TargetKind.ANDROID})
_WINDOWS_ONLY = frozenset({TargetKind.WINDOWS})
_BROWSER_ANDROID = frozenset({TargetKind.BROWSER, TargetKind.ANDROID})

# Intentional exceptions to the category defaults.  This is the only place a
# command-specific target capability should be added.
_TARGET_OVERRIDES: Mapping[str, frozenset[TargetKind]] = {
    "clear_text_field": _BROWSER_ANDROID,
    "swipe": _BROWSER_ANDROID,
    "press_keycode": _BROWSER_ANDROID,
    "input_fill": _BROWSER_ONLY,
    "for_each_browser_element": _BROWSER_ONLY,
    # This acts on the local Windows host, not the selected automation target.
    "hibernate_computer": _ALL_TARGETS,
}


def _build_target_matrix(
    command_types: Mapping[str, Mapping[str, Any]],
) -> dict[str, frozenset[TargetKind]]:
    matrix: dict[str, frozenset[TargetKind]] = {}
    for command_id, definition in command_types.items():
        category = str(definition.get("category", "")).casefold()
        if category == "browser":
            targets = _BROWSER_ONLY
        elif category == "android":
            targets = _ANDROID_ONLY
        elif category == "windows":
            targets = _WINDOWS_ONLY
        else:
            targets = _ALL_TARGETS
        matrix[command_id] = _TARGET_OVERRIDES.get(command_id, targets)

    unknown_overrides = set(_TARGET_OVERRIDES).difference(command_types)
    uncovered = set(command_types).difference(matrix)
    if unknown_overrides or uncovered:
        details = []
        if unknown_overrides:
            details.append(f"unknown overrides: {sorted(unknown_overrides)!r}")
        if uncovered:
            details.append(f"uncovered commands: {sorted(uncovered)!r}")
        raise ValueError("Invalid preflight target matrix (" + "; ".join(details) + ")")
    return matrix


_COMMAND_TYPES = load_command_types()
_TARGET_MATRIX = _build_target_matrix(_COMMAND_TYPES)
_LOCATOR_COMMANDS = frozenset(
    command_id
    for command_id, definition in _COMMAND_TYPES.items()
    if any(param.get("type") == "locator" for param in definition.get("params", ()))
)


def supported_targets(command_id: str) -> frozenset[TargetKind]:
    """Return the execution targets supported by a registered command."""
    try:
        return _TARGET_MATRIX[command_id]
    except KeyError as exc:
        raise KeyError(f"Unknown command id: {command_id}") from exc


def target_kind_for_device(device: object) -> TargetKind:
    """Normalize a concrete device, device type, or target-like value."""
    if isinstance(device, TargetKind):
        return device
    if isinstance(device, DeviceType):
        return TargetKind(device.value)
    if isinstance(device, BrowserDevice):
        return TargetKind.BROWSER
    if isinstance(device, AndroidDevice):
        return TargetKind.ANDROID
    if isinstance(device, WindowsDevice):
        return TargetKind.WINDOWS

    value = getattr(device, "value", device)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        try:
            return TargetKind(normalized)
        except ValueError:
            pass
    raise ValueError(f"Unsupported execution target: {device!r}")


_VARIABLE = re.compile(r"\{\{\w+\}\}")
_ANY_BRACE = re.compile(r"[{}]")
_SELECTOR_PARAMS = frozenset({"selector"})
_DELAY_PAIRS = (
    ("min_delay", "max_delay"),
    ("minDelay", "maxDelay"),
    ("min_ms", "max_ms"),
)
_IMAGE_ALIAS = "image_name"
_GLOBAL_PARAM_NAMES = frozenset({"pause_ms", "min_wait_time"})
_EMPTY_FALLBACK_PARAMS = frozenset(
    {
        ("press_keycode", "keys"),
        ("win_key", "keys"),
    }
)


def _format_path(path: CommandPath) -> str:
    if not path:
        return "<flow>"
    if path[0] == "hooks":
        output = f"hooks[{path[1]}].actions[{path[3]}]"
        index = 4
        while index < len(path):
            output += f".{path[index]}[{path[index + 1]}]"
            index += 2
        return output
    output = f"commands[{path[0]}]"
    index = 1
    while index < len(path):
        branch = path[index]
        child_index = path[index + 1]
        output += f".{branch}[{child_index}]"
        index += 2
    return output


def _get(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _is_dynamic(value: str) -> bool:
    return bool(_VARIABLE.search(value))


def _has_malformed_template(value: str) -> bool:
    return bool(_ANY_BRACE.search(_VARIABLE.sub("", value)))


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == ()


def _is_defaultish_unknown(value: Any) -> bool:
    return (
        value is None
        or value == ""
        or value == 0
        or value is False
        or value == []
        or value == ()
    )


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _expected_type(param_type: str, value: Any) -> bool:
    if param_type == "locator":
        return validate_locator(value)
    if param_type == "value_reference":
        return isinstance(value, Mapping)
    if param_type == "variable_target":
        return isinstance(value, str)
    if param_type in {"string", "multiline", "flow_reference", "visual_rule"}:
        return isinstance(value, str)
    if param_type in {"int"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if param_type in {"double"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if param_type == "bool":
        return isinstance(value, bool)
    if param_type in {"enum"}:
        return isinstance(value, str)
    if param_type in {"hook_list", "image_list", "key_list", "string_list"}:
        return isinstance(value, list) and all(
            isinstance(item, str) for item in value
        )
    return False


def _resource_names(rows: Iterable[Any]) -> set[str]:
    names: set[str] = set()
    for row in rows:
        value = row.get("name") if isinstance(row, Mapping) else getattr(row, "name", row)
        if isinstance(value, str):
            names.add(value)
    return names


class _Validator:
    def __init__(
        self,
        *,
        db: object | None,
        target: TargetKind | None,
        mode: PreflightMode,
        include_called_flows: bool,
    ) -> None:
        self.db = db
        self.target = target
        self.mode = mode
        self.include_called_flows = include_called_flows
        self.issues: list[PreflightIssue] = []
        self.visited: list[str] = []
        self._visited_set: set[str] = set()
        self._stack: list[str] = []
        self._flow_stack: list[object] = []
        self._flow_id_stack: list[str] = []
        self._step_ids: dict[str, PreflightLocation] = {}
        self._hook_maps: list[dict[str, AutomationHook]] = []
        self._images: set[str] | None = None
        self._visual_rules: set[str] | None = None
        self._image_lookup_error: str | None = None
        self._visual_rule_lookup_error: str | None = None
        self._flow_lookup_error: str | None = None
        self.requires_interactive_desktop = False

    def _lookup_failure_severity(self) -> PreflightSeverity:
        return (
            PreflightSeverity.ERROR
            if self.mode is PreflightMode.EXECUTION
            else PreflightSeverity.WARNING
        )

    def add(
        self,
        code: str,
        severity: PreflightSeverity,
        message: str,
        location: PreflightLocation,
        *,
        remediation: str = "",
        active: bool = True,
    ) -> None:
        if not active and severity in {
            PreflightSeverity.ERROR,
            PreflightSeverity.WARNING,
        }:
            severity = PreflightSeverity.INFO
            message = f"Disabled command: {message}"
        self.issues.append(
            PreflightIssue(code, severity, message, location, remediation)
        )

    def validate_flow(self, flow: object, name: str, *, active: bool = True) -> None:
        if name not in self._visited_set:
            self._visited_set.add(name)
            self.visited.append(name)
        self._stack.append(name)
        self._flow_stack.append(flow)
        flow_id = _get(flow, "flow_id", "")
        self._flow_id_stack.append(flow_id if isinstance(flow_id, str) else "")
        parent_step_ids = self._step_ids
        self._step_ids = {}
        hooks = self._validate_hooks(flow, name, active=active)
        self._hook_maps.append(hooks)
        commands = _get(flow, "commands", None)
        root = PreflightLocation(name, (), "", "")
        if not isinstance(commands, (list, tuple)):
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "Flow commands must be a list.",
                root,
                active=active,
            )
        else:
            self._validate_commands(commands, name, (), active=active, builtin_types={})
        self._hook_maps.pop()
        self._step_ids = parent_step_ids
        self._flow_id_stack.pop()
        self._flow_stack.pop()
        self._stack.pop()

    def _validate_commands(
        self,
        commands: Sequence[Any],
        flow_name: str,
        prefix: CommandPath,
        *,
        active: bool,
        builtin_types: Mapping[str, ValueType],
        parent_command: object | None = None,
        in_hook_body: bool = False,
    ) -> None:
        for index, command in enumerate(commands):
            path = prefix + (index,)
            self._validate_command(
                command,
                flow_name,
                path,
                active=active,
                builtin_types=builtin_types,
                parent_command=parent_command,
                in_hook_body=in_hook_body,
            )

    def _validate_command(
        self,
        command: object,
        flow_name: str,
        path: CommandPath,
        *,
        active: bool,
        builtin_types: Mapping[str, ValueType],
        parent_command: object | None = None,
        in_hook_body: bool = False,
    ) -> None:
        if not isinstance(command, Mapping) and not hasattr(command, "id"):
            location = PreflightLocation(flow_name, path, "", "")
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "Command must be an object.",
                location,
                active=active,
            )
            return

        command_id = _get(command, "id", "")
        step_id = _get(command, "step_id", "")
        enabled = _get(command, "enabled", True)
        command_active = active and enabled is not False
        location = PreflightLocation(
            flow_name,
            path,
            step_id if isinstance(step_id, str) else "",
            command_id if isinstance(command_id, str) else "",
        )

        if not isinstance(enabled, bool):
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "enabled must be a boolean.",
                PreflightLocation(
                    location.flow_name,
                    location.command_path,
                    location.step_id,
                    location.command_id,
                    "enabled",
                ),
                active=active,
            )
        field_types = (
            ("comment", "", str),
            ("breakpoint", False, bool),
            ("on_error", "stop", str),
            ("retry_count", 0, int),
            ("min_wait_time", 0, int),
            ("collapsed", True, bool),
        )
        for field_name, default, expected in field_types:
            field_value = _get(command, field_name, default)
            valid = isinstance(field_value, expected)
            if expected is int:
                valid = valid and not isinstance(field_value, bool)
            if not valid:
                self.add(
                    "INVALID_COMMAND_FIELD",
                    PreflightSeverity.ERROR,
                    f"'{field_name}' must be {expected.__name__}.",
                    PreflightLocation(
                        location.flow_name,
                        location.command_path,
                        location.step_id,
                        location.command_id,
                        field_name,
                    ),
                    active=command_active,
                )
        retry_count = _get(command, "retry_count", 0)
        if (
            isinstance(retry_count, int)
            and not isinstance(retry_count, bool)
            and not 0 <= retry_count <= 100
        ):
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "'retry_count' must be between 0 and 100.",
                PreflightLocation(
                    location.flow_name,
                    location.command_path,
                    location.step_id,
                    location.command_id,
                    "retry_count",
                ),
                active=command_active,
            )
        on_error = _get(command, "on_error", "stop")
        if isinstance(on_error, str) and on_error not in {"stop", "skip", "retry"}:
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "'on_error' must be one of: stop, skip, retry.",
                PreflightLocation(
                    location.flow_name,
                    location.command_path,
                    location.step_id,
                    location.command_id,
                    "on_error",
                ),
                active=command_active,
            )
        command_min_wait = _get(command, "min_wait_time", 0)
        if (
            isinstance(command_min_wait, int)
            and not isinstance(command_min_wait, bool)
            and command_min_wait < 0
        ):
            self.add(
                "INVALID_PARAMETER_VALUE",
                PreflightSeverity.ERROR,
                "'min_wait_time' cannot be negative.",
                PreflightLocation(
                    location.flow_name,
                    location.command_path,
                    location.step_id,
                    location.command_id,
                    "min_wait_time",
                ),
                active=command_active,
            )
        if not isinstance(command_id, str) or not command_id.strip():
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "Command id must be non-empty text.",
                location,
                active=command_active,
            )
            definition = None
        else:
            definition = _COMMAND_TYPES.get(command_id)
            if definition is None:
                self.add(
                    "UNKNOWN_COMMAND",
                    PreflightSeverity.ERROR,
                    f"Unknown command '{command_id}'.",
                    location,
                    remediation="Replace or remove this command.",
                    active=command_active,
                )

        if not _is_canonical_uuid(step_id):
            self.add(
                "INVALID_STEP_ID",
                PreflightSeverity.ERROR,
                "step_id must be a canonical UUID.",
                location,
                active=command_active,
            )
        elif step_id in self._step_ids:
            first = self._step_ids[step_id]
            self.add(
                "DUPLICATE_STEP_ID",
                PreflightSeverity.ERROR,
                f"step_id duplicates {first.flow_name}:{_format_path(first.command_path)}.",
                location,
                remediation="Assign a unique step_id.",
                active=command_active,
            )
        else:
            self._step_ids[step_id] = location

        params = _get(command, "params", {})
        if not isinstance(params, Mapping):
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "Command params must be an object.",
                location,
                active=command_active,
            )
            params = {}

        self._validate_hook_references(
            command,
            definition,
            params,
            location,
            active=command_active,
            in_hook_body=in_hook_body,
        )

        if definition is not None:
            if (
                command_active
                and self.target is TargetKind.WINDOWS
                and windows_command_requires_interactive_desktop(command_id, params)
            ):
                self.requires_interactive_desktop = True
            if self.target is not None and self.target not in _TARGET_MATRIX[command_id]:
                severity = (
                    PreflightSeverity.ERROR
                    if self.mode is PreflightMode.EXECUTION
                    else PreflightSeverity.WARNING
                )
                self.add(
                    "UNSUPPORTED_TARGET",
                    severity,
                    f"'{command_id}' does not support the {self.target.value} target.",
                    location,
                    active=command_active,
                )
            current_flow = self._flow_stack[-1]
            parent_command_id = _get(parent_command, "id", "")
            if command_id in {"switch_case", "switch_default"} and parent_command_id != "switch_value":
                self.add(
                    "SWITCH_BRANCH_OUTSIDE_SWITCH",
                    PreflightSeverity.ERROR,
                    "Case and Default branches may exist only directly inside Switch Value.",
                    location,
                    active=command_active,
                )
            if command_id == "switch_case" and parent_command_id == "switch_value":
                parent_params = _get(parent_command, "params", {})
                source = parent_params.get("source") if isinstance(parent_params, Mapping) else None
                if "left" in params:
                    self.add(
                        "INVALID_SWITCH_CASE_SOURCE",
                        PreflightSeverity.ERROR,
                        "Switch Case cannot define its own source; use the parent Switch Value.",
                        PreflightLocation(
                            location.flow_name,
                            location.command_path,
                            location.step_id,
                            location.command_id,
                            "left",
                        ),
                        active=command_active,
                    )
                self._validate_value_condition(
                    {**params, "left": source},
                    location,
                    active=command_active,
                    builtin_types=builtin_types,
                    action_label="Switch Case",
                    extra_params=frozenset(),
                )
            elif command_id == "update_variable":
                self._validate_variable_update(
                    params,
                    location,
                    active=command_active,
                    builtin_types=builtin_types,
                )
                self._validate_cross_fields(
                    command_id,
                    params,
                    location,
                    active=command_active,
                )
            elif command_id in {"if_value", "while_value"}:
                condition_extra_params = (
                    frozenset({"timeout"})
                    if command_id == "while_value"
                    else frozenset({"log_values"})
                )
                self._validate_value_condition(
                    params,
                    location,
                    active=command_active,
                    builtin_types=builtin_types,
                    action_label="If Value" if command_id == "if_value" else "While Value",
                    extra_params=condition_extra_params,
                )
                self._validate_cross_fields(
                    command_id,
                    params,
                    location,
                    active=command_active,
                )
            elif (
                command_id == "run_flow"
                and _get(current_flow, "value_model_version", 0)
                == CURRENT_FLOW_VALUE_MODEL_VERSION
            ):
                self._validate_v1_flow_reference(
                    params,
                    location,
                    active=command_active,
                    builtin_types=builtin_types,
                )
                self._validate_cross_fields(
                    command_id,
                    params,
                    location,
                    active=command_active,
                )
            else:
                self._validate_params(
                    command_id,
                    definition,
                    params,
                    location,
                    active=command_active,
                    builtin_types=builtin_types,
                    value_model_version=_get(
                        current_flow,
                        "value_model_version",
                        0,
                    ),
                )
                if command_id == "click_element":
                    target = params.get("target", "locator")
                    if target == "current_element":
                        if "element_index" not in builtin_types:
                            self.add(
                                "CURRENT_BROWSER_ELEMENT_OUT_OF_SCOPE",
                                PreflightSeverity.ERROR,
                                "Current element is available only inside For Each Browser Element.",
                                PreflightLocation(
                                    location.flow_name,
                                    location.command_path,
                                    location.step_id,
                                    location.command_id,
                                    "target",
                                ),
                                active=command_active,
                            )
                    elif _is_missing(params.get("locator")):
                        self.add(
                            "MISSING_PARAMETER",
                            PreflightSeverity.ERROR,
                            "Click Element requires a locator unless it targets the current element.",
                            PreflightLocation(
                                location.flow_name,
                                location.command_path,
                                location.step_id,
                                location.command_id,
                                "locator",
                            ),
                            active=command_active,
                        )
                elif command_id == "scroll_browser":
                    target = params.get("target", "page")
                    if target == "current_element":
                        if "element_index" not in builtin_types:
                            self.add(
                                "CURRENT_BROWSER_ELEMENT_OUT_OF_SCOPE",
                                PreflightSeverity.ERROR,
                                "Current element is available only inside For Each Browser Element.",
                                PreflightLocation(
                                    location.flow_name,
                                    location.command_path,
                                    location.step_id,
                                    location.command_id,
                                    "target",
                                ),
                                active=command_active,
                            )
                    elif target == "locator" and _is_missing(params.get("locator")):
                        self.add(
                            "MISSING_PARAMETER",
                            PreflightSeverity.ERROR,
                            "Scroll Browser requires a locator when its target is Locator.",
                            PreflightLocation(
                                location.flow_name,
                                location.command_path,
                                location.step_id,
                                location.command_id,
                                "locator",
                            ),
                            active=command_active,
                        )
                elif command_id == "extract_text":
                    target = params.get("target", "locator")
                    if target == "current_element" and "element_index" not in builtin_types:
                        self.add(
                            "CURRENT_BROWSER_ELEMENT_OUT_OF_SCOPE",
                            PreflightSeverity.ERROR,
                            "Current element is available only inside For Each Browser Element.",
                            PreflightLocation(
                                location.flow_name,
                                location.command_path,
                                location.step_id,
                                location.command_id,
                                "target",
                            ),
                            active=command_active,
                        )
                elif command_id == "click_dynamic_target":
                    source = params.get("target_source", "images")
                    if source == "images" and _is_missing(params.get("images")):
                        self.add(
                            "EMPTY_IMAGE_LIST",
                            PreflightSeverity.ERROR,
                            "Click Dynamic Target requires recorded poses or images.",
                            PreflightLocation(
                                location.flow_name,
                                location.command_path,
                                location.step_id,
                                location.command_id,
                                "images",
                            ),
                            remediation="Record the target or add representative images.",
                            active=command_active,
                        )
                    if source == "visual_rule" and _is_missing(
                        params.get("target_rule")
                    ):
                        self.add(
                            "MISSING_PARAMETER",
                            PreflightSeverity.ERROR,
                            "Click Dynamic Target requires a Visual Rule for this source.",
                            PreflightLocation(
                                location.flow_name,
                                location.command_path,
                                location.step_id,
                                location.command_id,
                                "target_rule",
                            ),
                            remediation="Choose or create a Visual Rule.",
                            active=command_active,
                        )

        children = _get(command, "children", [])
        else_children = _get(command, "else_children", [])
        children_valid = isinstance(children, (list, tuple))
        else_valid = isinstance(else_children, (list, tuple))
        if not children_valid:
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "children must be a list.",
                location,
                active=command_active,
            )
        if not else_valid:
            self.add(
                "INVALID_COMMAND_FIELD",
                PreflightSeverity.ERROR,
                "else_children must be a list.",
                location,
                active=command_active,
            )

        if definition is not None:
            if children_valid and children and not definition.get("canHaveChildren", False):
                self.add(
                    "ILLEGAL_CHILDREN",
                    PreflightSeverity.ERROR,
                    f"'{command_id}' cannot contain child commands.",
                    location,
                    active=command_active,
                )
            if else_valid and else_children and not definition.get("canHaveElse", False):
                self.add(
                    "ILLEGAL_ELSE",
                    PreflightSeverity.ERROR,
                    f"'{command_id}' cannot contain else commands.",
                    location,
                    active=command_active,
                )
            if command_id == "switch_value" and children_valid:
                self._validate_switch_structure(
                    children,
                    location,
                    active=command_active,
                )

        if children_valid:
            child_builtin_types = extend_builtin_types(
                builtin_types,
                definition.get("childBuiltins", ()) if definition is not None else (),
            )
            self._validate_commands(
                children,
                flow_name,
                path + ("children",),
                active=command_active,
                builtin_types=child_builtin_types,
                parent_command=command,
                in_hook_body=in_hook_body,
            )
        if else_valid:
            self._validate_commands(
                else_children,
                flow_name,
                path + ("else_children",),
                active=command_active,
                builtin_types=builtin_types,
                parent_command=command,
                in_hook_body=in_hook_body,
            )

    def _validate_hooks(
        self,
        flow: object,
        flow_name: str,
        *,
        active: bool,
    ) -> dict[str, AutomationHook]:
        raw_hooks = _get(flow, "hooks", [])
        root = PreflightLocation(flow_name, (), "", "", "hooks")
        if not isinstance(raw_hooks, (list, tuple)):
            self.add(
                "INVALID_HOOKS",
                PreflightSeverity.ERROR,
                "Flow hooks must be a list.",
                root,
                active=active,
            )
            return {}

        hooks: dict[str, AutomationHook] = {}
        names: dict[str, str] = {}
        normalized: list[tuple[int, AutomationHook]] = []
        for index, raw_hook in enumerate(raw_hooks):
            location = PreflightLocation(
                flow_name,
                (),
                "",
                "",
                scope=f"{flow_name} · hooks[{index}]",
            )
            try:
                hook = (
                    raw_hook
                    if isinstance(raw_hook, AutomationHook)
                    else AutomationHook.from_dict(raw_hook)
                )
            except (TypeError, ValueError) as exc:
                self.add(
                    "INVALID_HOOK",
                    PreflightSeverity.ERROR,
                    str(exc),
                    location,
                    active=active,
                )
                continue
            if hook.hook_id in hooks:
                self.add(
                    "DUPLICATE_HOOK_ID",
                    PreflightSeverity.ERROR,
                    f"Hook ID duplicates '{hooks[hook.hook_id].name}'.",
                    location,
                    active=active,
                )
            else:
                hooks[hook.hook_id] = hook
            name_key = hook.name.strip().casefold()
            if name_key in names:
                self.add(
                    "DUPLICATE_HOOK_NAME",
                    PreflightSeverity.ERROR,
                    f"Hook name duplicates '{names[name_key]}'.",
                    location,
                    active=active,
                )
            else:
                names[name_key] = hook.name
            normalized.append((index, hook))

        # Make definitions available while validating dormant action trees.
        self._hook_maps.append(hooks)
        try:
            for index, hook in normalized:
                location = PreflightLocation(
                    flow_name,
                    (),
                    "",
                    "",
                    scope=f"{flow_name} · hooks[{index}] ({hook.name})",
                )
                self._validate_hook_condition(hook, location, active=active)
                if (
                    hook.enabled
                    and hook.automatic_scope
                    is HookAutomaticScope.ALL_COMPATIBLE_COMMANDS
                ):
                    self._validate_hook_target(hook, location, active=active)
                self._validate_commands(
                    hook.actions,
                    flow_name,
                    ("hooks", index, "actions"),
                    active=active and hook.enabled,
                    builtin_types=(
                        IMAGE_MATCH_BUILTIN_TYPES
                        if isinstance(hook.condition, VisualImageHookCondition)
                        and hook.condition.match is HookMatch.FOUND
                        else {}
                    ),
                    in_hook_body=True,
                )
        finally:
            self._hook_maps.pop()
        return hooks

    def _validate_hook_condition(
        self,
        hook: AutomationHook,
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        condition = hook.condition
        if isinstance(condition, VisualImageHookCondition):
            self._validate_params(
                "find_image",
                _COMMAND_TYPES["find_image"],
                condition.match_params(),
                location,
                active=active and hook.enabled,
                builtin_types={},
                value_model_version=_get(
                    self._flow_stack[-1],
                    "value_model_version",
                    0,
                ),
            )
            return
        if isinstance(condition, BrowserElementHookCondition):
            self._validate_params(
                "assert_visible",
                _COMMAND_TYPES["assert_visible"],
                {"locator": condition.locator},
                location,
                active=active and hook.enabled,
                builtin_types={},
                value_model_version=_get(
                    self._flow_stack[-1],
                    "value_model_version",
                    0,
                ),
            )

    def _validate_hook_target(
        self,
        hook: AutomationHook,
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        if self.target is None or hook.type is not HookType.BROWSER_ELEMENT:
            return
        if self.target is TargetKind.BROWSER:
            return
        severity = (
            PreflightSeverity.ERROR
            if self.mode is PreflightMode.EXECUTION
            else PreflightSeverity.WARNING
        )
        self.add(
            "UNSUPPORTED_HOOK_TARGET",
            severity,
            f"Hook '{hook.name}' requires a browser target.",
            location,
            remediation="Use a Browser Run Setup or remove this hook invocation.",
            active=active and hook.enabled,
        )

    def _validate_hook_references(
        self,
        command: object,
        definition: Mapping[str, Any] | None,
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
        in_hook_body: bool,
    ) -> None:
        hooks = self._hook_maps[-1] if self._hook_maps else {}
        raw_bindings = _get(command, "hook_bindings", [])
        if not isinstance(raw_bindings, (list, tuple)):
            self.add(
                "INVALID_HOOK_BINDINGS",
                PreflightSeverity.ERROR,
                "hook_bindings must be a list.",
                location,
                active=active,
            )
            return
        bindings: list[HookBinding] = []
        for raw_binding in raw_bindings:
            try:
                binding = (
                    raw_binding
                    if isinstance(raw_binding, HookBinding)
                    else HookBinding.from_dict(raw_binding)
                )
            except (TypeError, ValueError) as exc:
                self.add(
                    "INVALID_HOOK_BINDING",
                    PreflightSeverity.ERROR,
                    str(exc),
                    location,
                    active=active,
                )
                continue
            bindings.append(binding)

        if in_hook_body and (bindings or _get(command, "id", "") == "check_hooks"):
            self.add(
                "HOOK_RECURSION",
                PreflightSeverity.ERROR,
                "Hook actions cannot bind or explicitly check hooks.",
                location,
                active=active,
            )
            return

        seen: set[str] = set()
        points = hook_points_for_command(definition)
        for binding in bindings:
            hook = hooks.get(binding.hook_id)
            if binding.hook_id in seen:
                self.add(
                    "DUPLICATE_HOOK_BINDING",
                    PreflightSeverity.ERROR,
                    f"Hook '{binding.hook_id}' is bound more than once.",
                    location,
                    active=active,
                )
                continue
            seen.add(binding.hook_id)
            if hook is None:
                self.add(
                    "STALE_HOOK_REFERENCE",
                    PreflightSeverity.ERROR,
                    f"Bound hook '{binding.hook_id}' does not exist.",
                    location,
                    active=active,
                )
                continue
            if hook.automatic_scope is not HookAutomaticScope.SELECTED_COMMANDS:
                self.add(
                    "INACTIVE_HOOK_BINDING",
                    PreflightSeverity.ERROR,
                    f"Hook '{hook.name}' is bound to a command but its automatic "
                    f"scope is '{hook.automatic_scope.value}'.",
                    location,
                    remediation=(
                        "Set the hook scope to Selected commands or remove the "
                        "command binding."
                    ),
                    active=active,
                )
            point = next(
                (item for item in points if item.get("type") == hook.type.value),
                None,
            )
            if point is None or any(
                phase.value not in point.get("phases", ())
                for phase in binding.phases
            ):
                self.add(
                    "INCOMPATIBLE_HOOK_BINDING",
                    PreflightSeverity.ERROR,
                    f"Hook '{hook.name}' is not compatible with '{location.command_id}' "
                    "at the selected phase.",
                    location,
                    active=active,
                )
            self._validate_hook_target(hook, location, active=active)

        if _get(command, "id", "") != "check_hooks":
            return
        hook_ids = params.get("hooks", [])
        if not isinstance(hook_ids, list):
            return
        explicit_seen: set[str] = set()
        for hook_id in hook_ids:
            if not isinstance(hook_id, str):
                continue
            if hook_id in explicit_seen:
                self.add(
                    "DUPLICATE_HOOK_REFERENCE",
                    PreflightSeverity.ERROR,
                    f"Check Hooks repeats hook '{hook_id}'.",
                    location,
                    active=active,
                )
                continue
            explicit_seen.add(hook_id)
            hook = hooks.get(hook_id)
            if hook is None:
                self.add(
                    "STALE_HOOK_REFERENCE",
                    PreflightSeverity.ERROR,
                    f"Check Hooks references missing hook '{hook_id}'.",
                    location,
                    active=active,
                )
            else:
                self._validate_hook_target(hook, location, active=active)

    def _validate_switch_structure(
        self,
        children: Sequence[Any],
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        cases = [child for child in children if _get(child, "id", "") == "switch_case"]
        defaults = [child for child in children if _get(child, "id", "") == "switch_default"]
        if not cases:
            self.add(
                "SWITCH_CASE_REQUIRED",
                PreflightSeverity.ERROR,
                "Switch Value requires at least one Case.",
                location,
                active=active,
            )
        if len(defaults) != 1:
            self.add(
                "SWITCH_DEFAULT_COUNT",
                PreflightSeverity.ERROR,
                "Switch Value requires exactly one Default branch.",
                location,
                active=active,
            )
        if defaults and _get(children[-1], "id", "") != "switch_default":
            self.add(
                "SWITCH_DEFAULT_LAST",
                PreflightSeverity.ERROR,
                "Switch Value Default must be the final branch.",
                location,
                active=active,
            )
        if len(cases) + len(defaults) != len(children):
            self.add(
                "INVALID_SWITCH_BRANCH",
                PreflightSeverity.ERROR,
                "Switch Value may contain only Case branches followed by Default.",
                location,
                active=active,
            )

        signatures: dict[str, int] = {}
        for index, case in enumerate(children):
            if _get(case, "id", "") != "switch_case":
                continue
            case_params = _get(case, "params", {})
            try:
                signature = json.dumps(case_params, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                continue
            if signature in signatures:
                case_location = PreflightLocation(
                    location.flow_name,
                    location.command_path + ("children", index),
                    str(_get(case, "step_id", "")),
                    "switch_case",
                )
                self.add(
                    "DUPLICATE_SWITCH_CASE",
                    PreflightSeverity.ERROR,
                    f"Switch Case duplicates Case {signatures[signature] + 1}.",
                    case_location,
                    active=active,
                )
            else:
                signatures[signature] = index

    def _validate_params(
        self,
        command_id: str,
        definition: Mapping[str, Any],
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
        builtin_types: Mapping[str, ValueType],
        value_model_version: int,
    ) -> None:
        known_names = set(_GLOBAL_PARAM_NAMES)
        for param in definition.get("params", []):
            name = param["name"]
            if command_id == "extract_text":
                target = params.get("target", "locator")
                if target == "current_element" and name == "locator":
                    continue
                if target != "current_element" and name == "child_selector":
                    continue
            if (
                command_id == "clear_text_field"
                and name == "selector"
                and self.target is TargetKind.ANDROID
            ):
                # Android clears the currently focused field with key events;
                # the browser-only selector is intentionally unused.
                known_names.add(name)
                continue
            aliases = tuple(param.get("aliases", ()))
            if name == "images":
                aliases += (_IMAGE_ALIAS,)
            known_names.update((name, *aliases))
            supplied = [candidate for candidate in (name, *aliases) if candidate in params]
            meaningful = [
                candidate for candidate in supplied if not _is_missing(params[candidate])
            ]
            if len(meaningful) > 1:
                self.add(
                    "INVALID_PARAMETER_VALUE",
                    PreflightSeverity.INFO,
                    (
                        f"Both {', '.join(meaningful)} are set; runtime compatibility "
                        f"rules use '{meaningful[0]}'."
                    ),
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )
            fallback_on_empty = (
                name == "images"
                or (command_id, name) in _EMPTY_FALLBACK_PARAMS
            )
            candidates = meaningful if fallback_on_empty else supplied
            source = (candidates or supplied or [name])[0]
            present = bool(supplied)
            value = params.get(source)
            param_location = PreflightLocation(
                location.flow_name,
                location.command_path,
                location.step_id,
                location.command_id,
                source,
            )

            required = not param.get("optional", False) and "default" not in param
            missing_value = _is_missing(value) and not (
                param.get("allow_empty", False) and value == ""
            )
            empty_selector = (
                name in _SELECTOR_PARAMS
                and present
                and isinstance(value, str)
                and not value.strip()
            )
            empty_locator = (
                param.get("type") == "locator"
                and present
                and isinstance(value, str)
                and not value.strip()
                and not param.get("allow_empty", False)
            )
            if (
                (not present or missing_value)
                and required
                and not empty_selector
                and not empty_locator
            ):
                severity = PreflightSeverity.ERROR
                message = f"Required parameter '{name}' is missing."
                remediation = ""
                if (
                    command_id == "clear_text_field"
                    and name == "selector"
                    and self.target is None
                ):
                    severity = PreflightSeverity.WARNING
                    message = (
                        "Selector is required when this command runs in a browser; "
                        "Android clears the focused field without one."
                    )
                    remediation = (
                        "Set a browser selector, or run this flow only on Android."
                    )
                code = "EMPTY_IMAGE_LIST" if param.get("type") == "image_list" else "MISSING_PARAMETER"
                self.add(
                    code,
                    severity,
                    message,
                    param_location,
                    remediation=remediation,
                    active=active,
                )
                continue
            if empty_locator:
                self.add(
                    "EMPTY_SELECTOR",
                    PreflightSeverity.ERROR,
                    "Element locator cannot be empty.",
                    param_location,
                    active=active,
                )
                continue
            if param.get("allow_empty", False) and value == "":
                continue
            if not present or value is None:
                continue
            param_type = str(param.get("type", "string"))
            legacy_image = name == "images" and source == _IMAGE_ALIAS
            structured_template = bool(
                value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
                and param_type in {"string", "multiline"}
                and isinstance(value, Mapping)
                and param.get("supportsValueTemplate", True)
            )
            structured_reference = bool(
                value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
                and param.get("supportsValueReference", False)
                and isinstance(value, Mapping)
            )
            if structured_template:
                if not self._validate_v1_value_template(
                    value,
                    param_location,
                    active=active,
                    builtin_types=builtin_types,
                ):
                    continue
                valid_type = True
            elif structured_reference:
                valid_type = self._validate_v1_value_reference(
                    value,
                    param,
                    param_location,
                    active=active,
                    builtin_types=builtin_types,
                )
            else:
                valid_type = _expected_type(param_type, value)
            if legacy_image:
                valid_type = isinstance(value, str)
            elif (
                command_id == "press_keycode"
                and name == "keys"
                and source == "keycode"
            ):
                valid_type = (
                    isinstance(value, (str, int)) and not isinstance(value, bool)
                )
            elif command_id == "win_key" and name == "keys" and source == "key":
                valid_type = isinstance(value, str)
            elif (
                command_id == "for_each"
                and name == "values"
                and source == "items"
            ):
                valid_type = isinstance(value, (str, list)) and (
                    not isinstance(value, list)
                    or all(isinstance(item, str) for item in value)
                )
            if not valid_type:
                if not structured_reference:
                    self.add(
                        "INVALID_PARAMETER_TYPE",
                        PreflightSeverity.ERROR,
                        f"'{name}' must be {param_type}.",
                        param_location,
                        active=active,
                    )
                continue

            if (
                value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
                and param.get("valueRole") == "output_target"
            ):
                self._validate_v1_output_target(
                    value,
                    param,
                    param_location,
                    active=active,
                )

            if structured_template or structured_reference:
                pass
            elif param_type == "locator":
                self._validate_locator_templates(value, param_location, active=active)
            else:
                self._validate_templates(
                    value,
                    param_location,
                    active=active,
                    allows_literal_braces=param.get("allowsLiteralBraces", False),
                )
            if param_type == "enum" and value not in param.get("options", []):
                self.add(
                    "INVALID_PARAMETER_VALUE",
                    PreflightSeverity.ERROR,
                    f"'{value}' is not an allowed value for '{name}'.",
                    param_location,
                    active=active,
                )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = param.get("min")
                maximum = param.get("max")
                if minimum is not None and value < minimum:
                    self.add(
                        "INVALID_PARAMETER_VALUE",
                        PreflightSeverity.ERROR,
                        f"'{name}' must be at least {minimum}.",
                        param_location,
                        active=active,
                    )
                if maximum is not None and value > maximum:
                    self.add(
                        "INVALID_PARAMETER_VALUE",
                        PreflightSeverity.ERROR,
                        f"'{name}' must be at most {maximum}.",
                        param_location,
                        active=active,
                    )

            if param_type == "image_list":
                self._validate_images(value, param_location, active=active)
            elif param_type == "visual_rule":
                self._validate_visual_rule(value, param_location, active=active)

        self._validate_cross_fields(command_id, params, location, active=active)
        for name in sorted(set(params).difference(known_names)):
            value = params[name]
            defaultish = _is_defaultish_unknown(value)
            self.add(
                "UNKNOWN_PARAMETER",
                (
                    PreflightSeverity.INFO
                    if defaultish
                    else PreflightSeverity.ERROR
                ),
                (
                    f"Unknown parameter '{name}' has no effect."
                    if defaultish
                    else f"Unknown parameter '{name}' is not understood and may be a typo."
                ),
                PreflightLocation(**{**location.__dict__, "parameter": name}),
                remediation=(
                    "Remove the unused parameter."
                    if defaultish
                    else "Correct the parameter name or remove it."
                ),
                active=active,
            )

    def _validate_v1_value_reference(
        self,
        value: object,
        param: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
        builtin_types: Mapping[str, ValueType],
    ) -> bool:
        catalog = self._flow_value_catalog(self._flow_stack[-1])
        if catalog is None:
            self.add(
                "INVALID_VALUE_REFERENCE",
                PreflightSeverity.ERROR,
                "Flow value declarations could not be validated.",
                location,
                active=active,
            )
            return False
        try:
            reference = value_reference_from_dict(value)
            source_type = resolve_value_reference_type(
                reference,
                catalog,
                builtin_types=builtin_types,
            )
        except (ValueReferenceError, FlowValueDeclarationError) as exc:
            self.add(
                "INVALID_VALUE_REFERENCE",
                PreflightSeverity.ERROR,
                str(exc),
                location,
                active=active,
            )
            return False
        allowed = {
            ValueType(item) for item in param.get("allowed_value_types", [])
        }
        if allowed and not any(
            is_value_type_assignable(source_type, target) for target in allowed
        ):
            expected = ", ".join(sorted(item.value for item in allowed))
            self.add(
                "VALUE_REFERENCE_TYPE_MISMATCH",
                PreflightSeverity.ERROR,
                f"Value reference provides {source_type.value}; expected {expected}.",
                location,
                active=active,
            )
            return False
        return True

    def _validate_v1_output_target(
        self,
        value: object,
        param: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        if value == "" and param.get("optional", False):
            return
        catalog = self._flow_value_catalog(self._flow_stack[-1])
        target = catalog.variable_by_id(value) if catalog and isinstance(value, str) else None
        if target is None:
            self.add(
                "STALE_OUTPUT_VARIABLE",
                PreflightSeverity.ERROR,
                f"Output target Variable '{value}' does not exist.",
                location,
                remediation="Select a declared Variable.",
                active=active,
            )
            return
        allowed_types = {
            ValueType(item) for item in param.get("allowed_value_types", [])
        }
        if allowed_types and target.type not in allowed_types:
            expected = ", ".join(sorted(item.value for item in allowed_types))
            self.add(
                "OUTPUT_VARIABLE_TYPE_MISMATCH",
                PreflightSeverity.ERROR,
                f"Variable '{target.name}' is {target.type.value}; this command "
                f"requires an output Variable of type {expected}.",
                location,
                remediation="Select a compatible declared Variable.",
                active=active,
            )

    def _validate_templates(
        self,
        value: Any,
        location: PreflightLocation,
        *,
        active: bool,
        allows_literal_braces: bool = False,
    ) -> None:
        if allows_literal_braces:
            return
        values = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else ()
        for item in values:
            if isinstance(item, str) and _has_malformed_template(item):
                self.add(
                    "MALFORMED_TEMPLATE",
                    PreflightSeverity.ERROR,
                    "Template variables must use the form {{word}}.",
                    location,
                    active=active,
                )
                return

    def _validate_v1_value_template(
        self,
        value: object,
        location: PreflightLocation,
        *,
        active: bool,
        builtin_types: Mapping[str, ValueType],
    ) -> bool:
        try:
            template = FlowValueTemplate.from_dict(value)
        except ValueReferenceError as exc:
            self.add(
                "INVALID_VALUE_TEMPLATE",
                PreflightSeverity.ERROR,
                str(exc),
                location,
                active=active,
            )
            return False
        catalog = self._flow_value_catalog(self._flow_stack[-1])
        if catalog is None:
            self.add(
                "INVALID_VALUE_TEMPLATE",
                PreflightSeverity.ERROR,
                "Flow value declarations could not be validated.",
                location,
                active=active,
            )
            return False
        for segment in template.segments:
            if isinstance(segment, TemplateTextSegment):
                continue
            try:
                resolve_value_reference_type(
                    segment,
                    catalog,
                    builtin_types=builtin_types,
                )
            except ValueReferenceError as exc:
                self.add(
                    "INVALID_VALUE_TEMPLATE",
                    PreflightSeverity.ERROR,
                    str(exc),
                    location,
                    active=active,
                )
        return True

    def _validate_locator_templates(
        self,
        value: object,
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        values = LocatorSpec.parse(value).text_values()
        if any(_has_malformed_template(item) for item in values):
            self.add(
                "MALFORMED_TEMPLATE",
                PreflightSeverity.ERROR,
                "Template variables must use the form {{word}}.",
                location,
                active=active,
            )
            return
        if any(_is_dynamic(item) for item in values):
            self.add(
                "DYNAMIC_REFERENCE",
                PreflightSeverity.WARNING,
                "Dynamic locator cannot be verified before execution.",
                location,
                active=active,
            )

    def _validate_images(
        self,
        value: Any,
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        names = [value] if isinstance(value, str) else list(value)
        valid_names = [name for name in names if not _has_malformed_template(name)]
        literal = [name for name in valid_names if not _is_dynamic(name)]
        dynamic = [name for name in valid_names if _is_dynamic(name)]
        if dynamic:
            qualifier = " Mixed literal values will still be checked." if literal else ""
            self.add(
                "DYNAMIC_REFERENCE",
                PreflightSeverity.WARNING,
                "Dynamic image references cannot be verified before execution."
                + qualifier,
                location,
                active=active,
            )
        if not literal or self.db is None:
            return
        if self._image_lookup_error is not None:
            self.add(
                "MISSING_IMAGE",
                self._lookup_failure_severity(),
                f"Could not verify images: {self._image_lookup_error}",
                location,
                active=active,
            )
            return
        try:
            if self._images is None:
                self._images = _resource_names(self.db.list_images())
        except Exception as exc:
            self._image_lookup_error = str(exc)
            self.add(
                "MISSING_IMAGE",
                self._lookup_failure_severity(),
                f"Could not verify images: {exc}",
                location,
                active=active,
            )
            return
        any_resolvable = bool(dynamic) or any(
            name in self._images for name in literal
        )
        for name in literal:
            if name not in self._images:
                self.add(
                    "MISSING_IMAGE",
                    (
                        PreflightSeverity.WARNING
                        if any_resolvable
                        else PreflightSeverity.ERROR
                    ),
                    f"Image '{name}' does not exist.",
                    location,
                    active=active,
                )

    def _validate_visual_rule(
        self,
        value: str,
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        if not value:
            return
        if _has_malformed_template(value):
            return
        if _is_dynamic(value):
            self.add(
                "INVALID_PARAMETER_VALUE",
                PreflightSeverity.ERROR,
                "Dynamic visual-rule references are not supported.",
                location,
                active=active,
            )
            return
        if self.db is None:
            return
        if self._visual_rule_lookup_error is not None:
            self.add(
                "MISSING_VISUAL_RULE",
                self._lookup_failure_severity(),
                (
                    "Could not verify visual rules: "
                    f"{self._visual_rule_lookup_error}"
                ),
                location,
                active=active,
            )
            return
        try:
            if self._visual_rules is None:
                self._visual_rules = _resource_names(self.db.list_visual_rules())
        except Exception as exc:
            self._visual_rule_lookup_error = str(exc)
            self.add(
                "MISSING_VISUAL_RULE",
                self._lookup_failure_severity(),
                f"Could not verify visual rules: {exc}",
                location,
                active=active,
            )
            return
        if value not in self._visual_rules:
            self.add(
                "MISSING_VISUAL_RULE",
                PreflightSeverity.ERROR,
                f"Visual rule '{value}' does not exist.",
                location,
                active=active,
            )

    def _validate_cross_fields(
        self,
        command_id: str,
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        if (
            command_id == "send_notification"
            and params.get("channel", "pushover") == "pushover"
            and params.get("priority") == "emergency"
        ):
            retry_seconds = params.get("retry_seconds", 60)
            expire_seconds = params.get("expire_seconds", 1800)
            if (
                isinstance(retry_seconds, int)
                and not isinstance(retry_seconds, bool)
                and isinstance(expire_seconds, int)
                and not isinstance(expire_seconds, bool)
                and retry_seconds > expire_seconds
            ):
                self.add(
                    "INVALID_PARAMETER_VALUE",
                    PreflightSeverity.ERROR,
                    "'retry_seconds' must not exceed 'expire_seconds'.",
                    PreflightLocation(
                        **{**location.__dict__, "parameter": "retry_seconds"}
                    ),
                    active=active,
                )

        if command_id == "type_text" and self.target is TargetKind.ANDROID:
            delay_names = ("min_delay", "max_delay", "minDelay", "maxDelay", "delay_ms")
            if any(
                isinstance(params.get(name), (int, float))
                and not isinstance(params.get(name), bool)
                and params.get(name, 0) > 0
                for name in delay_names
            ):
                self.add(
                    "PARAMETER_NOT_APPLICABLE",
                    PreflightSeverity.WARNING,
                    (
                        "Android sends this text in one ADB command, so "
                        "character delays are ignored."
                    ),
                    PreflightLocation(
                        **{**location.__dict__, "parameter": "min_delay"}
                    ),
                    remediation=(
                        "Remove the character delay, or use a browser/Windows target."
                    ),
                    active=active,
                )

        for minimum_name, maximum_name in _DELAY_PAIRS:
            minimum = params.get(minimum_name)
            maximum = params.get(maximum_name)
            if (
                isinstance(minimum, (int, float))
                and not isinstance(minimum, bool)
                and isinstance(maximum, (int, float))
                and not isinstance(maximum, bool)
                and minimum > maximum
            ):
                self.add(
                    "INVALID_PARAMETER_VALUE",
                    PreflightSeverity.ERROR,
                    f"'{minimum_name}' must not exceed '{maximum_name}'.",
                    PreflightLocation(**{**location.__dict__, "parameter": minimum_name}),
                    active=active,
                )

        for name, value in params.items():
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (
                    name.endswith(("_ms", "_delay", "Delay"))
                    or name in {"timeout", "min_wait_time"}
                )
                and value < 0
            ):
                self.add(
                    "INVALID_PARAMETER_VALUE",
                    PreflightSeverity.ERROR,
                    f"'{name}' cannot be negative.",
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )

        for name in ("threshold", "color_tolerance"):
            value = params.get(name)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and not 0 <= value <= 1
            ):
                self.add(
                    "INVALID_PARAMETER_VALUE",
                    PreflightSeverity.ERROR,
                    f"'{name}' must be between 0 and 1.",
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )

        for selector in _SELECTOR_PARAMS:
            if (
                command_id == "clear_text_field"
                and self.target is TargetKind.ANDROID
            ):
                continue
            if (
                selector in params
                and isinstance(params[selector], str)
                and not params[selector].strip()
            ):
                selector_location = PreflightLocation(
                    **{**location.__dict__, "parameter": selector}
                )
                if any(
                    issue.code == "MISSING_PARAMETER"
                    and issue.location == selector_location
                    for issue in self.issues
                ):
                    continue
                self.add(
                    "EMPTY_SELECTOR",
                    (
                        PreflightSeverity.WARNING
                        if command_id == "clear_text_field" and self.target is None
                        else PreflightSeverity.ERROR
                    ),
                    (
                        "Selector is required for browser execution; Android clears "
                        "the focused field without one."
                        if command_id == "clear_text_field" and self.target is None
                        else "Selector cannot be empty."
                    ),
                    selector_location,
                    active=active,
                )
            elif (
                selector in params
                and isinstance(params[selector], str)
                and _is_dynamic(params[selector])
                and not _has_malformed_template(params[selector])
                and command_id not in _LOCATOR_COMMANDS
            ):
                self.add(
                    "DYNAMIC_REFERENCE",
                    PreflightSeverity.WARNING,
                    "Dynamic selector cannot be verified before execution.",
                    PreflightLocation(**{**location.__dict__, "parameter": selector}),
                    active=active,
                )

        if command_id in {"navigate_to", "open_new_tab"}:
            url = params.get("url", "")
            required = command_id == "navigate_to"
            structured_url = bool(
                isinstance(url, Mapping)
                and _get(self._flow_stack[-1], "value_model_version", 0)
                == CURRENT_FLOW_VALUE_MODEL_VERSION
            )
            if required and not structured_url and (
                not isinstance(url, str) or not url.strip()
            ):
                # The generic required check normally reports this; keep this
                # command-specific code useful for legacy metadata/defaults.
                if not any(
                    i.location == PreflightLocation(
                        location.flow_name,
                        location.command_path,
                        location.step_id,
                        location.command_id,
                        "url",
                    )
                    and i.code == "MISSING_PARAMETER"
                    for i in self.issues
                ):
                    self.add(
                        "INVALID_URL",
                        PreflightSeverity.ERROR,
                        "Navigation URL cannot be empty.",
                        PreflightLocation(**{**location.__dict__, "parameter": "url"}),
                        active=active,
                    )
            elif isinstance(url, str) and url.strip():
                self._validate_url(url, location, active=active)

        if command_id == "screenshot":
            self._validate_screenshot(params, location, active=active)

        if command_id == "read_screen_text":
            self._validate_read_screen_text(params, location, active=active)

        if command_id == "run_flow":
            self._validate_flow_reference(params, location, active=active)

    def _validate_url(
        self,
        url: str,
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        url_location = PreflightLocation(**{**location.__dict__, "parameter": "url"})
        if _has_malformed_template(url):
            return
        if _is_dynamic(url):
            self.add(
                "DYNAMIC_REFERENCE",
                PreflightSeverity.WARNING,
                "Dynamic URL cannot be fully verified before execution.",
                url_location,
                active=active,
            )
            return
        text = url.strip()
        parsed = urlparse(text)
        valid = parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)
        if location.command_id == "navigate_to" and not parsed.scheme:
            with_default_scheme = urlparse(f"https://{text}")
            valid = bool(with_default_scheme.netloc) and not any(
                character.isspace() for character in text
            )
        if not valid:
            self.add(
                "INVALID_URL",
                PreflightSeverity.ERROR,
                "URL must be an absolute http:// or https:// URL.",
                url_location,
                active=active,
            )

    def _validate_screenshot(
        self,
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        if params.get("save_to_disk") is not True:
            return
        raw_path = params.get("save_path", "")
        path_location = PreflightLocation(**{**location.__dict__, "parameter": "save_path"})
        if not isinstance(raw_path, str) or not raw_path.strip():
            self.add(
                "INVALID_OUTPUT_PATH",
                PreflightSeverity.ERROR,
                "A screenshot output directory is required.",
                path_location,
                active=active,
            )
            return
        if _is_dynamic(raw_path):
            self.add(
                "INVALID_OUTPUT_PATH",
                PreflightSeverity.ERROR,
                "Dynamic screenshot directories are not supported.",
                path_location,
                active=active,
            )
            return
        output = Path(raw_path).expanduser()
        if not output.is_dir():
            self.add(
                "INVALID_OUTPUT_PATH",
                PreflightSeverity.ERROR,
                f"Screenshot directory does not exist: {raw_path}",
                path_location,
                active=active,
            )
        elif not os.access(output, os.W_OK):
            self.add(
                "UNWRITABLE_OUTPUT_PATH",
                (
                    PreflightSeverity.ERROR
                    if self.mode is PreflightMode.EXECUTION
                    else PreflightSeverity.WARNING
                ),
                f"Screenshot directory is not writable: {raw_path}",
                path_location,
                active=active,
            )

    def _validate_read_screen_text(
        self,
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        region_mode = str(params.get("region_mode", "fixed")).casefold()
        if region_mode not in {"fixed", "image_anchor"}:
            self.add(
                "INVALID_PARAMETER_VALUE",
                PreflightSeverity.ERROR,
                "'region_mode' must be fixed or image_anchor.",
                PreflightLocation(**{**location.__dict__, "parameter": "region_mode"}),
                active=active,
            )
            return
        unit = str(params.get("coordinate_unit", "pixels")).casefold()
        if unit not in {"pixels", "percent"}:
            self.add(
                "INVALID_PARAMETER_VALUE",
                PreflightSeverity.ERROR,
                "'coordinate_unit' must be pixels or percent.",
                PreflightLocation(**{**location.__dict__, "parameter": "coordinate_unit"}),
                active=active,
            )
            return

        coordinate_names = (
            ("start_x", "start_y", "end_x", "end_y")
            if region_mode == "fixed"
            else ("offset_x", "offset_y", "region_width", "region_height")
        )
        required_names = (
            coordinate_names
            if region_mode == "fixed"
            else ("images", "region_width", "region_height")
        )
        for name in required_names:
            value = params.get(name)
            if value is None or value == "" or value == []:
                self.add(
                    "MISSING_PARAMETER",
                    PreflightSeverity.ERROR,
                    f"Required parameter '{name}' is missing.",
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )

        coordinates = {name: params.get(name) for name in coordinate_names}
        literal_coordinates: dict[str, float] = {}
        for name, value in coordinates.items():
            if isinstance(value, Mapping):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = float(value)
            if not math.isfinite(number):
                self.add(
                    "INVALID_PARAMETER_VALUE",
                    PreflightSeverity.ERROR,
                    f"'{name}' must be finite.",
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )
                continue
            lower_bound = (
                -100
                if region_mode == "image_anchor" and name.startswith("offset_")
                else 0
            )
            if unit == "percent" and not lower_bound <= number <= 100:
                self.add(
                    "INVALID_PARAMETER_VALUE",
                    PreflightSeverity.ERROR,
                    f"'{name}' must be between {lower_bound} and 100 for "
                    "percentage coordinates.",
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )
            literal_coordinates[name] = number

        if region_mode == "fixed" and len(literal_coordinates) == 4 and (
            literal_coordinates["start_x"] >= literal_coordinates["end_x"]
            or literal_coordinates["start_y"] >= literal_coordinates["end_y"]
        ):
            self.add(
                "INVALID_PARAMETER_VALUE",
                PreflightSeverity.ERROR,
                "Screen-text start coordinates must be before end coordinates.",
                PreflightLocation(**{**location.__dict__, "parameter": "start_x"}),
                active=active,
            )
        if region_mode == "image_anchor":
            for name in ("region_width", "region_height"):
                value = literal_coordinates.get(name)
                if value is not None and value <= 0:
                    self.add(
                        "INVALID_PARAMETER_VALUE",
                        PreflightSeverity.ERROR,
                        f"'{name}' must be positive.",
                        PreflightLocation(**{**location.__dict__, "parameter": name}),
                        active=active,
                    )

        result_type = params.get("result_type", "text")
        try:
            expected_type = ValueType(str(result_type).casefold())
        except ValueError:
            expected_type = None
        target_id = params.get("variable")
        catalog = self._flow_value_catalog(self._flow_stack[-1])
        target = catalog.variable_by_id(target_id) if catalog and isinstance(target_id, str) else None
        if expected_type is not None and target is not None and target.type is not expected_type:
            self.add(
                "OUTPUT_VARIABLE_TYPE_MISMATCH",
                PreflightSeverity.ERROR,
                f"Read Screen Text result type is {expected_type.value}, but target "
                f"Variable '{target.name}' is {target.type.value}.",
                PreflightLocation(**{**location.__dict__, "parameter": "variable"}),
                remediation="Choose a Variable matching the selected result type.",
                active=active,
            )

    @staticmethod
    def _value_type_assignable(source: ValueType, target: ValueType) -> bool:
        return is_value_type_assignable(source, target)

    @staticmethod
    def _flow_value_catalog(flow: object) -> FlowValueCatalog | None:
        catalog = _get(flow, "value_catalog", None)
        if isinstance(catalog, FlowValueCatalog):
            return catalog
        if _get(flow, "value_model_version", 0) != CURRENT_FLOW_VALUE_MODEL_VERSION:
            return None
        try:
            return FlowValueCatalog.build(
                _get(flow, "inputs", []),
                _get(flow, "variables", []),
                _get(flow, "outputs", []),
            )
        except (FlowValueDeclarationError, TypeError, ValueError):
            return None

    def _validate_variable_update(
        self,
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
        builtin_types: Mapping[str, ValueType],
    ) -> None:
        current_flow = self._flow_stack[-1]
        if (
            _get(current_flow, "value_model_version", 0)
            != CURRENT_FLOW_VALUE_MODEL_VERSION
        ):
            self.add(
                "INVALID_VARIABLE_UPDATE",
                PreflightSeverity.ERROR,
                "Update Variable is available only in version-1 flows.",
                location,
                active=active,
            )
            return
        for name in sorted(_GLOBAL_PARAM_NAMES.intersection(params)):
            value = params[name]
            if not isinstance(value, int) or isinstance(value, bool):
                self.add(
                    "INVALID_PARAMETER_TYPE",
                    PreflightSeverity.ERROR,
                    f"'{name}' must be int.",
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )
        contract_params = {
            name: value
            for name, value in params.items()
            if name not in _GLOBAL_PARAM_NAMES | {"log_values"}
        }
        try:
            update = VariableUpdate.from_params(contract_params)
        except VariableUpdateError as exc:
            self.add(
                "INVALID_VARIABLE_UPDATE",
                PreflightSeverity.ERROR,
                str(exc),
                location,
                active=active,
            )
            return
        catalog = self._flow_value_catalog(current_flow)
        if catalog is None:
            self.add(
                "INVALID_VARIABLE_UPDATE",
                PreflightSeverity.ERROR,
                "Update Variable declarations could not be validated.",
                location,
                active=active,
            )
            return
        target = catalog.variable_by_id(update.target_variable_id)
        if target is None:
            self.add(
                "STALE_VARIABLE_UPDATE_TARGET",
                PreflightSeverity.ERROR,
                f"Target Variable '{update.target_variable_id}' does not exist.",
                PreflightLocation(
                    **{**location.__dict__, "parameter": "target_variable_id"}
                ),
                active=active,
            )
            return
        if update.operation not in variable_update_allowed_operations(target.type):
            self.add(
                "INVALID_VARIABLE_UPDATE_OPERATION",
                PreflightSeverity.ERROR,
                f"Operation '{update.operation.value}' is not valid for "
                f"{target.type.value} Variable '{target.name}'.",
                PreflightLocation(
                    **{**location.__dict__, "parameter": "operation"}
                ),
                active=active,
            )
            return

        operands = (
            (("find", update.find), ("replacement", update.replacement))
            if update.operation is VariableUpdateOperation.REPLACE
            else (("source", update.source),)
            if update.source is not None
            else ()
        )
        resolved_types: dict[str, ValueType] = {}
        for name, reference in operands:
            assert reference is not None
            try:
                resolved_types[name] = resolve_value_reference_type(
                    reference,
                    catalog,
                    builtin_types=builtin_types,
                )
            except ValueReferenceError as exc:
                self.add(
                    "INVALID_VARIABLE_UPDATE_SOURCE",
                    PreflightSeverity.ERROR,
                    str(exc),
                    PreflightLocation(
                        **{**location.__dict__, "parameter": name}
                    ),
                    active=active,
                )
        if len(resolved_types) != len(operands):
            return
        if update.operation is VariableUpdateOperation.REPLACE:
            for name in ("find", "replacement"):
                if resolved_types[name] is not ValueType.TEXT:
                    self.add(
                        "VARIABLE_UPDATE_TYPE_MISMATCH",
                        PreflightSeverity.ERROR,
                        "Text Replace requires text find and replacement values.",
                        PreflightLocation(
                            **{**location.__dict__, "parameter": name}
                        ),
                        active=active,
                    )
            if (
                isinstance(update.find, LiteralValueReference)
                and update.find.value == ""
            ):
                self.add(
                    "INVALID_VARIABLE_UPDATE",
                    PreflightSeverity.ERROR,
                    "Text Replace find value cannot be empty.",
                    PreflightLocation(
                        **{**location.__dict__, "parameter": "find"}
                    ),
                    active=active,
                )
            return
        if update.source is None:
            return
        source_type = resolved_types["source"]
        if source_type not in variable_update_allowed_source_types(
            target.type,
            update.operation,
        ):
            self.add(
                "VARIABLE_UPDATE_TYPE_MISMATCH",
                PreflightSeverity.ERROR,
                f"Operation '{update.operation.value}' on Variable '{target.name}' "
                f"cannot use a {source_type.value} source.",
                PreflightLocation(
                    **{**location.__dict__, "parameter": "source"}
                ),
                active=active,
            )
        if (
            update.operation is VariableUpdateOperation.DIVIDE
            and isinstance(update.source, LiteralValueReference)
            and update.source.value == 0
        ):
            self.add(
                "INVALID_VARIABLE_UPDATE",
                PreflightSeverity.ERROR,
                "Update Variable cannot divide by zero.",
                PreflightLocation(
                    **{**location.__dict__, "parameter": "source"}
                ),
                active=active,
            )

    def _validate_value_condition(
        self,
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
        builtin_types: Mapping[str, ValueType],
        action_label: str,
        extra_params: frozenset[str],
    ) -> None:
        current_flow = self._flow_stack[-1]
        if (
            _get(current_flow, "value_model_version", 0)
            != CURRENT_FLOW_VALUE_MODEL_VERSION
        ):
            self.add(
                "INVALID_VALUE_CONDITION",
                PreflightSeverity.ERROR,
                f"{action_label} is available only in version-1 flows.",
                location,
                active=active,
            )
            return
        for name in sorted(_GLOBAL_PARAM_NAMES.intersection(params)):
            value = params[name]
            if not isinstance(value, int) or isinstance(value, bool):
                self.add(
                    "INVALID_PARAMETER_TYPE",
                    PreflightSeverity.ERROR,
                    f"'{name}' must be int.",
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )
        if "timeout" in extra_params and "timeout" in params:
            timeout = params["timeout"]
            timeout_location = PreflightLocation(
                **{**location.__dict__, "parameter": "timeout"}
            )
            if isinstance(timeout, Mapping):
                self._validate_v1_value_reference(
                    timeout,
                    {"allowed_value_types": [ValueType.DURATION_MS.value]},
                    timeout_location,
                    active=active,
                    builtin_types=builtin_types,
                )
            elif not isinstance(timeout, int) or isinstance(timeout, bool):
                self.add(
                    "INVALID_PARAMETER_TYPE",
                    PreflightSeverity.ERROR,
                    "'timeout' must be int.",
                    timeout_location,
                    active=active,
                )
        contract_params = {
            name: value
            for name, value in params.items()
            if name not in _GLOBAL_PARAM_NAMES | extra_params
        }
        try:
            condition = ValueCondition.from_params(contract_params)
        except ValueConditionError as exc:
            self.add(
                "INVALID_VALUE_CONDITION",
                PreflightSeverity.ERROR,
                str(exc),
                location,
                active=active,
            )
            return
        catalog = self._flow_value_catalog(current_flow)
        if catalog is None:
            self.add(
                "INVALID_VALUE_CONDITION",
                PreflightSeverity.ERROR,
                f"{action_label} declarations could not be validated.",
                location,
                active=active,
            )
            return
        resolved_types: dict[str, ValueType] = {}
        for name, reference in (("left", condition.left), ("right", condition.right)):
            if reference is None:
                continue
            try:
                resolved_types[name] = resolve_value_reference_type(
                    reference,
                    catalog,
                    builtin_types=builtin_types,
                )
            except ValueReferenceError as exc:
                self.add(
                    "INVALID_VALUE_CONDITION_REFERENCE",
                    PreflightSeverity.ERROR,
                    str(exc),
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )
        if "left" not in resolved_types or (
            condition.right is not None and "right" not in resolved_types
        ):
            return
        try:
            condition.validate_types(
                resolved_types["left"],
                resolved_types.get("right"),
            )
        except ValueConditionError as exc:
            self.add(
                "VALUE_CONDITION_TYPE_MISMATCH",
                PreflightSeverity.ERROR,
                str(exc),
                PreflightLocation(**{**location.__dict__, "parameter": "operator"}),
                active=active,
            )

    def _validate_v1_flow_reference(
        self,
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
        builtin_types: Mapping[str, ValueType],
    ) -> None:
        for name in sorted(_GLOBAL_PARAM_NAMES.intersection(params)):
            value = params[name]
            if not isinstance(value, int) or isinstance(value, bool):
                self.add(
                    "INVALID_PARAMETER_TYPE",
                    PreflightSeverity.ERROR,
                    f"'{name}' must be int.",
                    PreflightLocation(**{**location.__dict__, "parameter": name}),
                    active=active,
                )
        binding_params = {
            name: value
            for name, value in params.items()
            if name not in _GLOBAL_PARAM_NAMES
        }
        try:
            binding = RunFlowBinding.from_params(binding_params)
        except RunFlowBindingError as exc:
            self.add(
                "INVALID_RUN_FLOW_BINDING",
                PreflightSeverity.ERROR,
                str(exc),
                PreflightLocation(**{**location.__dict__, "parameter": "flow_id"}),
                active=active,
            )
            return
        if not active:
            return
        if self.db is None:
            if self.mode is PreflightMode.EXECUTION:
                self.add(
                    "MISSING_FLOW",
                    PreflightSeverity.ERROR,
                    "Child Flow bindings require a database during execution preflight.",
                    PreflightLocation(
                        **{**location.__dict__, "parameter": "flow_id"}
                    ),
                    active=active,
                )
            return
        if binding.flow_id in self._flow_id_stack:
            cycle = " -> ".join((*self._stack, binding.flow_id))
            self.add(
                "RECURSIVE_FLOW_CALL",
                PreflightSeverity.ERROR,
                f"Reachable flow cycle: {cycle}.",
                PreflightLocation(**{**location.__dict__, "parameter": "flow_id"}),
                remediation="Remove one Run Flow edge from the cycle.",
                active=active,
            )
            return
        try:
            called_flow = self.db.load_flow_by_id(binding.flow_id)
        except Exception as exc:
            self.add(
                "MISSING_FLOW",
                self._lookup_failure_severity(),
                f"Could not load child Flow '{binding.flow_id}': {exc}",
                PreflightLocation(**{**location.__dict__, "parameter": "flow_id"}),
                active=active,
            )
            return
        if called_flow is None:
            self.add(
                "MISSING_FLOW",
                PreflightSeverity.ERROR,
                f"Child Flow '{binding.flow_id}' does not exist.",
                PreflightLocation(**{**location.__dict__, "parameter": "flow_id"}),
                active=active,
            )
            return
        if (
            _get(called_flow, "value_model_version", 0)
            != CURRENT_FLOW_VALUE_MODEL_VERSION
        ):
            self.add(
                "INVALID_RUN_FLOW_TARGET",
                PreflightSeverity.ERROR,
                "Version-1 Run Flow can only call a version-1 child Flow.",
                PreflightLocation(**{**location.__dict__, "parameter": "flow_id"}),
                active=active,
            )
            return

        parent_catalog = self._flow_value_catalog(self._flow_stack[-1])
        child_catalog = self._flow_value_catalog(called_flow)
        if parent_catalog is None or child_catalog is None:
            self.add(
                "INVALID_RUN_FLOW_BINDING",
                PreflightSeverity.ERROR,
                "Run Flow declarations could not be validated.",
                location,
                active=active,
            )
            return

        child_inputs = {definition.id: definition for definition in child_catalog.inputs}
        for input_id, reference in binding.input_mappings.items():
            child_input = child_inputs.get(input_id)
            mapping_location = PreflightLocation(
                **{**location.__dict__, "parameter": "input_mappings"}
            )
            if child_input is None:
                self.add(
                    "STALE_CHILD_INPUT_MAPPING",
                    PreflightSeverity.ERROR,
                    f"Mapped child Input '{input_id}' does not exist.",
                    mapping_location,
                    active=active,
                )
                continue
            try:
                source_type = resolve_value_reference_type(
                    reference,
                    parent_catalog,
                    builtin_types=builtin_types,
                )
            except ValueReferenceError as exc:
                self.add(
                    "INVALID_RUN_FLOW_MAPPING",
                    PreflightSeverity.ERROR,
                    str(exc),
                    mapping_location,
                    active=active,
                )
                continue
            if not self._value_type_assignable(source_type, child_input.type):
                self.add(
                    "RUN_FLOW_TYPE_MISMATCH",
                    PreflightSeverity.ERROR,
                    f"Child Input '{child_input.name}' requires "
                    f"{child_input.type.value}, but the mapping provides "
                    f"{source_type.value}.",
                    mapping_location,
                    active=active,
                )
        for child_input in child_catalog.inputs:
            if child_input.required and child_input.id not in binding.input_mappings:
                self.add(
                    "MISSING_CHILD_INPUT_MAPPING",
                    PreflightSeverity.ERROR,
                    f"Required child Input '{child_input.name}' is not mapped.",
                    PreflightLocation(
                        **{**location.__dict__, "parameter": "input_mappings"}
                    ),
                    active=active,
                )

        child_outputs = {
            definition.id: definition for definition in child_catalog.outputs
        }
        for output_id, parent_variable_id in binding.output_mappings.items():
            mapping_location = PreflightLocation(
                **{**location.__dict__, "parameter": "output_mappings"}
            )
            child_output = child_outputs.get(output_id)
            if child_output is None:
                self.add(
                    "STALE_CHILD_OUTPUT_MAPPING",
                    PreflightSeverity.ERROR,
                    f"Mapped child Output '{output_id}' does not exist.",
                    mapping_location,
                    active=active,
                )
                continue
            parent_variable = parent_catalog.variable_by_id(parent_variable_id)
            if parent_variable is None:
                self.add(
                    "STALE_PARENT_VARIABLE_MAPPING",
                    PreflightSeverity.ERROR,
                    f"Mapped parent Variable '{parent_variable_id}' does not exist.",
                    mapping_location,
                    active=active,
                )
                continue
            if not self._value_type_assignable(
                child_output.type,
                parent_variable.type,
            ):
                self.add(
                    "RUN_FLOW_TYPE_MISMATCH",
                    PreflightSeverity.ERROR,
                    f"Child Output '{child_output.name}' provides "
                    f"{child_output.type.value}, but parent Variable "
                    f"'{parent_variable.name}' requires {parent_variable.type.value}.",
                    mapping_location,
                    active=active,
                )

        child_name = _get(called_flow, "name", binding.flow_id)
        if self.include_called_flows and child_name not in self._visited_set:
            self.validate_flow(called_flow, child_name)

    def _validate_flow_reference(
        self,
        params: Mapping[str, Any],
        location: PreflightLocation,
        *,
        active: bool,
    ) -> None:
        value = params.get("flow_name", "")
        ref_location = PreflightLocation(**{**location.__dict__, "parameter": "flow_name"})
        if not isinstance(value, str) or not value.strip():
            return
        if _has_malformed_template(value):
            return
        if _is_dynamic(value):
            self.add(
                "DYNAMIC_REFERENCE",
                PreflightSeverity.WARNING,
                "Dynamic flow references cannot be verified before execution.",
                ref_location,
                active=active,
            )
            return
        if not active:
            return
        if value in self._stack:
            cycle = " -> ".join((*self._stack, value))
            self.add(
                "RECURSIVE_FLOW_CALL",
                PreflightSeverity.ERROR,
                f"Reachable flow cycle: {cycle}.",
                ref_location,
                remediation="Remove one literal run_flow edge from the cycle.",
                active=active,
            )
            return
        if self.db is None:
            return
        if self._flow_lookup_error is not None:
            self.add(
                "MISSING_FLOW",
                self._lookup_failure_severity(),
                f"Could not verify flows: {self._flow_lookup_error}",
                ref_location,
                active=active,
            )
            return
        try:
            called_flow = self.db.load_flow(value)
        except Exception as exc:
            self._flow_lookup_error = str(exc)
            self.add(
                "MISSING_FLOW",
                self._lookup_failure_severity(),
                f"Could not load flow '{value}': {exc}",
                ref_location,
                active=active,
            )
            return
        if called_flow is None:
            self.add(
                "MISSING_FLOW",
                PreflightSeverity.ERROR,
                f"Flow '{value}' does not exist.",
                ref_location,
                active=active,
            )
            return
        if self.include_called_flows and value not in self._visited_set:
            self.validate_flow(called_flow, value)

    def report(self) -> PreflightReport:
        flow_order = {name: index for index, name in enumerate(self.visited)}
        severity_order = {
            PreflightSeverity.ERROR: 0,
            PreflightSeverity.WARNING: 1,
            PreflightSeverity.INFO: 2,
        }

        def path_key(path: CommandPath) -> tuple[tuple[int, str], ...]:
            return tuple(
                (0, f"{part:012d}") if isinstance(part, int) else (1, part)
                for part in path
            )

        ordered = sorted(
            self.issues,
            key=lambda issue: (
                flow_order.get(issue.location.flow_name, len(flow_order)),
                path_key(issue.location.command_path),
                issue.location.parameter,
                severity_order[issue.severity],
                issue.code,
                issue.message,
            ),
        )
        return PreflightReport(
            tuple(ordered),
            tuple(self.visited),
            self.requires_interactive_desktop,
        )


def preflight_flow(
    flow: object,
    *,
    db: object | None = None,
    root_name: str | None = None,
    target: object | None = None,
    mode: PreflightMode | str = PreflightMode.DOCUMENT,
    include_called_flows: bool = True,
) -> PreflightReport:
    """Validate a flow and, optionally, all reachable literal subflows."""
    try:
        normalized_mode = mode if isinstance(mode, PreflightMode) else PreflightMode(mode)
    except ValueError as exc:
        raise ValueError(f"Unsupported preflight mode: {mode!r}") from exc
    normalized_target = target_kind_for_device(target) if target is not None else None
    flow_name = root_name if root_name is not None else _get(flow, "name", "")
    if not isinstance(flow_name, str) or not flow_name.strip():
        flow_name = "<unnamed>"

    validator = _Validator(
        db=db,
        target=normalized_target,
        mode=normalized_mode,
        include_called_flows=include_called_flows,
    )
    validator.validate_flow(flow, flow_name)
    return validator.report()


__all__ = [
    "FlowPreflightError",
    "PreflightIssue",
    "PreflightLocation",
    "PreflightMode",
    "PreflightReport",
    "PreflightSeverity",
    "TargetKind",
    "preflight_flow",
    "supported_targets",
    "target_kind_for_device",
]
