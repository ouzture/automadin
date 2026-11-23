"""Lifecycle-safe background execution for Flow Editor ``Run Step`` actions."""

from __future__ import annotations

import copy
import logging
import time
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal, Slot

from automadin.engine.browser_session import BrowserSessionHandle
from automadin.execution.context import FlowContext
from automadin.models.device import BrowserDevice, DeviceSession
from automadin.models.flow import FlowCommand
from automadin.services.browser_profile_leases import ProfileUse
from automadin.services.device_operation_leases import (
    DeviceOperationConflictError,
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    default_device_operation_registry,
    device_operation_key,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)

logger = logging.getLogger(__name__)

_STATE_IDLE = "idle"
_STATE_RUNNING = "running"
_STATE_CANCEL_REQUESTED = "cancel_requested"
_STATE_CLEANUP_PENDING = "cleanup_pending"
_SAFE_FAILURE_MESSAGE = (
    "Run Step failed. Check the application log for details, then reconnect the "
    "device and try again."
)
_SAFE_START_FAILURE_MESSAGE = (
    "Run Step could not start. Reconnect the device and try again."
)
_SAFE_DEVICE_CONFLICT_MESSAGE = (
    "This device is already busy with another automation operation. Stop that "
    "operation or choose a different connected device, then try again."
)


class RunStepBusyError(RuntimeError):
    """Raised when an editor already owns an active Run Step operation."""


class RunStepStartError(RuntimeError):
    """A safe, user-displayable failure raised before a worker is running."""

    def __init__(self, message: str, *, cleanup_pending: bool = False) -> None:
        super().__init__(message)
        self.cleanup_pending = cleanup_pending


