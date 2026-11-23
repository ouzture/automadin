from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from automadin.models.device import AndroidDevice


class ManagedEmulatorTargetMode(Enum):
    PROVIDER_INSTANCE = "provider_instance"
    EXACT_ADB_ENDPOINT = "exact_adb_endpoint"


@dataclass(frozen=True, slots=True)
class ManagedEmulatorTargetResolution:
    """One exact provider observation, including not-yet-ready state."""

    running: bool
    device: AndroidDevice | None = None


class EmulatorCleanupPolicy(Enum):
    CLOSE_IF_STARTED = "close_if_started"
    LEAVE_RUNNING = "leave_running"
    ALWAYS_CLOSE = "always_close"


class EmulatorCleanupOverride(Enum):
    INHERIT = "inherit"
    CLOSE_IF_STARTED = EmulatorCleanupPolicy.CLOSE_IF_STARTED.value
    LEAVE_RUNNING = EmulatorCleanupPolicy.LEAVE_RUNNING.value
    ALWAYS_CLOSE = EmulatorCleanupPolicy.ALWAYS_CLOSE.value


def canonical_managed_emulator_profile_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Managed emulator profile ID must be a canonical UUID.")
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Managed emulator profile ID must be a canonical UUID.") from exc
    if value != canonical:
        raise ValueError("Managed emulator profile ID must be a canonical UUID.")
    return canonical


def canonical_adb_endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ADB endpoint must be text in host:port form.")
    endpoint = value.strip()
    if not endpoint or any(character.isspace() for character in endpoint):
        raise ValueError("ADB endpoint must be text in host:port form.")
    host, separator, port_text = endpoint.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError("ADB endpoint must be text in host:port form.")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("ADB endpoint port must be between 1 and 65535.")
    return f"{host.casefold()}:{port}"


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be non-empty text.")
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{label} must be non-empty text.")
    return normalized


