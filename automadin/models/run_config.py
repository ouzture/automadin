from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from automadin.models.managed_emulator import (
    EmulatorCleanupOverride,
    canonical_managed_emulator_profile_id,
)
from automadin.models.resource_path import normalize_resource_path
from automadin.models.resolution_scaling import ResolutionScalingOverride


class RunStatus(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    ACQUIRING_EMULATOR = "acquiring emulator"
    STARTING_EMULATOR = "starting emulator"
    WAITING_FOR_ANDROID = "waiting for android"
    CHECKING_ANDROID = "checking android"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    REQUIRES_ATTENTION = "requires attention"
    STOPPED = "stopped"
    ERROR = "error"
    COMPLETED = "completed"
    CLEANING_UP = "cleaning up"
    CLEANUP_PENDING = "cleanup pending"


class RunMode(Enum):
    ONCE = "once"
    COUNT = "count"
    DURATION = "duration"
    CONTINUOUS = "continuous"
    UNTIL_ERROR = "until_error"


class BrowserRunMode(Enum):
    """How a saved browser run obtains its browser session."""

    LAUNCH = "launch"
    CONNECTED = "connected"


def normalize_group_path(value: Any) -> str:
    """Return a compact slash-separated run-config group path."""
    return normalize_resource_path(value)


@dataclass
class RunConfig:
    mode: RunMode = RunMode.ONCE
    count: int = 1
    duration_seconds: int = 0
    stop_on_error: bool = True
    delay_between_runs_ms: int = 0
    resolution_scaling_override: ResolutionScalingOverride = ResolutionScalingOverride.INHERIT
    custom_scale_percent: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.resolution_scaling_override, ResolutionScalingOverride):
            raise ValueError("Resolution scaling override is invalid.")
        if type(self.custom_scale_percent) is not int or not (
            25 <= self.custom_scale_percent <= 400
        ):
            raise ValueError("Custom resolution scale must be from 25% to 400%.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "count": self.count,
            "duration_seconds": self.duration_seconds,
            "stop_on_error": self.stop_on_error,
            "delay_between_runs_ms": self.delay_between_runs_ms,
            "resolution_scaling_override": self.resolution_scaling_override.value,
            "custom_scale_percent": self.custom_scale_percent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        try:
            scaling_override = ResolutionScalingOverride(
                data.get("resolution_scaling_override", "inherit")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Resolution scaling override is invalid.") from exc
        return cls(
            mode=RunMode(data.get("mode", "once")),
            count=data.get("count", 1),
            duration_seconds=data.get("duration_seconds", 0),
            stop_on_error=data.get("stop_on_error", True),
            delay_between_runs_ms=data.get("delay_between_runs_ms", 0),
            resolution_scaling_override=scaling_override,
            custom_scale_percent=data.get("custom_scale_percent", 100),
        )


@dataclass
class SavedRunConfig:
    name: str
    flow_name: str
    device_id: str
    group_path: str = ""
    config: RunConfig = field(default_factory=RunConfig)
    variables: dict[str, Any] = field(default_factory=dict)
    parameter_overrides: dict[str, Any] = field(default_factory=dict)
    input_overrides: dict[str, Any] = field(default_factory=dict)
    browser_options: dict[str, Any] = field(default_factory=dict)
    browser_run_mode: BrowserRunMode = BrowserRunMode.LAUNCH
    managed_emulator_profile_id: str = ""
    emulator_cleanup_override: EmulatorCleanupOverride = EmulatorCleanupOverride.INHERIT
    is_pinned: bool = False
    ask_for_values: bool = True

    def __post_init__(self) -> None:
        if self.managed_emulator_profile_id:
            self.managed_emulator_profile_id = canonical_managed_emulator_profile_id(
                self.managed_emulator_profile_id
            )
        if not isinstance(self.emulator_cleanup_override, EmulatorCleanupOverride):
            raise ValueError("Emulator cleanup override is invalid.")
        if not isinstance(self.browser_run_mode, BrowserRunMode):
            raise ValueError("Browser run mode is invalid.")
        if type(self.is_pinned) is not bool:
            raise ValueError("Run Setup pin state must be Boolean.")
        if type(self.ask_for_values) is not bool:
            raise ValueError("Run Setup value prompt setting must be Boolean.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "flow_name": self.flow_name,
            "device_id": self.device_id,
            "group_path": normalize_group_path(self.group_path),
            "config": self.config.to_dict(),
            "variables": self.variables,
            "parameter_overrides": self.parameter_overrides,
            "input_overrides": self.input_overrides,
            "browser_options": self.browser_options,
            "browser_run_mode": self.browser_run_mode.value,
            "managed_emulator_profile_id": self.managed_emulator_profile_id,
            "emulator_cleanup_override": self.emulator_cleanup_override.value,
            "is_pinned": self.is_pinned,
            "ask_for_values": self.ask_for_values,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SavedRunConfig:
        variables = data.get("variables", {})
        if not isinstance(variables, dict):
            variables = {}
        parameter_overrides = data.get("parameter_overrides", {})
        if not isinstance(parameter_overrides, dict):
            parameter_overrides = {}
        input_overrides = data.get("input_overrides", {})
        if not isinstance(input_overrides, dict):
            input_overrides = {}
        browser_options = data.get("browser_options", {})
        if not isinstance(browser_options, dict):
            browser_options = {}
        profile_id = data.get("managed_emulator_profile_id", "")
        if not isinstance(profile_id, str):
            raise ValueError("Managed emulator profile ID must be text.")
        try:
            cleanup_override = EmulatorCleanupOverride(
                data.get("emulator_cleanup_override", "inherit")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Emulator cleanup override is invalid.") from exc
        try:
            browser_run_mode = BrowserRunMode(data.get("browser_run_mode", "launch"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Browser run mode is invalid.") from exc
        is_pinned = data.get("is_pinned", False)
        if type(is_pinned) is not bool:
            raise ValueError("Run Setup pin state must be Boolean.")
        ask_for_values = data.get("ask_for_values", True)
        if type(ask_for_values) is not bool:
            raise ValueError("Run Setup value prompt setting must be Boolean.")
        return cls(
            name=data["name"],
            flow_name=data["flow_name"],
            device_id=data["device_id"],
            group_path=normalize_group_path(data.get("group_path", "")),
            config=RunConfig.from_dict(data.get("config", {})),
            variables=variables,
            parameter_overrides=parameter_overrides,
            input_overrides=input_overrides,
            browser_options=browser_options,
            browser_run_mode=browser_run_mode,
            managed_emulator_profile_id=profile_id,
            emulator_cleanup_override=cleanup_override,
            is_pinned=is_pinned,
            ask_for_values=ask_for_values,
        )
