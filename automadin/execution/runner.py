"""Background execution service for running flows on QThread workers."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from automadin.engine.browser_session import BrowserSessionHandle
from automadin.execution.context import FlowContext, FlowExecutionListener
from automadin.execution.attention import AttentionRequest, AttentionResolution
from automadin.execution.executor import FlowExecutor
from automadin.execution.error_report import ExecutionErrorReport
from automadin.models.device import AndroidDevice, Device, WindowsDevice
from automadin.models.flow import AutomationFlow
from automadin.models.run_config import RunConfig, RunMode, RunStatus
from automadin.services.browser_profile_leases import ProfileUse
from automadin.services.device_operation_leases import (
    DeviceOperationConflictError,
    DeviceOperationLease,
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    default_device_operation_registry,
    device_operation_key,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)

logger = logging.getLogger(__name__)


def _ts() -> str:
    """Return a compact timestamp for UI logs: HH:MM:SS."""
    return datetime.now().strftime("%H:%M:%S")


class _WorkerListener(FlowExecutionListener):
    """Bridges FlowContext events to QThread signals with timestamps."""

    def __init__(self, worker: FlowRunWorker) -> None:
        self._worker = worker
        self._ctx: FlowContext | None = None
        self.error_reported = False

    def on_step_start(self, command_id: str, step_id: str) -> None:
        self._worker.step_started.emit(command_id, step_id)
        # In step-through mode, take a screenshot BEFORE the command runs
        ctx = self._ctx
        if ctx and ctx._step_through:
            self._refresh_debug_screenshot(ctx, stage="pre-step")
            if ctx._last_screenshot:
                self._worker.screenshot_pre_step.emit(ctx._last_screenshot)

    def on_step_end(self, command_id: str, step_id: str) -> None:
        self._worker.step_ended.emit(command_id, step_id)
        ctx = self._ctx
        # In step-through mode, auto-take a fresh screenshot after every command
        if ctx and ctx._step_through:
            self._refresh_debug_screenshot(ctx, stage="post-step")
        # Emit the latest screenshot (from step-through auto-capture or prior command)
        if ctx and ctx._last_screenshot:
            self._worker.screenshot_updated.emit(ctx._last_screenshot)

    def _refresh_debug_screenshot(
        self,
        ctx: FlowContext,
        *,
        stage: str,
    ) -> None:
        try:
            from automadin.execution.handlers.detection import (
                _take_screenshot_for_device,
            )

            screenshot = _take_screenshot_for_device(ctx)
            if screenshot is not None:
                ctx._last_screenshot = screenshot
                return
        except Exception:
            logger.exception("Flow debugger %s screenshot failed", stage)
        self._worker.screenshot_unavailable.emit(
            f"{stage.capitalize()} screenshot is unavailable. "
            "The last valid preview is still shown."
        )

    def on_log(self, message: str) -> None:
        # Auto-prefix with short_id if not already present
        if self._ctx and self._ctx.current_short_id and not message.startswith("["):
            sid = self._ctx.current_short_id
            message = f"[{sid}] {message}"
        self._worker.log_message.emit(f"[{_ts()}] {message}")

    def _diagnostic_message(self, message: str) -> str:
        if self._ctx and self._ctx.current_short_id and not message.startswith("["):
            sid = self._ctx.current_short_id
            message = f"[{sid}] {message}"
        return f"[{_ts()}] {message}"

    def on_detailed_log(self, message: str) -> None:
        self._worker.detailed_log_message.emit(self._diagnostic_message(message))

    def on_trace_log(self, message: str) -> None:
        self._worker.trace_log_message.emit(self._diagnostic_message(message))

    def on_error(self, message: str) -> None:
        self.error_reported = True
        if self._ctx and self._ctx.current_short_id and not message.startswith("["):
            sid = self._ctx.current_short_id
            message = f"[{sid}] {message}"
        self._worker.error_occurred.emit(f"[{_ts()}] {message}")

    def on_error_report(self, report: ExecutionErrorReport) -> None:
        self.error_reported = True
        self._worker.error_occurred.emit(f"[{_ts()}] {report.log_message()}")
        self._worker.error_details_available.emit(report)

    def on_complete(self, message: str) -> None:
        self._worker.log_message.emit(f"[{_ts()}] {message}")

    def on_attention_required(self, request: AttentionRequest) -> None:
        self._worker._set_status(RunStatus.REQUIRES_ATTENTION)
        if not self._worker.attention_enabled:
            self._worker.require_attention(request.message)
            self.on_log(f"Requires attention: {request.message}")
            request.resolve(AttentionResolution.STOP)
            return
        self._worker.attention_required.emit(request)

    def on_pause_reached(self) -> None:
        self._worker._pause_reached()

    def on_attention_resolved(self, resolution: AttentionResolution) -> None:
        if resolution is not AttentionResolution.STOP and not self._worker.ctx.is_stopped:
            self._worker._set_status(RunStatus.RUNNING)


class FlowRunWorker(QThread):
    """Runs a flow in a background thread, emitting Qt signals for UI updates."""

    log_message = Signal(str)
    detailed_log_message = Signal(str)
    trace_log_message = Signal(str)
    step_started = Signal(str, str)   # command_id, step_id
    step_ended = Signal(str, str)     # command_id, step_id
    status_changed = Signal(str)      # RunStatus value
    error_occurred = Signal(str)
    error_details_available = Signal(object)
    completed = Signal(str)
    screenshot_updated = Signal(bytes)    # raw PNG bytes after command executes
    screenshot_pre_step = Signal(bytes)  # raw PNG bytes BEFORE command runs (step-through only)
    screenshot_unavailable = Signal(str)
    attention_required = Signal(object)

    def __init__(
        self,
        executor: FlowExecutor,
        flow: AutomationFlow,
        ctx: FlowContext,
        config: RunConfig,
        parent: Any = None,
        *,
        device_operation_registry: DeviceOperationLeaseRegistry | None = None,
    ) -> None:
        super().__init__(parent)
        self._executor = executor
        self._flow = flow
        self._ctx = ctx
        self._config = config
        self._ctx.resolution_scaling_override = config.resolution_scaling_override
        self._ctx.custom_scale_percent = config.custom_scale_percent
        self._status = RunStatus.IDLE
        self._device_operation_registry = (
            device_operation_registry or default_device_operation_registry()
        )
        self._device_operation_use = DeviceOperationUse(
            kind="flow-run",
            label=f"Flow run: {flow.name}",
            operation_id=str(uuid4()),
        )
        self._device_operation_lease: DeviceOperationLease | None = None
        self._attention_enabled = False
        self._attention_required_message: str | None = None
        self._run_display_name = flow.name
        self._target_display_name = "Target"
        self._run_source = "manual"
        self._registered_monotonic = time.monotonic()
        self._pause_requested = False

    # ── Properties ─────────────────────────────────────────────────

    @property
    def status(self) -> RunStatus:
        return self._status

    @property
    def ctx(self) -> FlowContext:
        return self._ctx

    @property
    def attention_enabled(self) -> bool:
        return self._attention_enabled

    @property
    def attention_required_message(self) -> str | None:
        """Why a non-interactive run stopped for a human prerequisite."""
        return self._attention_required_message

    @property
    def run_display_name(self) -> str:
        return self._run_display_name

    @property
    def target_display_name(self) -> str:
        return self._target_display_name

    @property
    def run_source(self) -> str:
        return self._run_source

    @property
    def registered_monotonic(self) -> float:
        return self._registered_monotonic

    def configure_run_presentation(
        self,
        *,
        display_name: str,
        target_display_name: str,
        source: str,
    ) -> None:
        """Attach stable user-facing identity before the worker is registered."""
        self._run_display_name = display_name
        self._target_display_name = target_display_name
        self._run_source = source
        self._registered_monotonic = time.monotonic()

    def enable_attention(self) -> None:
        """Allow this worker's listener to request a GUI decision."""
        self._attention_enabled = True

    def require_attention(self, message: str) -> None:
        """Record the first unmet prerequisite for an unattended run result."""
        if self._attention_required_message is None:
            self._attention_required_message = message

    def request_pause(self) -> bool:
        if self._status in {
            RunStatus.COMPLETED,
            RunStatus.ERROR,
            RunStatus.STOPPED,
            RunStatus.STOPPING,
        }:
            return False
        self._pause_requested = True
        self._ctx.pause()
        self._set_status(RunStatus.PAUSING)
        return True

    def _pause_reached(self) -> None:
        # The context can pause itself after a command in step-through mode,
        # without going through request_pause().  wait_if_paused() only calls
        # this hook while the execution gate is actually closed, so reflecting
        # that state here also makes the next resume request valid.
        if self._status not in {
            RunStatus.COMPLETED,
            RunStatus.ERROR,
            RunStatus.STOPPED,
            RunStatus.STOPPING,
        }:
            self._set_status(RunStatus.PAUSED)

    def request_resume(self) -> bool:
        if self._status not in {RunStatus.PAUSING, RunStatus.PAUSED}:
            return False
        self._pause_requested = False
        self._ctx.resume()
        self._set_status(RunStatus.RUNNING)
        return True

    def request_stop(self) -> bool:
        if self._status in {
            RunStatus.COMPLETED,
            RunStatus.ERROR,
            RunStatus.STOPPED,
        }:
            return False
        self._pause_requested = False
        self._set_status(RunStatus.STOPPING)
        self._ctx.stop()
        return True

    def mark_cleanup_pending(self) -> None:
        self._set_status(RunStatus.CLEANUP_PENDING)

    # ── Thread body ────────────────────────────────────────────────

    def run(self) -> None:
        acquired, lease = self._acquire_device_operation()
        if not acquired:
            return
        try:
            self._run_with_device_operation()
        finally:
            if isinstance(lease, DeviceOperationLease):
                try:
                    lease.release()
                except BaseException:
                    logger.exception("Could not release flow device operation")
                else:
                    if lease is self._device_operation_lease:
                        self._device_operation_lease = None

    def reserve_device_operation(self) -> None:
        """Reserve this worker's target before it is published as started."""
        if self._device_operation_lease is not None:
            return
        device = getattr(self._ctx.session, "device", None)
        if not isinstance(device, (AndroidDevice, WindowsDevice)):
            return
        self._device_operation_lease = self._device_operation_registry.acquire(
            device_operation_key(device),
            self._device_operation_use,
        )

    def release_reserved_device_operation(self) -> bool:
        """Release a reservation after worker startup was rolled back."""
        lease = self._device_operation_lease
        if lease is None:
            return True
        try:
            lease.release()
        except BaseException:
            logger.exception("Could not release reserved flow device operation")
            return False
        if lease is self._device_operation_lease:
            self._device_operation_lease = None
        return True

    def _acquire_device_operation(
        self,
    ) -> tuple[bool, DeviceOperationLease | None]:
        device = getattr(self._ctx.session, "device", None)
        if not isinstance(device, (AndroidDevice, WindowsDevice)):
            return True, None
        if self._device_operation_lease is not None:
            return True, self._device_operation_lease
        try:
            self._device_operation_lease = self._device_operation_registry.acquire(
                device_operation_key(device),
                self._device_operation_use,
            )
            return True, self._device_operation_lease
        except DeviceOperationConflictError as exc:
            message = (
                f"This device is already being used by {exc.current.label}. "
                "Stop that operation and try again."
            )
            logger.info(
                "Flow device operation conflict: requested=%s current=%s",
                exc.requested.operation_id,
                exc.current.operation_id,
            )
            self.error_occurred.emit(message)
            self._set_status(RunStatus.ERROR)
            self.completed.emit(message)
            return False, None
        except Exception:
            logger.exception("Could not reserve device for flow execution")
            message = (
                "The device could not be reserved for this run. "
                "Stop other operations and try again."
            )
            self.error_occurred.emit(message)
            self._set_status(RunStatus.ERROR)
            self.completed.emit(message)
            return False, None

    def _run_with_device_operation(self) -> None:
        self._set_status(
            RunStatus.PAUSING if self._pause_requested else RunStatus.RUNNING
        )
        listener = _WorkerListener(self)
        listener._ctx = self._ctx
        self._ctx.listener = listener

        try:
            mode = self._config.mode

            if mode == RunMode.ONCE:
                self.log_message.emit(f"[{_ts()}] Starting flow (once)")
                self._run_once()

            elif mode == RunMode.COUNT:
                for i in range(self._config.count):
                    if self._ctx.is_stopped:
                        break
                    self.log_message.emit(
                        f"[{_ts()}] --- Run {i + 1}/{self._config.count} ---"
                    )
                    self._run_once()
                    if i < self._config.count - 1:
                        self._delay_between_runs()

            elif mode == RunMode.DURATION:
                end_time = time.monotonic() + self._config.duration_seconds
                run_num = 0
                while time.monotonic() < end_time and not self._ctx.is_stopped:
                    run_num += 1
                    remaining = int(end_time - time.monotonic())
                    self.log_message.emit(
                        f"[{_ts()}] --- Run {run_num} ({remaining}s remaining) ---"
                    )
                    self._run_once()
                    self._delay_between_runs()

            elif mode == RunMode.CONTINUOUS:
                run_num = 0
                while not self._ctx.is_stopped:
                    run_num += 1
                    self.log_message.emit(f"[{_ts()}] --- Continuous run {run_num} ---")
                    self._run_once()
                    self._delay_between_runs()

            elif mode == RunMode.UNTIL_ERROR:
                run_num = 0
                while not self._ctx.is_stopped:
                    run_num += 1
                    self.log_message.emit(f"[{_ts()}] --- Run {run_num} (until error) ---")
                    try:
                        self._run_once()
                    except BaseException as exc:
                        error_ref = uuid4().hex[:8].upper()
                        logger.exception(
                            "Flow run failed in until-error mode on run %d "
                            "[reference=%s]",
                            run_num,
                            error_ref,
                        )
                        if not listener.error_reported:
                            listener.on_error_report(
                                ExecutionErrorReport.from_exception(
                                    exc,
                                    reference=error_ref,
                                )
                            )
                        self._set_status(RunStatus.ERROR)
                        self.completed.emit(
                            f"Stopped after an error on run {run_num}. "
                            "Use View details beneath the execution log."
                        )
                        return
                    self._delay_between_runs()

            # Normal completion
            if self._attention_required_message is not None:
                self._set_status(RunStatus.STOPPED)
                self.completed.emit(
                    f"Requires attention: {self._attention_required_message}"
                )
            elif self._ctx.is_stopped:
                self._set_status(RunStatus.STOPPED)
                self.completed.emit(f"[{_ts()}] Stopped by user")
            else:
                self._set_status(RunStatus.COMPLETED)
                self.completed.emit(f"[{_ts()}] Flow completed successfully")

        except BaseException as exc:
            error_ref = uuid4().hex[:8].upper()
            logger.exception("Flow execution failed [reference=%s]", error_ref)
            if not listener.error_reported:
                listener.on_error_report(
                    ExecutionErrorReport.from_exception(
                        exc,
                        reference=error_ref,
                    )
                )
            self._set_status(RunStatus.ERROR)
            self.completed.emit(
                "Flow stopped after an error. Use View details beneath the execution log."
            )

    # ── Helpers ────────────────────────────────────────────────────

    def _run_once(self) -> None:
        self._executor.execute(self._flow, self._ctx)

    def _delay_between_runs(self) -> None:
        if self._config.delay_between_runs_ms > 0 and not self._ctx.is_stopped:
            delay_s = self._config.delay_between_runs_ms / 1000.0
            # Sleep in small increments so we can react to stop quickly
            end = time.monotonic() + delay_s
            while time.monotonic() < end and not self._ctx.is_stopped:
                time.sleep(min(0.1, end - time.monotonic()))

    def _set_status(self, status: RunStatus) -> None:
        if self._pause_requested and status in {
            RunStatus.IDLE,
            RunStatus.CONNECTING,
            RunStatus.ACQUIRING_EMULATOR,
            RunStatus.STARTING_EMULATOR,
            RunStatus.WAITING_FOR_ANDROID,
            RunStatus.CHECKING_ANDROID,
            RunStatus.RUNNING,
        }:
            status = RunStatus.PAUSING
        self._status = status
        self.status_changed.emit(status.value)


