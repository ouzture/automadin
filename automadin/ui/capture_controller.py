"""Application-owned asynchronous screenshot operation controller."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal, Slot

from automadin.models.device import (
    AndroidDevice,
    DeviceSession,
    WindowsDevice,
)
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
from automadin.services.screen_capture import (
    CaptureError,
    CaptureFrame,
    ScreenCaptureService,
)

logger = logging.getLogger(__name__)

_SAFE_CAPTURE_START_FAILURE = (
    "The screenshot operation could not be started. Try again."
)


def _device_conflict_message(current: DeviceOperationUse) -> str:
    """Explain the live owner without exposing an operation ID or raw details."""
    return (
        f"The selected device is already being used by {current.label}. Stop that "
        "operation and try again."
    )


class CaptureBusyError(RuntimeError):
    """A second capture was requested while the shared controller was busy."""


class CaptureStartError(RuntimeError):
    """A screenshot operation could not be started safely."""


class _CaptureThread(QThread):
    """Run one typed capture task away from the Qt UI thread."""

    def __init__(
        self,
        service: ScreenCaptureService,
        *,
        session: DeviceSession | None = None,
        file_path: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if (session is None) == (file_path is None):
            raise ValueError("Exactly one capture source is required")
        self._service = service
        self._session = session
        self._file_path = file_path
        self.frame: CaptureFrame | None = None
        self.error_message: str | None = None

    def run(self) -> None:
        try:
            if self._session is not None:
                self.frame = self._service.capture_session(self._session)
            else:
                assert self._file_path is not None
                self.frame = self._service.import_file(self._file_path)
        except CaptureError as exc:
            self.error_message = str(exc).strip() or "Screenshot capture failed."
            logger.info(
                "Screenshot capture did not complete: exception=%s",
                type(exc).__name__,
            )
        except Exception:
            self.error_message = "Screenshot capture failed unexpectedly. Try again."
            logger.exception("Unexpected screenshot capture failure")


class CaptureOperationController(QObject):
    """Single-flight capture lifecycle shared by all design surfaces.

    Cancellation is intentionally logical.  It requests interruption and drops
    the eventual result, but it never terminates a QThread or an in-flight
    Playwright/PrintWindow call.  The controller retains the worker until the
    provider really returns, so browser/profile ownership remains safe.
    """

    busy_changed = Signal(bool)
    capture_succeeded = Signal(str, object)  # operation_id, CaptureFrame
    capture_failed = Signal(str, str)  # operation_id, user-readable message
    capture_cancelled = Signal(str)
    capture_finished = Signal(str)

    def __init__(
        self,
        service: ScreenCaptureService,
        parent: QObject | None = None,
        *,
        device_operation_registry: DeviceOperationLeaseRegistry | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._device_operation_registry = (
            device_operation_registry
            if device_operation_registry is not None
            else default_device_operation_registry()
        )
        self._device_operation_lease: DeviceOperationLease | None = None
        self._worker: _CaptureThread | None = None
        self._operation_id: str | None = None
        self._cancel_requested = False
        self._delivering_result = False

    @property
    def is_busy(self) -> bool:
        return (
            self._worker is not None
            or self._delivering_result
            or self._device_operation_lease is not None
        )

    @property
    def active_operation_id(self) -> str | None:
        return self._operation_id

    def is_operation_active(self, operation_id: str | None) -> bool:
        return bool(operation_id) and operation_id == self._operation_id

    def capture_session(self, session: DeviceSession) -> str:
        return self._start(session=session)

    def import_file(self, path: str | Path) -> str:
        file_path = Path(os.path.abspath(os.fspath(path)))
        return self._start(file_path=file_path)

    def cancel(self, operation_id: str | None = None) -> bool:
        worker = self._worker
        if worker is None:
            return False
        if operation_id is not None and operation_id != self._operation_id:
            return False
        self._cancel_requested = True
        worker.requestInterruption()
        return True

    def shutdown(self, wait_ms: int = 0) -> bool:
        """Request cancellation and retain the worker until it really exits."""
        worker = self._worker
        if worker is None:
            self._release_device_operation_lease(
                context="while retrying screenshot cleanup during shutdown"
            )
            if self._device_operation_lease is None:
                self.busy_changed.emit(False)
                return True
            return False
        self.cancel(self._operation_id)
        if worker.isRunning() and not worker.wait(max(0, int(wait_ms))):
            return False
        self._finalize_worker(worker)
        return self._worker is None

    def _start(
        self,
        *,
        session: DeviceSession | None = None,
        file_path: Path | None = None,
    ) -> str:
        if self._worker is not None or self._delivering_result:
            raise CaptureBusyError(
                "Another screenshot operation is still finishing. Try again shortly."
            )
        if self._device_operation_lease is not None:
            self._release_device_operation_lease(
                context="before starting the next screenshot"
            )
            if self._device_operation_lease is not None:
                raise CaptureBusyError(
                    "Screenshot cleanup is still pending. Close Automadin and try "
                    "again after the device is released."
                )
            self.busy_changed.emit(False)
        operation_id = str(uuid4())
        if session is not None and isinstance(
            getattr(session, "device", None),
            (AndroidDevice, WindowsDevice),
        ):
            try:
                self._device_operation_lease = (
                    self._device_operation_registry.acquire(
                        device_operation_key(session.device),
                        DeviceOperationUse(
                            kind="screenshot-capture",
                            label="Screenshot capture",
                            operation_id=operation_id,
                        ),
                    )
                )
            except DeviceOperationConflictError as exc:
                logger.info(
                    "Screenshot capture device conflict: current_kind=%s",
                    exc.current.kind,
                )
                raise CaptureStartError(_device_conflict_message(exc.current)) from exc
            except Exception as exc:
                logger.exception(
                    "Screenshot capture could not reserve the selected device"
                )
                raise CaptureStartError(_SAFE_CAPTURE_START_FAILURE) from exc

        worker: _CaptureThread | None = None
        busy_announced = False
        try:
            worker = _CaptureThread(
                self._service,
                session=session,
                file_path=file_path,
                parent=self,
            )
            worker.finished.connect(self._on_worker_finished)
            self._worker = worker
            self._operation_id = operation_id
            self._cancel_requested = False
            self.busy_changed.emit(True)
            busy_announced = True
            worker.start()
        except Exception as exc:
            logger.exception("Screenshot worker could not be started")
            if qthread_start_may_have_succeeded(worker):
                # A start implementation may raise after creating the native
                # thread. Keep both worker and lease until the real finish signal.
                self._cancel_requested = True
                worker.requestInterruption()
                raise CaptureStartError(_SAFE_CAPTURE_START_FAILURE) from exc
            self._worker = None
            self._operation_id = None
            self._cancel_requested = False
            self._release_device_operation_lease(
                context="during screenshot startup rollback"
            )
            if busy_announced:
                self.busy_changed.emit(False)
            if worker is not None:
                self._release_worker(worker, context="after screenshot start failure")
            raise CaptureStartError(_SAFE_CAPTURE_START_FAILURE) from exc
        return operation_id

    @Slot()
    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if isinstance(worker, _CaptureThread):
            self._finalize_worker(worker)

    def _finalize_worker(self, worker: _CaptureThread) -> None:
        if worker is not self._worker:
            return
        operation_id = self._operation_id
        if operation_id is None:
            return
        cancelled = self._cancel_requested
        frame = worker.frame
        error_message = worker.error_message
        if not cancelled and frame is not None:
            validator = getattr(self._service, "validate_frame_source", None)
            if callable(validator):
                try:
                    validator(frame)
                except CaptureError as exc:
                    frame = None
                    error_message = (
                        str(exc).strip()
                        or "The screenshot source changed. Try again."
                    )
                except Exception:
                    logger.exception("Capture source validation failed")
                    frame = None
                    error_message = (
                        "The screenshot source could not be validated. Try again."
                    )

        self._worker = None
        self._operation_id = None
        self._cancel_requested = False
        self._release_device_operation_lease(
            context="after screenshot worker completion"
        )
        self._delivering_result = True
        try:
            if cancelled:
                self.capture_cancelled.emit(operation_id)
            elif frame is not None:
                self.capture_succeeded.emit(operation_id, frame)
            else:
                self.capture_failed.emit(
                    operation_id,
                    error_message or "Screenshot capture failed.",
                )
            self.capture_finished.emit(operation_id)
        finally:
            self._delivering_result = False
            self._release_worker(worker, context="after screenshot worker completion")
            self.busy_changed.emit(self.is_busy)

    def _release_device_operation_lease(self, *, context: str) -> None:
        lease = self._device_operation_lease
        if lease is None:
            return
        try:
            lease.release()
        except BaseException:
            logger.exception("Could not release device operation lease %s", context)
        else:
            if lease is self._device_operation_lease:
                self._device_operation_lease = None

    @staticmethod
    def _release_worker(worker: _CaptureThread, *, context: str) -> None:
        try:
            worker.deleteLater()
        except Exception:
            logger.exception("Could not release capture worker %s", context)
