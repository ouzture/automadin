from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from automadin.execution.build_executor import build_executor
from automadin.execution.flow_values import (
    FlowRuntimeValueError,
    resolve_flow_inputs,
)
from automadin.execution.runner import FlowRunWorker, RunnerService
from automadin.engine.browser_session import BrowserSessionHandle
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    Device,
    DeviceSession,
    WindowsDevice,
    device_from_saved_id,
)
from automadin.models.managed_emulator import (
    EmulatorCleanupPolicy,
    ManagedEmulatorProfile,
    ManagedEmulatorTargetMode,
)
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
)
from automadin.models.flow_parameter import (
    FlowParameterError,
    ParameterValue,
    resolve_parameter_values,
)
from automadin.models.run_config import BrowserRunMode, SavedRunConfig
from automadin.models.typed_value import TypedValue
from automadin.runtime import device_runtime_status
from automadin.services.browser_profile_leases import (
    OwnerProfileLease,
    ProfileUse,
)
from automadin.services.browser_profile_paths import (
    BrowserProfilePathError,
    resolve_legacy_profile_path,
)
from automadin.services.database import DatabaseService
from automadin.services.flow_preflight import (
    PreflightIssue,
    PreflightLocation,
    PreflightMode,
    PreflightReport,
    PreflightSeverity,
    preflight_flow,
    target_kind_for_device,
)
from automadin.services.managed_emulator_lifecycle import (
    acquire_managed_emulator_lease,
)
from automadin.services.windows_desktop import (
    WindowsDesktopCheck,
    WindowsDesktopState,
    detect_windows_desktop,
)


WorkerFactory = Callable[..., FlowRunWorker]


class RunPreparationError(ValueError):
    def __init__(self, title: str, message: str, *, warning: bool = False) -> None:
        super().__init__(message)
        self.title = title
        self.public_message = message
        self.warning = warning


class RunCapacityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedRun:
    saved_config: SavedRunConfig
    flow: AutomationFlow
    device: Device
    preflight: PreflightReport
    parameter_values: Mapping[str, ParameterValue]
    input_values: Mapping[str, TypedValue]
    connected_session: DeviceSession | None = None
    managed_emulator_profile: ManagedEmulatorProfile | None = None
    emulator_cleanup_policy: EmulatorCleanupPolicy | None = None


@dataclass(frozen=True, slots=True)
class RegisteredRun:
    run_id: str
    prepared: PreparedRun
    worker: FlowRunWorker


@dataclass(frozen=True, slots=True)
class CoordinatorShutdownResult:
    stopped: bool
    profile_cleanup_pending_ids: tuple[str, ...] = ()


def device_from_run_config(device_id: object) -> Device:
    return device_from_saved_id(device_id)


def device_from_managed_emulator_profile(
    profile: ManagedEmulatorProfile,
) -> AndroidDevice:
    if profile.target_mode is ManagedEmulatorTargetMode.PROVIDER_INSTANCE:
        return AndroidDevice(
            "",
            provider_id=profile.provider_id,
            provider_instance_id=profile.provider_instance_id,
            provider_display_name=profile.name,
        )
    return AndroidDevice(profile.adb_endpoint, provider_display_name=profile.name)


def browser_device_with_headless(
    device: BrowserDevice,
    headless: bool,
    *,
    headless_uses_headed_user_agent: bool = True,
) -> BrowserDevice:
    if device.config.cdp_endpoint:
        return device
    if (
        device.config.headless == headless
        and device.config.headless_uses_headed_user_agent == headless_uses_headed_user_agent
    ):
        return device
    return BrowserDevice(
        config=BrowserDeviceConfig(
            engine=device.config.engine,
            headless=headless,
            headless_uses_headed_user_agent=headless_uses_headed_user_agent,
            screen_mode=device.config.screen_mode,
            profile_name=device.config.profile_name,
            profile_id=device.config.profile_id,
            cdp_endpoint=device.config.cdp_endpoint,
            cdp_auto_launch=device.config.cdp_auto_launch,
        )
    )