class BrowserAwareRunWorker(FlowRunWorker):
    """Run a flow through the selected browser session's owner thread.

    The device selector already owns the Playwright context.  Opening another
    persistent context for the debugger risks profile corruption and makes the
    selected browser differ from the browser being automated.  This worker
    therefore submits each flow pass to the existing
    :class:`BrowserSessionHandle`; the QThread remains responsible for run
    controls and waits for the owner-thread operation to finish.
    """

    def __init__(
        self,
        executor: FlowExecutor,
        flow: AutomationFlow,
        ctx: FlowContext,
        config: RunConfig,
        parent: Any = None,
        *,
        profile_use_kind: str = "flow-debugger",
        profile_use_label: str | None = None,
        unavailable_context: str = "debugger",
    ) -> None:
        super().__init__(executor, flow, ctx, config, parent)
        self._original_session = ctx.session
        self._browser_handle: BrowserSessionHandle | None = None
        self._run_borrow = None
        self._unavailable_context = unavailable_context
        self._profile_use = ProfileUse(
            kind=profile_use_kind,
            label=profile_use_label or f"Flow debugger: {flow.name}",
            operation_id=str(uuid4()),
        )

    def reserve_browser_borrow(self) -> None:
        """Reserve the exact connected owner before a coordinated run starts."""
        browser_data = self._original_session.get_data("browser_data")
        if not isinstance(browser_data, BrowserSessionHandle):
            raise RuntimeError(
                "The selected browser session is no longer available. "
                f"Reconnect it and start the {self._unavailable_context} again."
            )
        self._browser_handle = browser_data
        owner = browser_data.profile_lease
        if owner is not None and self._run_borrow is None:
            self._run_borrow = owner.borrow(self._profile_use)

    def release_reserved_profile_lease(self) -> bool:
        """Release a pre-start or active connected-browser borrow."""
        borrow = self._run_borrow
        if borrow is None:
            return True
        try:
            borrow.release()
        except BaseException:
            logger.exception("Could not release connected browser run reservation")
            return False
        if borrow is self._run_borrow:
            self._run_borrow = None
        return True

    def run(self) -> None:
        try:
            self.reserve_browser_borrow()
            super().run()
        except BaseException:
            logger.exception("Could not reserve browser for debugger")
            message = (
                "The selected browser session is busy or no longer available. "
                "Stop the other browser operation, reconnect if needed, and try again."
            )
            self.error_occurred.emit(message)
            self._set_status(RunStatus.ERROR)
            self.completed.emit(message)
        finally:
            self.release_reserved_profile_lease()

    def _run_once(self) -> None:
        if self._browser_handle is None:
            raise RuntimeError(
                "The selected browser session is no longer available. "
                f"Reconnect it and start the {self._unavailable_context} again."
            )
        self._browser_handle.execute_flow(
            self._executor,
            self._flow,
            self._ctx,
            use=self._profile_use,
            borrowed_lease=self._run_borrow,
            timeout=None,
        )


