from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from automadin.models.device import AndroidDevice
from automadin.models.managed_emulator import (
    EmulatorCleanupPolicy,
    EmulatorProcessCommand,
    ManagedEmulatorProfile,
    ManagedEmulatorTargetResolution,
    ManagedEmulatorTargetMode,
)
from automadin.services.device_operation_leases import (
    DeviceOperationLease,
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.25
_MAX_DIAGNOSTIC_OUTPUT = 4000


class ManagedEmulatorLifecycleError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        partial_acquisition: ManagedEmulatorAcquisition | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_acquisition = partial_acquisition


class ManagedEmulatorCancelled(ManagedEmulatorLifecycleError):
    pass


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class ManagedEmulatorAcquisition:
    profile: ManagedEmulatorProfile
    device: AndroidDevice
    started_by_automadin: bool
    process: ProcessHandle | None = None


@dataclass(frozen=True, slots=True)
class ManagedEmulatorCleanupResult:
    warning: str = ""


_managed_emulator_leases = DeviceOperationLeaseRegistry()


def acquire_managed_emulator_lease(
    profile: ManagedEmulatorProfile,
    *,
    run_id: str,
    run_label: str,
    registry: DeviceOperationLeaseRegistry | None = None,
) -> DeviceOperationLease:
    active_registry = registry or _managed_emulator_leases
    return active_registry.acquire(
        f"managed-emulator:{profile.profile_id}",
        DeviceOperationUse(
            kind="managed-emulator-run",
            label=f"Run setup '{run_label}'",
            operation_id=run_id,
        ),
    )


def _process_arguments(command: EmulatorProcessCommand) -> list[str]:
    return [command.executable_path, *command.arguments]


def _subprocess_kwargs(command: EmulatorProcessCommand) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if command.working_directory:
        kwargs["cwd"] = command.working_directory
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _bounded_output(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(
        part.strip()
        for part in (result.stdout or "", result.stderr or "")
        if part and part.strip()
    )
    return output[:_MAX_DIAGNOSTIC_OUTPUT]


class ManagedEmulatorLifecycle:
    """Acquire and release one managed emulator around an existing Android run."""

    def __init__(
        self,
        discover_devices: Callable[[], Sequence[AndroidDevice]],
        *,
        discover_managed_target: Callable[
            [str, str], ManagedEmulatorTargetResolution | AndroidDevice | None
        ] | None = None,
        popen: Callable[..., ProcessHandle] = subprocess.Popen,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._discover_devices = discover_devices
        self._discover_managed_target = discover_managed_target
        self._popen = popen
        self._run_command = run_command
        self._monotonic = monotonic
        self._sleep = sleep

    def acquire(
        self,
        profile: ManagedEmulatorProfile,
        *,
        cancelled: Callable[[], bool],
        status: Callable[[str], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> ManagedEmulatorAcquisition:
        report_status = status or (lambda _message: None)
        report_log = log or (lambda _message: None)
        report_status("Acquiring emulator")
        observation = self._observe_target(profile)
        if observation.device is not None:
            report_log(f"Managed emulator '{profile.name}' is already available.")
            return ManagedEmulatorAcquisition(profile, observation.device, False)

        process: ProcessHandle | None = None
        started_by_automadin = False
        if not observation.running:
            if cancelled():
                raise ManagedEmulatorCancelled("Managed emulator startup was cancelled.")

            report_status("Starting emulator")
            try:
                process = self._popen(
                    _process_arguments(profile.launch_command),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **_subprocess_kwargs(profile.launch_command),
                )
            except OSError as exc:
                raise ManagedEmulatorLifecycleError(
                    f"Could not start managed emulator '{profile.name}': {exc}"
                ) from exc
            started_by_automadin = True

        partial_acquisition = ManagedEmulatorAcquisition(
            profile,
            self._placeholder_device(profile),
            started_by_automadin,
            process,
        )
        if cancelled():
            raise ManagedEmulatorCancelled(
                "Managed emulator startup was cancelled.",
                partial_acquisition=partial_acquisition,
            )
        report_status("Waiting for Android")
        deadline = self._monotonic() + profile.startup_timeout_seconds
        while self._monotonic() < deadline:
            if cancelled():
                raise ManagedEmulatorCancelled(
                    "Managed emulator startup was cancelled.",
                    partial_acquisition=partial_acquisition,
                )
            observation = self._observe_target(profile)
            if observation.device is not None:
                report_log(f"Managed emulator '{profile.name}' is ready.")
                return ManagedEmulatorAcquisition(
                    profile,
                    observation.device,
                    started_by_automadin,
                    process,
                )
            exit_code = process.poll() if process is not None else None
            if started_by_automadin and exit_code not in (None, 0):
                report_status("Android unavailable")
                raise ManagedEmulatorLifecycleError(
                    f"Managed emulator launcher exited with code {exit_code} "
                    "before Android became ready.",
                    partial_acquisition=partial_acquisition,
                )
            self._sleep(_POLL_INTERVAL_SECONDS)
        report_status("Android unavailable")
        raise ManagedEmulatorLifecycleError(
            f"Managed emulator '{profile.name}' did not become ready within "
            f"{profile.startup_timeout_seconds} seconds.",
            partial_acquisition=partial_acquisition,
        )

    def cleanup(
        self,
        acquisition: ManagedEmulatorAcquisition,
        policy: EmulatorCleanupPolicy,
        *,
        status: Callable[[str], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> ManagedEmulatorCleanupResult:
        if not isinstance(policy, EmulatorCleanupPolicy):
            raise ValueError("Cleanup policy is invalid.")
        should_close = policy is EmulatorCleanupPolicy.ALWAYS_CLOSE or (
            policy is EmulatorCleanupPolicy.CLOSE_IF_STARTED
            and acquisition.started_by_automadin
        )
        if not should_close:
            return ManagedEmulatorCleanupResult()

        report_status = status or (lambda _message: None)
        report_log = log or (lambda _message: None)
        report_status("Cleaning up")
        profile = acquisition.profile
        graceful_error = ""
        if profile.shutdown_command is not None:
            try:
                result = self._run_command(
                    _process_arguments(profile.shutdown_command),
                    capture_output=True,
                    text=True,
                    timeout=profile.shutdown_timeout_seconds,
                    **_subprocess_kwargs(profile.shutdown_command),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                graceful_error = str(exc)
            else:
                if result.returncode == 0:
                    report_log(f"Managed emulator '{profile.name}' was closed.")
                    return ManagedEmulatorCleanupResult()
                detail = _bounded_output(result)
                graceful_error = f"exit code {result.returncode}"
                if detail:
                    graceful_error += f": {detail}"

        process = acquisition.process
        if (
            acquisition.started_by_automadin
            and profile.force_terminate_owned_process
            and process is not None
            and process.poll() is None
        ):
            self._terminate_owned_process(
                process,
                timeout_seconds=profile.shutdown_timeout_seconds,
            )
            warning = "Graceful emulator shutdown failed; the owned process was terminated."
            if graceful_error:
                warning += f" Reason: {graceful_error}"
            report_log(warning)
            return ManagedEmulatorCleanupResult(warning=warning)

        if graceful_error:
            raise ManagedEmulatorLifecycleError(
                f"Managed emulator cleanup failed: {graceful_error}"
            )
        raise ManagedEmulatorLifecycleError(
            "Managed emulator cleanup failed because no safe shutdown operation "
            "was configured."
        )

    def _observe_target(
        self,
        profile: ManagedEmulatorProfile,
    ) -> ManagedEmulatorTargetResolution:
        devices = self._discover_devices()
        for device in devices:
            if not isinstance(device, AndroidDevice):
                continue
            if self._matches_profile_target(device, profile):
                return ManagedEmulatorTargetResolution(running=True, device=device)
        if (
            self._discover_managed_target is not None
            and profile.target_mode is ManagedEmulatorTargetMode.PROVIDER_INSTANCE
        ):
            try:
                candidate = self._discover_managed_target(
                    profile.provider_id,
                    profile.provider_instance_id,
                )
            except Exception as exc:
                raise ManagedEmulatorLifecycleError(
                    "Could not inspect managed emulator "
                    f"'{profile.name}' through its provider."
                ) from exc
            else:
                if isinstance(candidate, AndroidDevice):
                    candidate = ManagedEmulatorTargetResolution(
                        running=True,
                        device=candidate,
                    )
                if candidate is not None and candidate.device is not None:
                    if self._matches_profile_target(candidate.device, profile):
                        return candidate
                elif candidate is not None and candidate.running:
                    return candidate
        return ManagedEmulatorTargetResolution(running=False)

    @staticmethod
    def _matches_profile_target(
        device: AndroidDevice,
        profile: ManagedEmulatorProfile,
    ) -> bool:
        if profile.target_mode is ManagedEmulatorTargetMode.PROVIDER_INSTANCE:
            return (
                device.provider_id.casefold() == profile.provider_id
                and device.provider_instance_id == profile.provider_instance_id
            )
        return device.device_id.casefold() == profile.adb_endpoint

    @staticmethod
    def _placeholder_device(profile: ManagedEmulatorProfile) -> AndroidDevice:
        if profile.target_mode is ManagedEmulatorTargetMode.PROVIDER_INSTANCE:
            return AndroidDevice(
                "",
                provider_id=profile.provider_id,
                provider_instance_id=profile.provider_instance_id,
            )
        return AndroidDevice(profile.adb_endpoint)

    def _terminate_owned_process(
        self,
        process: ProcessHandle,
        *,
        timeout_seconds: int,
    ) -> None:
        try:
            process.terminate()
            process.wait(timeout=timeout_seconds)
            return
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Owned emulator process did not terminate gracefully")
        if sys.platform == "win32":
            try:
                result = self._run_command(
                    [
                        "taskkill.exe",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                process.wait(timeout=timeout_seconds)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ManagedEmulatorLifecycleError(
                    "Could not terminate the emulator process tree started by "
                    "Automadin."
                ) from exc
            if result.returncode != 0 and process.poll() is None:
                detail = _bounded_output(result)
                raise ManagedEmulatorLifecycleError(
                    "Could not terminate the emulator process tree started by "
                    f"Automadin.{f' {detail}' if detail else ''}"
                )
            return
        try:
            process.kill()
            process.wait(timeout=timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagedEmulatorLifecycleError(
                "Could not terminate the emulator process started by Automadin."
            ) from exc


__all__ = [
    "ManagedEmulatorAcquisition",
    "ManagedEmulatorCancelled",
    "ManagedEmulatorCleanupResult",
    "ManagedEmulatorLifecycle",
    "ManagedEmulatorLifecycleError",
    "acquire_managed_emulator_lease",
]