def _optional_text(value: object, label: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{label} must be text.")
    return value.strip()


def _arguments(value: object, label: str) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of text arguments.")
    arguments: list[str] = []
    for argument in value:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ValueError(f"{label} must contain only text arguments.")
        arguments.append(argument)
    return tuple(arguments)


def _bounded_seconds(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum} seconds.")
    return value


@dataclass(frozen=True, slots=True)
class EmulatorProcessCommand:
    executable_path: str
    arguments: tuple[str, ...] = ()
    working_directory: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "executable_path",
            _required_text(self.executable_path, "Executable path"),
        )
        object.__setattr__(
            self,
            "arguments",
            _arguments(self.arguments, "Process arguments"),
        )
        object.__setattr__(
            self,
            "working_directory",
            _optional_text(self.working_directory, "Working directory"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable_path": self.executable_path,
            "arguments": list(self.arguments),
            "working_directory": self.working_directory,
        }

    @classmethod
    def from_dict(cls, data: object) -> EmulatorProcessCommand:
        if not isinstance(data, dict):
            raise ValueError("Process command must be an object.")
        return cls(
            executable_path=data.get("executable_path", ""),
            arguments=_arguments(data.get("arguments", ()), "Process arguments"),
            working_directory=data.get("working_directory", ""),
        )


@dataclass(frozen=True, slots=True)
class ManagedEmulatorProfile:
    name: str
    launch_command: EmulatorProcessCommand
    target_mode: ManagedEmulatorTargetMode
    profile_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    provider_id: str = ""
    provider_instance_id: str = ""
    adb_endpoint: str = ""
    startup_timeout_seconds: int = 120
    default_cleanup_policy: EmulatorCleanupPolicy = (
        EmulatorCleanupPolicy.CLOSE_IF_STARTED
    )
    shutdown_command: EmulatorProcessCommand | None = None
    shutdown_timeout_seconds: int = 30
    force_terminate_owned_process: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "Profile name"))
        object.__setattr__(
            self,
            "profile_id",
            canonical_managed_emulator_profile_id(self.profile_id),
        )
        if not isinstance(self.launch_command, EmulatorProcessCommand):
            raise ValueError("Launch command must be configured.")
        if not isinstance(self.target_mode, ManagedEmulatorTargetMode):
            raise ValueError("Managed emulator target mode is invalid.")
        provider_id = _optional_text(self.provider_id, "Provider ID")
        instance_id = _optional_text(self.provider_instance_id, "Provider instance ID")
        endpoint = _optional_text(self.adb_endpoint, "ADB endpoint")
        if self.target_mode is ManagedEmulatorTargetMode.PROVIDER_INSTANCE:
            provider_id = _required_text(provider_id, "Provider ID").casefold()
            instance_id = _required_text(instance_id, "Provider instance ID")
            endpoint = ""
        else:
            provider_id = ""
            instance_id = ""
            endpoint = canonical_adb_endpoint(endpoint)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "provider_instance_id", instance_id)
        object.__setattr__(self, "adb_endpoint", endpoint)
        object.__setattr__(
            self,
            "startup_timeout_seconds",
            _bounded_seconds(
                self.startup_timeout_seconds,
                "Startup timeout",
                5,
                600,
            ),
        )
        if not isinstance(self.default_cleanup_policy, EmulatorCleanupPolicy):
            raise ValueError("Default cleanup policy is invalid.")
        if self.shutdown_command is not None and not isinstance(
            self.shutdown_command,
            EmulatorProcessCommand,
        ):
            raise ValueError("Shutdown command is invalid.")
        object.__setattr__(
            self,
            "shutdown_timeout_seconds",
            _bounded_seconds(
                self.shutdown_timeout_seconds,
                "Shutdown timeout",
                1,
                120,
            ),
        )
        if type(self.force_terminate_owned_process) is not bool:
            raise ValueError("Force-terminate fallback must be true or false.")
        if (
            self.default_cleanup_policy is EmulatorCleanupPolicy.ALWAYS_CLOSE
            and self.shutdown_command is None
        ):
            raise ValueError(
                "Always close requires a shutdown command."
            )
        if (
            self.default_cleanup_policy is EmulatorCleanupPolicy.CLOSE_IF_STARTED
            and self.shutdown_command is None
            and not self.force_terminate_owned_process
        ):
            raise ValueError(
                "Close if started requires a shutdown command or the owned-process "
                "termination fallback."
            )

    @property
    def target_key(self) -> str:
        if self.target_mode is ManagedEmulatorTargetMode.PROVIDER_INSTANCE:
            return f"provider:{self.provider_id}:{self.provider_instance_id}"
        return f"adb:{self.adb_endpoint}"

    def effective_cleanup_policy(
        self,
        override: EmulatorCleanupOverride,
    ) -> EmulatorCleanupPolicy:
        if not isinstance(override, EmulatorCleanupOverride):
            raise ValueError("Cleanup override is invalid.")
        if override is EmulatorCleanupOverride.INHERIT:
            return self.default_cleanup_policy
        return EmulatorCleanupPolicy(override.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "launch_command": self.launch_command.to_dict(),
            "target_mode": self.target_mode.value,
            "provider_id": self.provider_id,
            "provider_instance_id": self.provider_instance_id,
            "adb_endpoint": self.adb_endpoint,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "default_cleanup_policy": self.default_cleanup_policy.value,
            "shutdown_command": (
                self.shutdown_command.to_dict()
                if self.shutdown_command is not None
                else None
            ),
            "shutdown_timeout_seconds": self.shutdown_timeout_seconds,
            "force_terminate_owned_process": self.force_terminate_owned_process,
        }

    @classmethod
    def from_dict(cls, data: object) -> ManagedEmulatorProfile:
        if not isinstance(data, dict):
            raise ValueError("Managed emulator profile must be an object.")
        shutdown_data = data.get("shutdown_command")
        try:
            target_mode = ManagedEmulatorTargetMode(data.get("target_mode"))
            cleanup_policy = EmulatorCleanupPolicy(
                data.get(
                    "default_cleanup_policy",
                    EmulatorCleanupPolicy.CLOSE_IF_STARTED.value,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Managed emulator profile contains an invalid option.") from exc
        return cls(
            profile_id=data.get("profile_id", ""),
            name=data.get("name", ""),
            launch_command=EmulatorProcessCommand.from_dict(
                data.get("launch_command")
            ),
            target_mode=target_mode,
            provider_id=data.get("provider_id", ""),
            provider_instance_id=data.get("provider_instance_id", ""),
            adb_endpoint=data.get("adb_endpoint", ""),
            startup_timeout_seconds=data.get("startup_timeout_seconds", 120),
            default_cleanup_policy=cleanup_policy,
            shutdown_command=(
                EmulatorProcessCommand.from_dict(shutdown_data)
                if shutdown_data is not None
                else None
            ),
            shutdown_timeout_seconds=data.get("shutdown_timeout_seconds", 30),
            force_terminate_owned_process=data.get(
                "force_terminate_owned_process",
                False,
            ),
        )


__all__ = [
    "EmulatorCleanupOverride",
    "EmulatorCleanupPolicy",
    "EmulatorProcessCommand",
    "ManagedEmulatorProfile",
    "ManagedEmulatorTargetResolution",
    "ManagedEmulatorTargetMode",
    "canonical_adb_endpoint",
    "canonical_managed_emulator_profile_id",
]
