from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from automadin.models.flow import FlowCommand


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID string") from exc


class HookType(str, Enum):
    VISUAL_IMAGE = "visual_image"
    BROWSER_ELEMENT = "browser_element"


class HookPhase(str, Enum):
    BEFORE = "before"
    AFTER = "after"


class HookAutomaticScope(str, Enum):
    NONE = "none"
    SELECTED_COMMANDS = "selected_commands"
    ALL_COMPATIBLE_COMMANDS = "all_compatible_commands"


class HookRetriggerPolicy(str, Enum):
    REARM_AFTER_ABSENT = "rearm_after_absent"
    ONCE_PER_RUN = "once_per_run"


class HookMatch(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"


def _phases(value: object, *, label: str) -> tuple[HookPhase, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{label} must contain at least one phase")
    try:
        phases = tuple(HookPhase(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} contains an unsupported phase") from exc
    if len(set(phases)) != len(phases):
        raise ValueError(f"{label} cannot contain duplicate phases")
    return phases


@dataclass(frozen=True, slots=True)
class HookBinding:
    hook_id: str
    phases: tuple[HookPhase, ...] = (HookPhase.BEFORE,)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hook_id", _uuid(self.hook_id, "Hook binding ID"))
        object.__setattr__(
            self,
            "phases",
            _phases(self.phases, label="Hook binding phases"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "phases": [phase.value for phase in self.phases],
        }

    @classmethod
    def from_dict(cls, data: object) -> HookBinding:
        if not isinstance(data, Mapping):
            raise ValueError("Hook binding must be an object")
        return cls(
            hook_id=data.get("hook_id"),  # type: ignore[arg-type]
            phases=_phases(
                data.get("phases", [HookPhase.BEFORE.value]),
                label="Hook binding phases",
            ),
        )


@dataclass(frozen=True, slots=True)
class VisualImageHookCondition:
    images: tuple[str, ...]
    match: HookMatch = HookMatch.FOUND
    threshold: object = 0.9
    color_match: object = False
    color_tolerance: object = 0.3
    search_settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.images or any(not isinstance(name, str) or not name for name in self.images):
            raise ValueError("Visual Image hook requires at least one image name")
        object.__setattr__(self, "images", tuple(self.images))
        object.__setattr__(self, "match", HookMatch(self.match))
        if not isinstance(self.search_settings, Mapping):
            raise ValueError("Visual Image hook search_settings must be an object")
        object.__setattr__(self, "search_settings", copy.deepcopy(dict(self.search_settings)))

    def match_params(self) -> dict[str, Any]:
        return {
            **copy.deepcopy(dict(self.search_settings)),
            "images": list(self.images),
            "threshold": copy.deepcopy(self.threshold),
            "color_match": copy.deepcopy(self.color_match),
            "color_tolerance": copy.deepcopy(self.color_tolerance),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": HookType.VISUAL_IMAGE.value,
            "images": list(self.images),
            "match": self.match.value,
            "threshold": copy.deepcopy(self.threshold),
            "color_match": copy.deepcopy(self.color_match),
            "color_tolerance": copy.deepcopy(self.color_tolerance),
            "search_settings": copy.deepcopy(dict(self.search_settings)),
        }


@dataclass(frozen=True, slots=True)
class BrowserElementHookCondition:
    locator: object
    match: HookMatch = HookMatch.FOUND

    def __post_init__(self) -> None:
        object.__setattr__(self, "locator", copy.deepcopy(self.locator))
        object.__setattr__(self, "match", HookMatch(self.match))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": HookType.BROWSER_ELEMENT.value,
            "locator": copy.deepcopy(self.locator),
            "match": self.match.value,
        }


HookCondition = VisualImageHookCondition | BrowserElementHookCondition


def hook_condition_from_dict(data: object) -> HookCondition:
    if not isinstance(data, Mapping):
        raise ValueError("Hook condition must be an object")
    try:
        hook_type = HookType(data.get("type"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Hook condition has an unsupported type") from exc
    try:
        match = HookMatch(data.get("match", HookMatch.FOUND.value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Hook condition has an unsupported match mode") from exc
    if hook_type is HookType.VISUAL_IMAGE:
        images = data.get("images", [])
        if not isinstance(images, (list, tuple)):
            raise ValueError("Visual Image hook images must be an array")
        return VisualImageHookCondition(
            images=tuple(images),  # type: ignore[arg-type]
            match=match,
            threshold=copy.deepcopy(data.get("threshold", 0.9)),
            color_match=copy.deepcopy(data.get("color_match", False)),
            color_tolerance=copy.deepcopy(data.get("color_tolerance", 0.3)),
            search_settings=copy.deepcopy(data.get("search_settings", {})),
        )
    return BrowserElementHookCondition(
        locator=copy.deepcopy(data.get("locator", "")),
        match=match,
    )


@dataclass(slots=True)
class AutomationHook:
    name: str
    condition: HookCondition
    actions: list[FlowCommand] = field(default_factory=list)
    hook_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    enabled: bool = True
    automatic_scope: HookAutomaticScope = HookAutomaticScope.NONE
    automatic_phases: tuple[HookPhase, ...] = (HookPhase.BEFORE,)
    retrigger: HookRetriggerPolicy = HookRetriggerPolicy.REARM_AFTER_ABSENT

    def __post_init__(self) -> None:
        self.hook_id = _uuid(self.hook_id, "Hook ID")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Hook requires a non-empty name")
        if not isinstance(self.description, str):
            raise ValueError("Hook description must be text")
        if not isinstance(self.enabled, bool):
            raise ValueError("Hook enabled must be a boolean")
        if not isinstance(self.condition, (VisualImageHookCondition, BrowserElementHookCondition)):
            raise ValueError("Hook condition is invalid")
        if not isinstance(self.actions, list) or any(not isinstance(item, FlowCommand) for item in self.actions):
            raise ValueError("Hook actions must be Flow commands")
        self.automatic_scope = HookAutomaticScope(self.automatic_scope)
        self.automatic_phases = _phases(
            self.automatic_phases,
            label="Hook automatic phases",
        )
        self.retrigger = HookRetriggerPolicy(self.retrigger)

    @property
    def type(self) -> HookType:
        if isinstance(self.condition, VisualImageHookCondition):
            return HookType.VISUAL_IMAGE
        return HookType.BROWSER_ELEMENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "condition": self.condition.to_dict(),
            "actions": [command.to_dict() for command in self.actions],
            "automatic_scope": self.automatic_scope.value,
            "automatic_phases": [phase.value for phase in self.automatic_phases],
            "retrigger": self.retrigger.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> AutomationHook:
        if not isinstance(data, Mapping):
            raise ValueError("Hook must be an object")
        actions = data.get("actions", [])
        if not isinstance(actions, list):
            raise ValueError("Hook actions must be an array")
        return cls(
            hook_id=data.get("hook_id"),  # type: ignore[arg-type]
            name=data.get("name"),  # type: ignore[arg-type]
            description=data.get("description", ""),  # type: ignore[arg-type]
            enabled=data.get("enabled", True),  # type: ignore[arg-type]
            condition=hook_condition_from_dict(data.get("condition")),
            actions=[FlowCommand.from_dict(item) for item in actions],
            automatic_scope=HookAutomaticScope(data.get("automatic_scope", "none")),
            automatic_phases=_phases(
                data.get("automatic_phases", [HookPhase.BEFORE.value]),
                label="Hook automatic phases",
            ),
            retrigger=HookRetriggerPolicy(data.get("retrigger", "rearm_after_absent")),
        )

    def deep_copy(self, id_map: Mapping[str, str] | None = None) -> AutomationHook:
        copied_id = (id_map or {}).get(self.hook_id, str(uuid.uuid4()))
        return AutomationHook.from_dict(
            {**self.to_dict(), "hook_id": copied_id, "actions": [item.deep_copy().to_dict() for item in self.actions]}
        )


__all__ = [
    "AutomationHook",
    "BrowserElementHookCondition",
    "HookAutomaticScope",
    "HookBinding",
    "HookMatch",
    "HookPhase",
    "HookRetriggerPolicy",
    "HookType",
    "VisualImageHookCondition",
]
