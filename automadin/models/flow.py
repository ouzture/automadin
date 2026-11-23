from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automadin.models.hook import AutomationHook, HookBinding

from automadin.models.flow_parameter import (
    FlowParameterDefinition,
    validate_parameter_definitions,
)
from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowOutputDefinition,
    FlowValueCatalog,
    FlowValueDeclarationError,
    FlowVariableDefinition,
)
from automadin.models.resolution_scaling import ResolutionScalingSettings

LEGACY_FLOW_VALUE_MODEL_VERSION = 0
CURRENT_FLOW_VALUE_MODEL_VERSION = 1


def _new_flow_id() -> str:
    return str(uuid.uuid4())


def _normalize_flow_id(value: object) -> str:
    if not isinstance(value, str):
        raise FlowValueDeclarationError("Flow ID must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise FlowValueDeclarationError("Flow ID must be a UUID string.") from exc


@dataclass
class FlowCommand:
    id: str
    step_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    params: dict[str, Any] = field(default_factory=dict)
    children: list[FlowCommand] = field(default_factory=list)
    else_children: list[FlowCommand] = field(default_factory=list)
    comment: str = ""
    breakpoint: bool = False
    enabled: bool = True
    on_error: str = "stop"
    retry_count: int = 0
    min_wait_time: int = 0
    collapsed: bool = True
    hook_bindings: list[HookBinding] = field(default_factory=list)

    @property
    def short_id(self) -> str:
        """First 4 hex chars of step_id for human-readable log/UI reference."""
        return self.step_id[:4]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "step_id": self.step_id,
            "params": self.params,
            "children": [c.to_dict() for c in self.children],
            "else_children": [c.to_dict() for c in self.else_children],
            "comment": self.comment,
            "breakpoint": self.breakpoint,
            "enabled": self.enabled,
            "on_error": self.on_error,
            "retry_count": self.retry_count,
            "min_wait_time": self.min_wait_time,
            "collapsed": self.collapsed,
            "hook_bindings": [binding.to_dict() for binding in self.hook_bindings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlowCommand:
        from automadin.models.hook import HookBinding

        if not isinstance(data, dict):
            raise ValueError("Flow command must be an object")
        command_id = data.get("id")
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("Flow command requires a non-empty string id")
        params = data.get("params", {})
        children = data.get("children", [])
        else_children = data.get("else_children", [])
        if not isinstance(params, dict):
            raise ValueError(f"Command '{command_id}' params must be an object")
        if not isinstance(children, list) or not isinstance(else_children, list):
            raise ValueError(f"Command '{command_id}' children must be arrays")
        hook_bindings = data.get("hook_bindings", [])
        if not isinstance(hook_bindings, list):
            raise ValueError(f"Command '{command_id}' hook_bindings must be an array")
        return cls(
            id=command_id,
            step_id=data.get("step_id", str(uuid.uuid4())),
            params=params,
            children=[cls.from_dict(c) for c in children],
            else_children=[cls.from_dict(c) for c in else_children],
            comment=data.get("comment", ""),
            breakpoint=data.get("breakpoint", False),
            enabled=data.get("enabled", True),
            on_error=data.get("on_error", "stop"),
            retry_count=data.get("retry_count", 0),
            min_wait_time=data.get("min_wait_time", 0),
            collapsed=data.get("collapsed", command_id != "switch_case"),
            hook_bindings=[HookBinding.from_dict(item) for item in hook_bindings],
        )

    def deep_copy(self) -> FlowCommand:
        return FlowCommand(
            id=self.id,
            step_id=str(uuid.uuid4()),
            params=copy.deepcopy(self.params),
            children=[c.deep_copy() for c in self.children],
            else_children=[c.deep_copy() for c in self.else_children],
            comment=self.comment,
            breakpoint=self.breakpoint,
            enabled=self.enabled,
            on_error=self.on_error,
            retry_count=self.retry_count,
            min_wait_time=self.min_wait_time,
            collapsed=self.collapsed,
            hook_bindings=copy.deepcopy(self.hook_bindings),
        )


@dataclass
class AutomationFlow:
    name: str
    commands: list[FlowCommand] = field(default_factory=list)
    parameters: list[FlowParameterDefinition] = field(default_factory=list)
    flow_id: str = field(default_factory=_new_flow_id)
    value_model_version: int = LEGACY_FLOW_VALUE_MODEL_VERSION
    inputs: list[FlowInputDefinition] = field(default_factory=list)
    variables: list[FlowVariableDefinition] = field(default_factory=list)
    outputs: list[FlowOutputDefinition] = field(default_factory=list)
    resolution_scaling: ResolutionScalingSettings = field(
        default_factory=ResolutionScalingSettings
    )
    hooks: list[AutomationHook] = field(default_factory=list)

    def __post_init__(self) -> None:
        from automadin.models.hook import AutomationHook

        self.flow_id = _normalize_flow_id(self.flow_id)
        if not isinstance(self.resolution_scaling, ResolutionScalingSettings):
            raise ValueError("Flow resolution scaling settings are invalid.")
        if type(self.value_model_version) is not int or self.value_model_version not in {
            LEGACY_FLOW_VALUE_MODEL_VERSION,
            CURRENT_FLOW_VALUE_MODEL_VERSION,
        }:
            raise FlowValueDeclarationError(
                "Flow value model version must be 0 (legacy) or 1."
            )
        self.parameters = list(validate_parameter_definitions(self.parameters))
        catalog = FlowValueCatalog.build(self.inputs, self.variables, self.outputs)
        self.inputs = list(catalog.inputs)
        self.variables = list(catalog.variables)
        self.outputs = list(catalog.outputs)
        if self.value_model_version == LEGACY_FLOW_VALUE_MODEL_VERSION:
            if self.inputs or self.variables or self.outputs:
                raise FlowValueDeclarationError(
                    "Legacy value-model flows cannot declare Inputs, Variables, or "
                    "Outputs."
                )
        elif self.parameters:
            raise FlowValueDeclarationError(
                "Value-model version 1 flows cannot declare legacy parameters."
            )
        if not isinstance(self.hooks, list) or any(
            not isinstance(hook, AutomationHook) for hook in self.hooks
        ):
            raise ValueError("Flow hooks must be AutomationHook objects.")

    @property
    def value_catalog(self) -> FlowValueCatalog:
        return FlowValueCatalog.build(self.inputs, self.variables, self.outputs)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "flow_id": self.flow_id,
            "value_model_version": self.value_model_version,
            "commands": [c.to_dict() for c in self.commands],
            "resolution_scaling": self.resolution_scaling.to_dict(),
            "hooks": [hook.to_dict() for hook in self.hooks],
        }
        if self.value_model_version == LEGACY_FLOW_VALUE_MODEL_VERSION:
            data["parameters"] = [
                parameter.to_dict() for parameter in self.parameters
            ]
        else:
            data["inputs"] = [definition.to_dict() for definition in self.inputs]
            data["variables"] = [definition.to_dict() for definition in self.variables]
            data["outputs"] = [definition.to_dict() for definition in self.outputs]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutomationFlow:
        from automadin.models.hook import AutomationHook

        if not isinstance(data, dict):
            raise ValueError("Flow must be a JSON object")
        name = data.get("name")
        commands = data.get("commands", [])
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Flow requires a non-empty string name")
        if not isinstance(commands, list):
            raise ValueError("Flow commands must be an array")
        hooks = data.get("hooks", [])
        if not isinstance(hooks, list):
            raise ValueError("Flow hooks must be an array")
        value_model_version = data.get(
            "value_model_version",
            LEGACY_FLOW_VALUE_MODEL_VERSION,
        )
        if value_model_version == LEGACY_FLOW_VALUE_MODEL_VERSION:
            if any(key in data for key in ("inputs", "variables", "outputs")):
                raise FlowValueDeclarationError(
                    "Legacy value-model flow data cannot contain new declarations."
                )
            parameters = data.get("parameters", [])
            inputs: object = []
            variables: object = []
            outputs: object = []
        else:
            if "parameters" in data:
                raise FlowValueDeclarationError(
                    "Value-model version 1 flow data cannot contain legacy parameters."
                )
            missing = [
                key
                for key in ("inputs", "variables", "outputs")
                if key not in data
            ]
            if missing:
                raise FlowValueDeclarationError(
                    "Value-model version 1 flow data is missing: "
                    + ", ".join(missing)
                    + "."
                )
            parameters = []
            inputs = data["inputs"]
            variables = data["variables"]
            outputs = data["outputs"]
        normalized_parameters = validate_parameter_definitions(parameters)
        return cls(
            name=name,
            commands=[FlowCommand.from_dict(c) for c in commands],
            parameters=list(normalized_parameters),
            flow_id=data.get("flow_id", _new_flow_id()),  # type: ignore[arg-type]
            value_model_version=value_model_version,
            inputs=list(inputs) if isinstance(inputs, (list, tuple)) else inputs,  # type: ignore[arg-type]
            variables=(
                list(variables)
                if isinstance(variables, (list, tuple))
                else variables  # type: ignore[arg-type]
            ),
            outputs=(
                list(outputs)
                if isinstance(outputs, (list, tuple))
                else outputs  # type: ignore[arg-type]
            ),
            resolution_scaling=ResolutionScalingSettings.from_dict(
                data.get("resolution_scaling")
            ),
            hooks=[AutomationHook.from_dict(item) for item in hooks],
        )

    def deep_copy(self, *, name: str | None = None) -> AutomationFlow:
        """Copy a flow while rewriting all stable hook references."""
        from automadin.models.hook import AutomationHook, HookBinding

        hook_ids = {hook.hook_id: str(uuid.uuid4()) for hook in self.hooks}

        def copy_command(command: FlowCommand) -> FlowCommand:
            copied = command.deep_copy()
            copied.hook_bindings = [
                HookBinding(hook_ids.get(binding.hook_id, binding.hook_id), binding.phases)
                for binding in command.hook_bindings
            ]
            if copied.id == "check_hooks":
                values = copied.params.get("hooks", [])
                if isinstance(values, list):
                    copied.params["hooks"] = [hook_ids.get(item, item) for item in values]
            copied.children = [copy_command(item) for item in command.children]
            copied.else_children = [copy_command(item) for item in command.else_children]
            return copied

        data = self.to_dict()
        data["name"] = self.name if name is None else name
        data["flow_id"] = _new_flow_id()
        data["commands"] = [copy_command(command).to_dict() for command in self.commands]
        data["hooks"] = [
            AutomationHook.from_dict(
                {
                    **hook.to_dict(),
                    "hook_id": hook_ids[hook.hook_id],
                    "actions": [copy_command(command).to_dict() for command in hook.actions],
                }
            ).to_dict()
            for hook in self.hooks
        ]
        return AutomationFlow.from_dict(data)