class _RunStepWorker(QThread):
    """Execute one immutable command snapshot without touching GUI objects."""

    terminal = Signal(str, str, str, str)

    def __init__(
        self,
        *,
        operation_id: str,
        executor: Any,
        command: FlowCommand,
        session: DeviceSession,
        flow_name: str,
        context: FlowContext,
        device_lease: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.operation_id = operation_id
        self.step_id = command.step_id
        self._executor = executor
        self._command = command
        self._session = session
        self._flow_name = flow_name
        self._context = context
        self._device_lease = device_lease

    def run(self) -> None:
        outcome = "succeeded"
        failure_message = _SAFE_FAILURE_MESSAGE
        try:
            if isinstance(self._session.device, BrowserDevice):
                self._execute_browser()
            else:
                self._executor.execute_command(
                    self._command,
                    self._context,
                    flow_name=self._flow_name,
                )
            if self._context.is_stopped:
                outcome = "cancelled"
        except BaseException as exc:
            logger.exception(
                "Run Step operation %s failed while executing step %s",
                self.operation_id,
                self.step_id,
            )
            outcome = "cancelled" if self._context.is_stopped else "failed"
            if self._context.hook_probe_only and outcome == "failed":
                detail = str(exc).strip()
                failure_message = (
                    f"Hook condition test failed: {detail}"
                    if detail
                    else "Hook condition test failed. Check the application log."
                )
        finally:
            if not self.release_device_lease():
                outcome = "failed"

        message = failure_message if outcome == "failed" else ""
        self.terminal.emit(self.operation_id, self.step_id, outcome, message)

    def release_device_lease(self) -> bool:
        """Release retained ownership, including after an abnormal worker exit."""
        lease = self._device_lease
        if lease is None:
            return True
        try:
            lease.release()
        except BaseException:
            logger.exception(
                "Run Step operation %s could not release its device lease",
                self.operation_id,
            )
            return False
        if lease is self._device_lease:
            self._device_lease = None
        return True

    def _execute_browser(self) -> None:
        browser_data = self._session.get_data("browser_data")
        if not isinstance(browser_data, BrowserSessionHandle):
            raise RuntimeError("Selected browser session is unavailable")
        self._context._executor = self._executor
        browser_data.execute_command(
            self._executor,
            self._command,
            self._context,
            use=ProfileUse(
                kind="run-step",
                label=f"Run Step: {self._command.id}",
                operation_id=self.operation_id,
            ),
            timeout=None,
        )


class RunStepOperationController(QObject):
    """Own exactly one active Run Step and retain every live worker safely."""

    state_changed = Signal(str, str, str)
    succeeded = Signal(str, str)
    failed = Signal(str, str, str)
    cancelled = Signal(str, str)
    probe_completed = Signal(str, str, object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        lease_registry: DeviceOperationLeaseRegistry | None = None,
    ) -> None:
        super().__init__(parent)
        self._lease_registry = (
            lease_registry
            if lease_registry is not None
            else default_device_operation_registry()
        )
        self._state = _STATE_IDLE
        self._active_operation_id: str | None = None
        self._active_step_id: str | None = None
        self._active_context: FlowContext | None = None
        self._workers: dict[str, _RunStepWorker] = {}
        self._terminal_outcomes: dict[str, tuple[str, str, str]] = {}

    @property
    def is_busy(self) -> bool:
        return self._active_operation_id is not None or bool(self._workers)

    @property
    def active_operation_id(self) -> str | None:
        return self._active_operation_id

    @property
    def active_step_id(self) -> str | None:
        return self._active_step_id

    @property
    def state(self) -> str:
        return self._state

    def start(
        self,
        executor: Any,
        command: FlowCommand,
        session: DeviceSession,
        flow_name: str,
        *,
        context: FlowContext | None = None,
    ) -> str:
        """Start one snapshotted command, or fail without leaking ownership."""
        if self.is_busy:
            raise RunStepBusyError(
                "Another Run Step operation is already active. Cancel it or wait "
                "for it to finish."
            )
        if command.id == "restart_flow":
            raise RunStepStartError(
                "Restart Flow cannot run as an isolated step. Run the complete "
                "flow from the debugger instead."
            )

        operation_id = str(uuid4())
        try:
            command_snapshot = copy.deepcopy(command)
            flow_name_snapshot = str(flow_name)
        except BaseException as exc:
            logger.exception("Run Step could not snapshot the requested command")
            raise RunStepStartError(_SAFE_START_FAILURE_MESSAGE) from exc
        if context is None:
            context = FlowContext(session)
        device_lease = None

        if not isinstance(session.device, BrowserDevice):
            try:
                device_lease = self._lease_registry.acquire(
                    device_operation_key(session.device),
                    DeviceOperationUse(
                        kind="run-step",
                        label=f"Run Step: {command_snapshot.id}",
                        operation_id=operation_id,
                    ),
                )
            except DeviceOperationConflictError as exc:
                logger.info("Run Step device ownership conflict: %s", exc)
                raise RunStepStartError(_SAFE_DEVICE_CONFLICT_MESSAGE) from exc
            except BaseException as exc:
                logger.exception("Run Step could not reserve the selected device")
                raise RunStepStartError(_SAFE_START_FAILURE_MESSAGE) from exc

        worker: _RunStepWorker | None = None
        try:
            worker = _RunStepWorker(
                operation_id=operation_id,
                executor=executor,
                command=command_snapshot,
                session=session,
                flow_name=flow_name_snapshot,
                context=context,
                device_lease=device_lease,
                parent=self,
            )
            worker.terminal.connect(self._on_worker_terminal)
            worker.finished.connect(
                lambda current=worker: self._on_worker_finished(
                    current.operation_id,
                    current,
                )
            )

            self._active_operation_id = operation_id
            self._active_step_id = command_snapshot.step_id
            self._active_context = context
            self._workers[operation_id] = worker
            worker.start()
        except BaseException as exc:
            logger.exception("Run Step worker could not be started")
            if qthread_start_may_have_succeeded(worker):
                context.stop()
                self._set_state(
                    operation_id,
                    command_snapshot.step_id,
                    _STATE_CANCEL_REQUESTED,
                )
                raise RunStepStartError(
                    _SAFE_START_FAILURE_MESSAGE,
                    cleanup_pending=True,
                ) from exc
            lease_released = True
            if worker is not None:
                lease_released = worker.release_device_lease()
            elif device_lease is not None:
                try:
                    device_lease.release()
                except BaseException:
                    logger.exception(
                        "Run Step startup rollback could not release its device lease"
                    )
                    lease_released = False
            if not lease_released and worker is not None:
                # A never-started worker is still the durable owner of the
                # retryable lease. Keep the operation visibly busy so shutdown
                # and the next cleanup pass can call _on_worker_finished().
                context.stop()
                self._set_state(
                    operation_id,
                    command_snapshot.step_id,
                    _STATE_CLEANUP_PENDING,
                )
                raise RunStepStartError(
                    _SAFE_START_FAILURE_MESSAGE,
                    cleanup_pending=True,
                ) from exc
            self._workers.pop(operation_id, None)
            self._active_operation_id = None
            self._active_step_id = None
            self._active_context = None
            if worker is not None:
                worker.deleteLater()
            raise RunStepStartError(_SAFE_START_FAILURE_MESSAGE) from exc
        self._set_state(operation_id, command_snapshot.step_id, _STATE_RUNNING)
        return operation_id

    def cancel(self, operation_id: str | None = None) -> bool:
        """Request cooperative cancellation of the exact active operation."""
        active_id = self._active_operation_id
        if active_id is None:
            return False
        if operation_id is not None and operation_id != active_id:
            return False
        context = self._active_context
        if context is None:
            return False
        worker = self._workers.get(active_id)
        if worker is not None and not worker.isRunning():
            self._on_worker_finished(active_id, worker)
            return True
        context.stop()
        if self._state != _STATE_CANCEL_REQUESTED:
            self._set_state(
                active_id,
                self._active_step_id or "",
                _STATE_CANCEL_REQUESTED,
            )
        return True

    def shutdown(self, wait_ms: int = 0) -> bool:
        """Cancel active work and retain workers that miss the shared deadline."""
        cleanup_retry_id = (
            self._active_operation_id
            if self._state == _STATE_CLEANUP_PENDING
            else None
        )
        self.cancel()
        deadline = time.monotonic() + max(0, int(wait_ms)) / 1000.0
        workers = list(self._workers.values())
        for worker in workers:
            if not worker.isRunning():
                continue
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if remaining_ms:
                worker.wait(remaining_ms)
        for operation_id, worker in list(self._workers.items()):
            if operation_id == cleanup_retry_id:
                continue
            if not worker.isRunning():
                self._on_worker_finished(operation_id, worker)
        return not self._workers

    @Slot(str, str, str, str)
    def _on_worker_terminal(
        self,
        operation_id: str,
        step_id: str,
        outcome: str,
        safe_message: str,
    ) -> None:
        if operation_id != self._active_operation_id:
            return
        if step_id != self._active_step_id:
            return
        if operation_id in self._terminal_outcomes:
            logger.error(
                "Run Step operation %s emitted more than one terminal outcome",
                operation_id,
            )
            return
        self._terminal_outcomes[operation_id] = (
            step_id,
            outcome,
            safe_message,
        )

    def _on_worker_finished(
        self,
        operation_id: str,
        worker: _RunStepWorker,
    ) -> None:
        if self._workers.get(operation_id) is not worker:
            worker.deleteLater()
            return
        lease_released = worker.release_device_lease()
        if not lease_released:
            logger.error(
                "Retaining Run Step worker %s after lease cleanup failed",
                operation_id,
            )
            if operation_id == self._active_operation_id:
                step_id = self._active_step_id or worker.step_id
            else:
                step_id = worker.step_id
            self._set_state(operation_id, step_id, _STATE_CLEANUP_PENDING)
            return
        if self._workers.get(operation_id) is worker:
            self._workers.pop(operation_id, None)
        if operation_id == self._active_operation_id:
            step_id = self._active_step_id or worker.step_id
            context = self._active_context
            was_cancelled = context is not None and context.is_stopped
            terminal = self._terminal_outcomes.pop(operation_id, None)
            probe_results = (
                (
                    dict(context.hook_probe_details)
                    if context.hook_probe_details
                    else dict(context.hook_probe_results)
                )
                if context is not None
                else {}
            )
            if terminal is None:
                logger.error(
                    "Run Step operation %s finished without a terminal outcome",
                    operation_id,
                )
                outcome = "cancelled" if was_cancelled else "failed"
                safe_message = "" if was_cancelled else _SAFE_FAILURE_MESSAGE
            else:
                _terminal_step_id, outcome, safe_message = terminal
            self._clear_active(operation_id, step_id)
            if outcome == "succeeded":
                self.succeeded.emit(operation_id, step_id)
                if probe_results:
                    self.probe_completed.emit(operation_id, step_id, probe_results)
            elif outcome == "cancelled":
                self.cancelled.emit(operation_id, step_id)
            else:
                self.failed.emit(
                    operation_id,
                    step_id,
                    safe_message or _SAFE_FAILURE_MESSAGE,
                )
        elif not self._workers and self._state != _STATE_IDLE:
            self._terminal_outcomes.pop(operation_id, None)
            self._set_state(operation_id, worker.step_id, _STATE_IDLE)
        worker.deleteLater()

    def _clear_active(self, operation_id: str, step_id: str) -> None:
        if operation_id != self._active_operation_id:
            return
        self._active_operation_id = None
        self._active_step_id = None
        self._active_context = None
        self._set_state(operation_id, step_id, _STATE_IDLE)

    def _set_state(self, operation_id: str, step_id: str, state: str) -> None:
        self._state = state
        self.state_changed.emit(operation_id, step_id, state)