def profile_reference_error(db: DatabaseService, device: Device) -> str | None:
    if not isinstance(device, BrowserDevice):
        return None

    config = device.config
    if config.profile_name and not config.profile_id:
        try:
            resolve_legacy_profile_path(config.profile_name)
        except BrowserProfilePathError:
            return (
                f"Browser profile '{config.profile_name}' has a saved reference "
                "that is unsafe and was blocked. Open Tools > Browser Profiles, repair "
                "or recreate it, then edit this run setup and choose a valid managed "
                "profile."
            )
        return (
            f"Browser profile '{config.profile_name}' uses an older unresolved "
            "reference. Open Tools > Browser Profiles and finish attaching that "
            "profile, then edit this run setup and select it again."
        )
    if not config.profile_id:
        return None

    record = db.get_profile(config.profile_id)
    profile_label = (
        record.name
        if record is not None
        else config.profile_name or f"profile {config.profile_id[:8]}"
    )
    if record is None:
        return (
            f"Browser profile '{profile_label}' no longer exists. "
            "Edit this run setup and choose an available profile."
        )
    if record.legacy_storage_name:
        return (
            f"Browser profile '{profile_label}' still needs setup after a database "
            "migration or import. Open Tools > Browser Profiles and explicitly use "
            "the existing folder or import a copy."
        )

    available_profiles = {target.profile_id: target for target in db.profile_engine_specs()}
    target = available_profiles.get(config.profile_id)
    if target is None:
        return (
            f"Browser profile '{profile_label}' is not ready because its UUID "
            "storage or identity manifest is missing, damaged, or incompatible. "
            "Open Tools > Browser Profiles to repair or import it, then try again."
        )
    if target.engine != config.engine.value:
        return (
            f"Browser profile '{profile_label}' now requires {target.engine}, but "
            f"this run setup uses {config.engine.value}. Edit the run setup "
            "and select the profile again."
        )
    return None


def acquire_profile_lease(
    device: Device,
    session_service: Any,
    *,
    run_id: str,
    run_label: str,
) -> OwnerProfileLease | None:
    if not isinstance(device, BrowserDevice) or not device.config.profile_id:
        return None
    registry = session_service.browser_profile_lease_registry
    return registry.acquire_owner(
        device.config.profile_id,
        ProfileUse(
            kind="launcher",
            label=f"Run setup '{run_label}'",
            operation_id=run_id,
        ),
    )


def launcher_preflight_report(
    *,
    db: DatabaseService,
    flow: AutomationFlow,
    device: Device,
    session_service: Any,
    run_setup_name: str = "",
    scheduled: bool = False,
    uses_connected_browser: bool = False,
    windows_desktop_probe: Callable[[], WindowsDesktopCheck] = detect_windows_desktop,
) -> PreflightReport:
    report = preflight_flow(
        flow,
        db=db,
        root_name=flow.name,
        target=target_kind_for_device(device),
        mode=PreflightMode.EXECUTION,
    )
    issues = list(report.issues)
    setup_label = f"Run setup '{run_setup_name}'" if run_setup_name else "Run setup"
    target_location = PreflightLocation(
        flow.name,
        (),
        "",
        "",
        scope=f"{setup_label} · target '{device.name}'",
    )

    if scheduled and isinstance(device, WindowsDevice) and report.requires_interactive_desktop:
        try:
            desktop = windows_desktop_probe()
        except Exception:
            desktop = WindowsDesktopCheck(WindowsDesktopState.UNKNOWN)
        if not isinstance(desktop, WindowsDesktopCheck) or not desktop.is_interactive:
            if (
                isinstance(desktop, WindowsDesktopCheck)
                and desktop.state is WindowsDesktopState.NON_INTERACTIVE
            ):
                message = (
                    "Windows UI automation is blocked because the desktop is locked "
                    "or non-interactive. Unlock the Windows session and retry the "
                    "scheduled run."
                )
            else:
                message = (
                    "Windows UI automation is blocked because Automadin could not "
                    "verify that the desktop is interactive. Unlock the Windows "
                    "session and retry the scheduled run."
                )
            issues.append(
                PreflightIssue(
                    code="WINDOWS_DESKTOP_NOT_INTERACTIVE",
                    severity=PreflightSeverity.ERROR,
                    message=message,
                    location=target_location,
                    remediation=("Unlock the Windows session, then retry the scheduled run."),
                )
            )

    runtime_status = (
        None
        if uses_connected_browser
        else device_runtime_status(device, Path(db.db_path).resolve().parent)
    )
    if runtime_status is not None and not runtime_status.usable:
        issues.append(
            PreflightIssue(
                code="RUNTIME_UNAVAILABLE",
                severity=PreflightSeverity.ERROR,
                message=runtime_status.error or "The selected target runtime is unavailable.",
                location=target_location,
                remediation=("Open Tools > Runtime Components, repair the runtime, and try again."),
            )
        )

    if (
        isinstance(device, BrowserDevice)
        and device.config.profile_id
        and not uses_connected_browser
    ):
        registry = getattr(session_service, "browser_profile_lease_registry", None)
        snapshot = (
            registry.snapshot(device.config.profile_id)
            if registry is not None and hasattr(registry, "snapshot")
            else None
        )
        if snapshot is not None:
            profile_label = db.get_profile_display_label(device.config.profile_id)
            profile_location = PreflightLocation(
                flow.name,
                (),
                "",
                "",
                scope=f"{setup_label} · browser profile '{profile_label}'",
            )
            active_use = snapshot.borrowed_by or snapshot.use
            detail = (
                f" It is currently borrowed by {snapshot.borrowed_by.label}."
                if snapshot.borrowed_by is not None
                else ""
            )
            remediation = (
                f"Open Runs and stop {active_use.label}, then try again."
                if active_use.kind == "launcher"
                else "Stop or disconnect the named browser operation, then try again."
            )
            issues.append(
                PreflightIssue(
                    code="PROFILE_IN_USE",
                    severity=PreflightSeverity.ERROR,
                    message=(
                        f"This browser profile is already in use by {snapshot.use.label}.{detail}"
                    ),
                    location=profile_location,
                    remediation=remediation,
                )
            )
    return PreflightReport(
        tuple(issues),
        report.visited_flows,
        report.requires_interactive_desktop,
    )