class RunnerService(QObject):
    """Manages active FlowRunWorker threads."""

    run_registered = Signal(str, object)
    run_removed = Signal(str)

    def __init__(
        self,
        max_concurrent: int = 6,
        parent: QObject | None = None,
        *,
        default_source: str = "manual",
    ) -> None:
        super().__init__(parent)
        self._workers: dict[str, FlowRunWorker] = {}
        self._max_concurrent: int = max_concurrent
        self._default_source = default_source

    def start_run(
        self,
        run_id: str,
        executor: FlowExecutor,
        flow: AutomationFlow,
        ctx: FlowContext,
        config: RunConfig,
    ) -> FlowRunWorker:
        """Create, register, and start a worker for the given run."""
        worker = FlowRunWorker(executor, flow, ctx, config)
        device: Device | None = getattr(ctx.session, "device", None)
        target_name = device.name if device is not None else "Target"
        worker.configure_run_presentation(
            display_name=flow.name,
            target_display_name=target_name,
            source=self._default_source,
        )
        try:
            self.register_run(run_id, worker)
            worker.reserve_device_operation()
            worker.start()
        except BaseException:
            if qthread_start_may_have_succeeded(worker):
                # QThread.start() may raise after native execution has begun.
                # Keep both the worker and its lease registered until callers
                # observe finished and explicitly remove it.
                worker.ctx.stop()
                raise
            lease_released = worker.release_reserved_device_operation()
            if lease_released and self._workers.get(run_id) is worker:
                self._workers.pop(run_id, None)
            raise
        return worker

    def register_run(self, run_id: str, worker: FlowRunWorker) -> None:
        """Register an unstarted worker after enforcing service invariants.

        UI surfaces can connect signals before calling ``worker.start()`` so
        fast startup failures and status changes are never lost.
        """
        if run_id in self._workers:
            raise RuntimeError(f"Run '{run_id}' is already registered")
        self.ensure_capacity()

        self._workers[run_id] = worker
        self.run_registered.emit(run_id, worker)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def active_count(self) -> int:
        return sum(1 for worker in self._workers.values() if not worker.isFinished())

    def ensure_capacity(self) -> None:
        if self.active_count() >= self._max_concurrent:
            raise RuntimeError(
                f"Maximum concurrent runs ({self._max_concurrent}) reached"
            )

    def workers(self) -> tuple[FlowRunWorker, ...]:
        return tuple(self._workers.values())

    def run_items(self) -> tuple[tuple[str, FlowRunWorker], ...]:
        return tuple(self._workers.items())

    def pause_run(self, run_id: str) -> None:
        """Pause execution of the given run."""
        worker = self._workers.get(run_id)
        if worker is not None:
            worker.request_pause()

    def resume_run(self, run_id: str) -> None:
        """Resume execution of the given run."""
        worker = self._workers.get(run_id)
        if worker is not None:
            worker.request_resume()

    def stop_run(self, run_id: str, *, wait_ms: int = 0) -> bool:
        """Request stop and report whether the worker actually finished."""
        worker = self._workers.get(run_id)
        if worker is None:
            return True
        worker.request_stop()
        if worker.isRunning():
            worker.wait(wait_ms)
        return not worker.isRunning()

    def get_worker(self, run_id: str) -> FlowRunWorker | None:
        """Return the worker for the given run, or ``None``."""
        return self._workers.get(run_id)

    def remove_run(self, run_id: str, *, wait_ms: int = 0) -> bool:
        """Remove a finished run without losing a still-live worker handle."""
        worker = self._workers.get(run_id)
        if worker is None:
            return True
        if worker.isRunning() and not self.stop_run(run_id, wait_ms=wait_ms):
            return False
        if not worker.release_reserved_device_operation():
            return False
        self._workers.pop(run_id, None)
        self.run_removed.emit(run_id)
        return True

    def stop_all(self, *, wait_ms: int = 0) -> bool:
        """Stop all runs within one shared deadline and retain live workers."""
        workers = list(self._workers.values())
        for worker in workers:
            worker.request_stop()

        deadline = time.monotonic() + max(0, wait_ms) / 1000.0
        for worker in workers:
            if not worker.isRunning():
                continue
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if remaining_ms:
                worker.wait(remaining_ms)

        all_stopped = all(not worker.isRunning() for worker in workers)
        cleanup_ok = True
        if all_stopped:
            for worker in workers:
                cleanup_ok = (
                    worker.release_reserved_device_operation() and cleanup_ok
                )
        return all_stopped and cleanup_ok