class RunCoordinator:
    """Shared preparation and lifecycle boundary for saved-run launches."""

    def __init__(
        self,
        database: DatabaseService,
        session_service: Any,
        runner_service: RunnerService,
        *,
        worker_factory: WorkerFactory,
        windows_desktop_probe: Callable[[], WindowsDesktopCheck] = detect_windows_desktop,
    ) -> None:
        self._database = database
        self._session_service = session_service
        self._runner_service = runner_service
        self._worker_factory = worker_factory
        self._windows_desktop_probe = windows_desktop_probe

    def prepare(
        self,
        saved_config: SavedRunConfig,
        *,
        transient_parameter_overrides: Mapping[str, Any] | object | None = None,
        transient_input_overrides: Mapping[str, Any] | object | None = None,
        scheduled: bool = False,
    ) -> PreparedRun:
        flow = self._database.load_flow(saved_config.flow_name)
        if flow is None:
            raise RunPreparationError(
                "Error",
                f"Flow '{saved_config.flow_name}' not found in database.",
            )
        if not flow.commands:
            raise RunPreparationError(
                "Empty Flow",
                "The selected flow has no commands.",
                warning=True,
            )
        if flow.value_model_version != CURRENT_FLOW_VALUE_MODEL_VERSION:
            raise RunPreparationError(
                "Legacy Flow Needs Repair",
                f"Flow '{flow.name}' uses the legacy value model and cannot run. "
                "Open Tools > Value Migration Audit, then rebuild it with "
                "declared Inputs, Variables, and explicit subflow mappings.",
                warning=True,
            )
        uses_connected_browser = saved_config.browser_run_mode is BrowserRunMode.CONNECTED
        connected_session = None
        managed_emulator_profile = None
        emulator_cleanup_policy = None
        if saved_config.managed_emulator_profile_id:
            managed_emulator_profile = self._database.get_managed_emulator_profile(
                saved_config.managed_emulator_profile_id
            )
            if managed_emulator_profile is None:
                raise RunPreparationError(
                    "Managed Emulator Unavailable",
                    "The managed emulator profile selected by this run setup no "
                    "longer exists. Edit the run setup and choose another profile.",
                )
            device = device_from_managed_emulator_profile(managed_emulator_profile)
            emulator_cleanup_policy = managed_emulator_profile.effective_cleanup_policy(
                saved_config.emulator_cleanup_override
            )
            if (
                emulator_cleanup_policy is EmulatorCleanupPolicy.ALWAYS_CLOSE
                and managed_emulator_profile.shutdown_command is None
            ):
                raise RunPreparationError(
                    "Managed Emulator Cleanup Unavailable",
                    "This run setup requests Always close, but the managed emulator "
                    "profile has no shutdown command.",
                )
            if (
                emulator_cleanup_policy is EmulatorCleanupPolicy.CLOSE_IF_STARTED
                and managed_emulator_profile.shutdown_command is None
                and not managed_emulator_profile.force_terminate_owned_process
            ):
                raise RunPreparationError(
                    "Managed Emulator Cleanup Unavailable",
                    "This run setup requests Close if started, but the managed "
                    "emulator profile has neither a shutdown command nor the "
                    "owned-process termination fallback.",
                )
        elif uses_connected_browser:
            list_sessions = getattr(self._session_service, "list_sessions", None)
            sessions = list_sessions() if callable(list_sessions) else []
            connected_browsers: list[DeviceSession] = []
            for candidate in sessions:
                browser_data = (
                    candidate.get_data("browser_data")
                    if isinstance(candidate, DeviceSession)
                    else None
                )
                if (
                    isinstance(getattr(candidate, "device", None), BrowserDevice)
                    and isinstance(browser_data, BrowserSessionHandle)
                    and not browser_data.is_closed
                ):
                    connected_browsers.append(candidate)
            if not connected_browsers:
                raise RunPreparationError(
                    "Connected Browser Unavailable",
                    "This run setup uses the live connected browser, but no browser "
                    "is currently connected in Automadin. Connect one in the top "
                    "target bar, then run this setup again.",
                )
            if len(connected_browsers) > 1:
                raise RunPreparationError(
                    "Connected Browser Ambiguous",
                    "More than one browser session is connected. Leave only the "
                    "browser this run should use connected, then try again.",
                )
            connected_session = connected_browsers[0]
            device = connected_session.device
        else:
            try:
                device = device_from_run_config(saved_config.device_id)
            except (TypeError, ValueError, IndexError, AttributeError) as exc:
                raise RunPreparationError(
                    "Invalid Device Target",
                    "This run setup contains an invalid saved device target. Edit the "
                    f"run setup and choose a device again.\n\n{exc}",
                ) from exc
        if not uses_connected_browser and isinstance(device, BrowserDevice) and saved_config.browser_options:
            device = browser_device_with_headless(
                device,
                bool(saved_config.browser_options.get("headless", False)),
                headless_uses_headed_user_agent=bool(
                    saved_config.browser_options.get(
                        "headless_uses_headed_user_agent",
                        True,
                    )
                ),
            )
        profile_error = profile_reference_error(self._database, device)
        if profile_error is not None:
            raise RunPreparationError("Profile Unavailable", profile_error)
        if flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION:
            try:
                parameter_values: Mapping[str, ParameterValue] = {}
                input_values = resolve_flow_inputs(
                    flow.value_catalog.inputs,
                    saved_config.input_overrides,
                    transient_input_overrides,
                )
            except FlowRuntimeValueError as exc:
                raise RunPreparationError("Invalid Flow Inputs", str(exc)) from exc
        else:
            try:
                parameter_values = resolve_parameter_values(
                    flow.parameters,
                    saved_config.parameter_overrides,
                    transient_parameter_overrides,
                )
                input_values = {}
            except FlowParameterError as exc:
                raise RunPreparationError("Invalid Flow Parameters", str(exc)) from exc
        return PreparedRun(
            saved_config=saved_config,
            flow=flow,
            device=device,
            preflight=launcher_preflight_report(
                db=self._database,
                flow=flow,
                device=device,
                session_service=self._session_service,
                run_setup_name=saved_config.name,
                scheduled=scheduled,
                uses_connected_browser=uses_connected_browser,
                windows_desktop_probe=self._windows_desktop_probe,
            ),
            parameter_values=parameter_values,
            input_values=input_values,
            connected_session=connected_session,
            managed_emulator_profile=managed_emulator_profile,
            emulator_cleanup_policy=emulator_cleanup_policy,
        )

    def prepare_scheduled(self, saved_config: SavedRunConfig) -> PreparedRun:
        """Prepare a scheduled run with unattended desktop safety checks."""
        return self.prepare(saved_config, scheduled=True)

    def register(
        self,
        prepared: PreparedRun,
        *,
        run_id: str | None = None,
        source: str = "manual",
        display_name: str | None = None,
    ) -> RegisteredRun:
        try:
            self._runner_service.ensure_capacity()
        except RuntimeError as exc:
            raise RunCapacityError(str(exc)) from exc
        identity = run_id or uuid.uuid4().hex[:12]
        worker = self._worker_factory(
            executor=build_executor(db=self._database),
            flow=prepared.flow,
            saved_config=prepared.saved_config,
            device=prepared.device,
            session_service=self._session_service,
            profile_lease=None,
            parameter_values=prepared.parameter_values,
            input_values=prepared.input_values,
            connected_session=prepared.connected_session,
            managed_emulator_profile=prepared.managed_emulator_profile,
            emulator_cleanup_policy=prepared.emulator_cleanup_policy,
        )
        configure_presentation = getattr(worker, "configure_run_presentation", None)
        if callable(configure_presentation):
            configure_presentation(
                display_name=display_name or prepared.saved_config.name,
                target_display_name=prepared.device.name,
                source=source,
            )
        try:
            self._runner_service.register_run(identity, worker)
        except RuntimeError as exc:
            raise RunCapacityError(str(exc)) from exc
        return RegisteredRun(identity, prepared, worker)

    def start(self, registered: RegisteredRun) -> None:
        worker = registered.worker
        if registered.prepared.connected_session is not None:
            reserve_browser = getattr(worker, "reserve_browser_borrow")
            reserve_browser()
            worker.reserve_device_operation()
            worker.start()
            return
        managed_profile = registered.prepared.managed_emulator_profile
        managed_attached = False
        if managed_profile is not None:
            attach_managed = getattr(worker, "attach_reserved_managed_emulator_lease")
            attach_managed(
                acquire_managed_emulator_lease(
                    managed_profile,
                    run_id=registered.run_id,
                    run_label=registered.prepared.saved_config.name,
                )
            )
            managed_attached = True
        try:
            attach_profile = getattr(worker, "attach_reserved_profile_lease")
            attach_profile(
                acquire_profile_lease(
                    registered.prepared.device,
                    self._session_service,
                    run_id=registered.run_id,
                    run_label=registered.prepared.saved_config.name,
                )
            )
            worker.reserve_device_operation()
        except BaseException:
            if managed_attached:
                release_managed = getattr(
                    worker,
                    "release_reserved_managed_emulator_lease",
                )
                release_managed()
            raise
        # Do not roll ownership back around QThread.start(): an exception can be
        # ambiguous about whether the worker actually began running. The normal
        # worker/coordinator cleanup path retains and releases both leases safely.
        worker.start()

    def remove(self, run_id: str) -> bool:
        worker = self._runner_service.get_worker(run_id)
        if worker is None:
            return True
        if worker.isRunning():
            self._runner_service.stop_run(run_id, wait_ms=0)
            return False
        release_profile = getattr(worker, "release_reserved_profile_lease", None)
        if release_profile is not None and not release_profile():
            return False
        release_managed = getattr(
            worker,
            "release_reserved_managed_emulator_lease",
            None,
        )
        if release_managed is not None and not release_managed():
            return False
        return self._runner_service.remove_run(run_id)

    def shutdown(self, *, wait_ms: int = 0) -> CoordinatorShutdownResult:
        if not self._runner_service.stop_all(wait_ms=wait_ms):
            return CoordinatorShutdownResult(False)
        pending: list[str] = []
        for run_id, worker in self._runner_service.run_items():
            release_profile = getattr(worker, "release_reserved_profile_lease", None)
            if release_profile is not None and not release_profile():
                pending.append(run_id)
                continue
            release_managed = getattr(
                worker,
                "release_reserved_managed_emulator_lease",
                None,
            )
            if release_managed is not None and not release_managed():
                pending.append(run_id)
        return CoordinatorShutdownResult(True, tuple(pending))
