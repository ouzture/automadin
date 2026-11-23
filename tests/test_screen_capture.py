from __future__ import annotations

import io
import inspect
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
    WindowsDevice,
)
from automadin.services.screen_capture import (
    CaptureCoordinateSpace,
    CaptureFrame,
    CaptureImageTooLargeError,
    CaptureProviderError,
    CaptureSource,
    CaptureSourceChangedError,
    CaptureSourceKind,
    CaptureSourceUnavailableError,
    InvalidCaptureImageError,
    ScreenCaptureService,
)
from automadin.services.device_operation_leases import (
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    device_operation_key,
)
from automadin.services.database import DatabaseService
from automadin.ui.capture_controller import (
    CaptureBusyError,
    CaptureOperationController,
    CaptureStartError,
)
from automadin.ui.management.management_view import ManagementView
from automadin.ui.utility.utility_view import UtilityView
from automadin.ui.visual_rule_dialog import VisualRuleDialog
from automadin.ui.widgets.interactive_screenshot import InteractiveScreenshotWidget


def _image_bytes(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (8, 6),
    color: tuple[int, int, int] = (20, 80, 140),
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        QApplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for Qt operation")
        time.sleep(0.005)
    QApplication.processEvents()


class _SessionPort:
    def __init__(self, session: DeviceSession, payload: bytes) -> None:
        self.sessions = {session.session_id: session}
        self.payload = payload
        self.calls: list[DeviceSession] = []
        self.after_capture = None

    def get_session(self, session_id: str) -> DeviceSession | None:
        return self.sessions.get(session_id)

    def take_screenshot(self, session: DeviceSession) -> bytes:
        self.calls.append(session)
        if self.after_capture is not None:
            self.after_capture()
        return self.payload


@pytest.mark.parametrize(
    ("device", "kind", "coordinate_space"),
    [
        (
            BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM)),
            CaptureSourceKind.BROWSER,
            CaptureCoordinateSpace.BROWSER_VIEWPORT,
        ),
        (
            AndroidDevice("emulator-5554", model="Pixel"),
            CaptureSourceKind.ANDROID,
            CaptureCoordinateSpace.ANDROID_DISPLAY,
        ),
        (
            WindowsDevice(123, "Example"),
            CaptureSourceKind.WINDOWS,
            CaptureCoordinateSpace.WINDOWS_WINDOW,
        ),
    ],
)
def test_connected_capture_returns_canonical_frame_with_stable_source(
    device,
    kind: CaptureSourceKind,
    coordinate_space: CaptureCoordinateSpace,
) -> None:
    session = DeviceSession(device=device)
    port = _SessionPort(session, _image_bytes("JPEG", size=(11, 7)))
    service = ScreenCaptureService(port)

    frame = service.capture_session(session)

    assert frame.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert (frame.width, frame.height) == (11, 7)
    assert frame.source.kind is kind
    assert frame.source.coordinate_space is coordinate_space
    assert frame.source.identifier == device.id
    assert frame.source.label == device.name
    assert frame.source.session_id == session.session_id
    assert frame.source.session_token == id(session)
    assert port.calls == [session]


def test_connected_capture_rejects_inactive_or_replaced_session() -> None:
    session = DeviceSession(
        device=AndroidDevice("emulator-5554"),
    )
    port = _SessionPort(session, _image_bytes())
    service = ScreenCaptureService(port)
    port.sessions.clear()

    with pytest.raises(CaptureSourceUnavailableError, match="no longer connected"):
        service.capture_session(session)

    port.sessions[session.session_id] = DeviceSession(
        session_id=session.session_id,
        device=session.device,
    )
    with pytest.raises(CaptureSourceUnavailableError, match="no longer connected"):
        service.capture_session(session)


def test_connected_capture_discards_result_when_target_changes_in_flight() -> None:
    session = DeviceSession(device=WindowsDevice(123, "Before"))
    port = _SessionPort(session, _image_bytes())
    service = ScreenCaptureService(port)

    def change_target() -> None:
        session.device = WindowsDevice(456, "After")

    port.after_capture = change_target

    with pytest.raises(CaptureSourceChangedError, match="changed during capture"):
        service.capture_session(session)


@pytest.mark.parametrize("payload", [b"", b"not an image"])
def test_connected_capture_rejects_empty_or_invalid_provider_bytes(
    payload: bytes,
) -> None:
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    service = ScreenCaptureService(_SessionPort(session, payload))

    with pytest.raises(InvalidCaptureImageError):
        service.capture_session(session)


def test_connected_capture_redacts_provider_exception_secrets(caplog) -> None:
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    port = _SessionPort(session, _image_bytes())
    secret = r"SENTINEL_SECRET C:\Users\private\device-token"

    def fail_provider() -> None:
        raise RuntimeError(secret)

    port.take_screenshot = lambda _session: fail_provider()  # type: ignore[method-assign]
    service = ScreenCaptureService(port)

    with pytest.raises(CaptureProviderError) as error:
        service.capture_session(session)

    assert "SENTINEL_SECRET" not in str(error.value)
    assert "device-token" not in str(error.value)
    assert "SENTINEL_SECRET" not in caplog.text
    assert "device-token" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_file_import_is_bounded_normalized_and_does_not_expose_parent_path(
    tmp_path: Path,
) -> None:
    session = DeviceSession(device=AndroidDevice("unused"))
    source = tmp_path / "private-parent" / "photo.jpg"
    source.parent.mkdir()
    source.write_bytes(_image_bytes("JPEG", size=(13, 9)))
    service = ScreenCaptureService(_SessionPort(session, _image_bytes()))

    frame = service.import_file(source)

    assert frame.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert (frame.width, frame.height) == (13, 9)
    assert frame.source == CaptureSource(
        kind=CaptureSourceKind.FILE,
        identifier="photo.jpg",
        label="photo.jpg",
        coordinate_space=CaptureCoordinateSpace.OFFLINE_IMAGE,
    )
    assert str(source.parent) not in repr(frame)


@pytest.mark.skipif(os.name != "nt", reason="Windows stat semantics")
def test_windows_file_identity_uses_stable_birthtime_not_change_time() -> None:
    first = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_size=3,
        st_mtime_ns=4,
        st_ctime_ns=5,
        st_birthtime_ns=6,
    )
    second = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_size=3,
        st_mtime_ns=4,
        st_ctime_ns=999,
        st_birthtime_ns=6,
    )

    assert ScreenCaptureService._file_identity(first) == (
        ScreenCaptureService._file_identity(second)
    )


def test_file_import_rejects_missing_directory_and_encoded_size(
    tmp_path: Path,
) -> None:
    session = DeviceSession(device=AndroidDevice("unused"))
    service = ScreenCaptureService(
        _SessionPort(session, _image_bytes()),
        max_encoded_bytes=16,
    )

    with pytest.raises(CaptureSourceUnavailableError, match="no longer available"):
        service.import_file(tmp_path / "missing.png")
    with pytest.raises(CaptureSourceUnavailableError, match="regular file"):
        service.import_file(tmp_path)

    source = tmp_path / "large.png"
    source.write_bytes(_image_bytes())
    with pytest.raises(CaptureImageTooLargeError, match="too large"):
        service.import_file(source)


def test_file_import_rejects_symbolic_links(tmp_path: Path) -> None:
    target = tmp_path / "target.png"
    target.write_bytes(_image_bytes())
    link = tmp_path / "linked.png"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {type(exc).__name__}")

    service = ScreenCaptureService(None)
    with pytest.raises(CaptureSourceUnavailableError, match="direct regular file"):
        service.import_file(link)


@pytest.mark.skipif(os.name != "nt", reason="Windows network-path policy")
def test_file_import_rejects_unc_path_before_file_io() -> None:
    service = ScreenCaptureService(None)

    with pytest.raises(CaptureSourceUnavailableError, match="local drive"):
        service.import_file(r"\\server.invalid\share\private.png")


def test_capture_rejects_excessive_decoded_pixels() -> None:
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    service = ScreenCaptureService(
        _SessionPort(session, _image_bytes(size=(10, 10))),
        max_pixels=99,
    )

    with pytest.raises(CaptureImageTooLargeError, match="too many pixels"):
        service.capture_session(session)


@pytest.mark.parametrize(
    ("kwargs", "expected_message"),
    [
        ({"max_dimension": 9}, "dimensions are too large"),
        ({"max_decoded_bytes": 399}, "decoded memory"),
    ],
)
def test_capture_rejects_per_axis_and_decoded_byte_budgets(
    kwargs: dict[str, int],
    expected_message: str,
) -> None:
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    service = ScreenCaptureService(
        _SessionPort(session, _image_bytes(size=(10, 10))),
        **kwargs,
    )

    with pytest.raises(CaptureImageTooLargeError, match=expected_message):
        service.capture_session(session)


class _BlockingNormalizeService(ScreenCaptureService):
    def __init__(self, port: _SessionPort) -> None:
        super().__init__(port)
        self.normalize_entered = threading.Event()
        self.normalize_release = threading.Event()

    def _frame_from_bytes(
        self,
        payload: object,
        source: CaptureSource,
    ) -> CaptureFrame:
        self.normalize_entered.set()
        self.normalize_release.wait(2)
        return super()._frame_from_bytes(payload, source)


def test_connected_capture_rejects_exact_session_replacement_during_normalization() -> None:
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    port = _SessionPort(session, _image_bytes())
    service = _BlockingNormalizeService(port)
    errors: list[BaseException] = []

    def capture() -> None:
        try:
            service.capture_session(session)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=capture)
    worker.start()
    assert service.normalize_entered.wait(1)
    port.sessions[session.session_id] = DeviceSession(
        session_id=session.session_id,
        device=session.device,
    )
    service.normalize_release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], CaptureSourceUnavailableError)


_FRAME = CaptureFrame(
    png_bytes=_image_bytes(),
    width=8,
    height=6,
    source=CaptureSource(
        kind=CaptureSourceKind.ANDROID,
        identifier="emulator-5554",
        label="Pixel",
        coordinate_space=CaptureCoordinateSpace.ANDROID_DISPLAY,
        session_id="session",
    ),
)


class _BlockingCaptureService:
    def __init__(self, *, frame: CaptureFrame = _FRAME) -> None:
        self.frame = frame
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread_ids: list[int] = []
        self.sessions: list[object] = []
        self.paths: list[Path] = []

    def _capture(self) -> CaptureFrame:
        self.thread_ids.append(threading.get_ident())
        self.entered.set()
        self.release.wait(2)
        return self.frame

    def capture_session(self, session: object) -> CaptureFrame:
        self.sessions.append(session)
        return self._capture()

    def import_file(self, path: Path) -> CaptureFrame:
        self.paths.append(path)
        return self._capture()


class _TrackingDeviceOperationRegistry:
    class Lease:
        def __init__(self) -> None:
            self.release_calls = 0

        def release(self) -> bool:
            self.release_calls += 1
            return True

    def __init__(self) -> None:
        self.acquire_calls: list[tuple[str, DeviceOperationUse]] = []
        self.lease = self.Lease()

    def acquire(self, key: str, use: DeviceOperationUse) -> Lease:
        self.acquire_calls.append((key, use))
        return self.lease


def test_capture_controller_runs_off_ui_thread_and_emits_one_success() -> None:
    _application()
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    session = object()
    successes: list[tuple[str, CaptureFrame]] = []
    finished: list[str] = []
    busy: list[bool] = []
    controller.capture_succeeded.connect(
        lambda operation_id, frame: successes.append((operation_id, frame))
    )
    controller.capture_finished.connect(finished.append)
    controller.busy_changed.connect(busy.append)
    main_thread_id = threading.get_ident()

    operation_id = controller.capture_session(session)  # type: ignore[arg-type]
    assert service.entered.wait(1.0)
    assert controller.is_busy
    with pytest.raises(CaptureBusyError, match="still finishing"):
        controller.import_file("second.png")

    service.release.set()
    _wait_until(lambda: not controller.is_busy)

    assert successes == [(operation_id, _FRAME)]
    assert finished == [operation_id]
    assert busy == [True, False]
    assert service.sessions == [session]
    assert service.thread_ids and service.thread_ids[0] != main_thread_id


@pytest.mark.parametrize(
    "device",
    (
        AndroidDevice("emulator-5554"),
        WindowsDevice(123, "Calculator"),
    ),
)
def test_connected_capture_holds_device_lease_until_actual_worker_finish(
    device: AndroidDevice | WindowsDevice,
) -> None:
    _application()
    service = _BlockingCaptureService()
    registry = _TrackingDeviceOperationRegistry()
    controller = CaptureOperationController(
        service,  # type: ignore[arg-type]
        device_operation_registry=registry,  # type: ignore[arg-type]
    )
    session = DeviceSession(device=device)

    controller.capture_session(session)
    assert service.entered.wait(1)

    assert registry.acquire_calls[0][0] == device_operation_key(device)
    assert registry.acquire_calls[0][1].kind == "screenshot-capture"
    assert registry.lease.release_calls == 0

    service.release.set()
    _wait_until(lambda: not controller.is_busy)
    QApplication.processEvents()

    assert registry.lease.release_calls == 1


def test_connected_capture_fails_immediately_on_device_conflict() -> None:
    _application()
    registry = DeviceOperationLeaseRegistry()
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    existing = registry.acquire(
        device_operation_key(session.device),
        DeviceOperationUse(
            kind="flow-run",
            label="Flow run: existing",
            operation_id="existing-operation",
        ),
    )
    controller = CaptureOperationController(
        _BlockingCaptureService(),  # type: ignore[arg-type]
        device_operation_registry=registry,
    )
    try:
        with pytest.raises(CaptureStartError) as error:
            controller.capture_session(session)

        assert str(error.value) == (
            "The selected device is already being used by Flow run: existing. "
            "Stop that operation and try again."
        )
        assert not controller.is_busy
        assert registry.active_count == 1
    finally:
        existing.release()


def test_vision_capture_start_conflict_restores_idle_button_state(
    tmp_path: Path,
) -> None:
    _application()
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    registry = DeviceOperationLeaseRegistry()
    existing = registry.acquire(
        device_operation_key(session.device),
        DeviceOperationUse(
            kind="flow-run",
            label="Flow run: existing",
            operation_id="existing",
        ),
    )
    controller = CaptureOperationController(
        ScreenCaptureService(None),
        device_operation_registry=registry,
    )
    selector = _MutableSelector(session)
    database = DatabaseService(str(tmp_path / "vision-capture-conflict.db"))
    view = UtilityView(
        database,
        None,  # type: ignore[arg-type]
        selector,
        capture_controller=controller,
    )
    try:
        view._capture_screenshot()

        assert not controller.is_busy
        assert view._capture_operation_id is None
        assert view._capture_btn.isEnabled()
        assert "Flow run: existing" in view._match_label.text()
    finally:
        existing.release()
        controller.shutdown(1000)
        view.close()
        view.deleteLater()
        database.close()


def test_vision_crop_controls_use_responsive_grid_without_horizontal_clipping(
    tmp_path: Path,
) -> None:
    _application()
    database = DatabaseService(str(tmp_path / "vision-layout.db"))
    view = UtilityView(database, None)  # type: ignore[arg-type]
    try:
        view.resize(650, 500)
        view.show()
        QApplication.processEvents()

        layout = view._crop_bar.layout()
        assert layout is not None
        assert layout.count() >= 12
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is None or item.widget() is None:
                continue
            geometry = item.geometry()
            assert geometry.left() >= 0
            assert geometry.right() <= view._crop_bar.width()

        assert view._crop_group_combo.width() >= 100
        assert view._crop_name_edit.width() >= 90
        assert view._ocr_preprocessing_combo.width() >= 90
    finally:
        view.close()
        view.deleteLater()
        database.close()


def test_browser_capture_and_file_import_do_not_acquire_device_lease(
    tmp_path: Path,
) -> None:
    _application()
    service = _BlockingCaptureService()
    registry = _TrackingDeviceOperationRegistry()
    controller = CaptureOperationController(
        service,  # type: ignore[arg-type]
        device_operation_registry=registry,  # type: ignore[arg-type]
    )
    browser = DeviceSession(
        device=BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    )

    controller.capture_session(browser)
    assert service.entered.wait(1)
    service.release.set()
    _wait_until(lambda: not controller.is_busy)
    assert registry.acquire_calls == []

    service.entered.clear()
    service.release.clear()
    controller.import_file(tmp_path / "offline.png")
    assert service.entered.wait(1)
    service.release.set()
    _wait_until(lambda: not controller.is_busy)
    assert registry.acquire_calls == []


def test_capture_controller_logical_cancel_discards_eventual_result() -> None:
    _application()
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    successes: list[str] = []
    cancelled: list[str] = []
    controller.capture_succeeded.connect(
        lambda operation_id, _frame: successes.append(operation_id)
    )
    controller.capture_cancelled.connect(cancelled.append)

    operation_id = controller.capture_session(object())  # type: ignore[arg-type]
    assert service.entered.wait(1.0)
    assert controller.cancel("different-operation") is False
    assert controller.cancel(operation_id) is True
    service.release.set()
    _wait_until(lambda: not controller.is_busy)

    assert successes == []
    assert cancelled == [operation_id]


def test_capture_controller_shutdown_retains_live_worker_until_provider_returns() -> None:
    _application()
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    cancelled: list[str] = []
    controller.capture_cancelled.connect(cancelled.append)

    operation_id = controller.capture_session(object())  # type: ignore[arg-type]
    assert service.entered.wait(1.0)
    assert controller.shutdown(wait_ms=1) is False
    assert controller.is_operation_active(operation_id)

    service.release.set()
    assert controller.shutdown(wait_ms=1000) is True
    assert not controller.is_busy
    assert cancelled == [operation_id]


def test_capture_controller_import_uses_same_single_flight_lifecycle(
    tmp_path: Path,
) -> None:
    _application()
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    source = tmp_path / "offline.png"

    operation_id = controller.import_file(source)
    assert service.entered.wait(1.0)
    service.release.set()
    _wait_until(lambda: not controller.is_busy)

    assert service.paths == [source.resolve()]
    assert controller.is_operation_active(operation_id) is False


def test_capture_controller_makes_relative_import_path_absolute(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _application()
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    monkeypatch.chdir(tmp_path)

    controller.import_file(Path("images") / "offline.png")
    assert service.entered.wait(1)
    service.release.set()
    _wait_until(lambda: not controller.is_busy)

    assert service.paths == [(tmp_path / "images" / "offline.png").resolve()]


def test_capture_controller_validates_session_again_before_queued_delivery() -> None:
    _application()
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    port = _SessionPort(session, _image_bytes())
    service = ScreenCaptureService(port)
    controller = CaptureOperationController(service)
    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    controller.capture_succeeded.connect(
        lambda operation_id, _frame: successes.append(operation_id)
    )
    controller.capture_failed.connect(
        lambda operation_id, message: failures.append((operation_id, message))
    )

    operation_id = controller.capture_session(session)
    deadline = time.monotonic() + 2
    while controller._worker is not None and not controller._worker.isFinished():
        if time.monotonic() >= deadline:
            raise AssertionError("Capture worker did not finish")
        time.sleep(0.005)
    port.sessions[session.session_id] = DeviceSession(
        session_id=session.session_id,
        device=session.device,
    )
    _wait_until(lambda: not controller.is_busy)

    assert successes == []
    assert failures == [
        (
            operation_id,
            "The selected target changed before the screenshot could be shown. "
            "Try again.",
        )
    ]


def test_capture_controller_signal_order_blocks_reentrant_start_until_idle() -> None:
    _application()
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    events: list[object] = []
    reentrant_errors: list[BaseException] = []
    controller.busy_changed.connect(lambda busy: events.append(("busy", busy)))

    def on_success(operation_id: str, _frame: object) -> None:
        events.append(("success", operation_id))
        try:
            controller.capture_session(object())  # type: ignore[arg-type]
        except BaseException as exc:
            reentrant_errors.append(exc)

    controller.capture_succeeded.connect(on_success)
    controller.capture_finished.connect(
        lambda operation_id: events.append(("finished", operation_id))
    )

    operation_id = controller.capture_session(object())  # type: ignore[arg-type]
    assert service.entered.wait(1)
    service.release.set()
    _wait_until(lambda: not controller.is_busy)

    assert events == [
        ("busy", True),
        ("success", operation_id),
        ("finished", operation_id),
        ("busy", False),
    ]
    assert len(reentrant_errors) == 1
    assert isinstance(reentrant_errors[0], CaptureBusyError)


def test_capture_controller_recovers_when_worker_cannot_start(caplog) -> None:
    _application()
    service = _BlockingCaptureService()
    registry = _TrackingDeviceOperationRegistry()
    controller = CaptureOperationController(
        service,  # type: ignore[arg-type]
        device_operation_registry=registry,  # type: ignore[arg-type]
    )
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    busy: list[bool] = []
    controller.busy_changed.connect(busy.append)

    with patch(
        "automadin.ui.capture_controller._CaptureThread.start",
        side_effect=RuntimeError("SENTINEL_PRIVATE_START_FAILURE"),
    ):
        with caplog.at_level(
            logging.ERROR,
            logger="automadin.ui.capture_controller",
        ):
            with pytest.raises(CaptureStartError) as error:
                controller.capture_session(session)

    assert "SENTINEL" not in str(error.value)
    assert busy == [True, False]
    assert not controller.is_busy
    assert controller.active_operation_id is None
    assert registry.lease.release_calls == 1
    assert "SENTINEL_PRIVATE_START_FAILURE" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_capture_controller_logs_unexpected_worker_failure_but_emits_safe_text(
    caplog,
) -> None:
    class _UnexpectedWorkerService:
        def capture_session(self, _session: object) -> CaptureFrame:
            raise RuntimeError("SENTINEL_PRIVATE_WORKER_FAILURE")

    _application()
    controller = CaptureOperationController(
        _UnexpectedWorkerService(),  # type: ignore[arg-type]
    )
    failures: list[str] = []
    controller.capture_failed.connect(
        lambda _operation_id, message: failures.append(message)
    )

    with caplog.at_level(logging.ERROR, logger="automadin.ui.capture_controller"):
        controller.capture_session(object())  # type: ignore[arg-type]
        _wait_until(lambda: not controller.is_busy)

    assert failures == ["Screenshot capture failed unexpectedly. Try again."]
    assert "SENTINEL" not in failures[0]
    assert "SENTINEL_PRIVATE_WORKER_FAILURE" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_capture_controller_logs_unexpected_validation_failure_safely(
    caplog,
) -> None:
    class _UnexpectedValidationService(_BlockingCaptureService):
        def validate_frame_source(self, _frame: CaptureFrame) -> None:
            raise RuntimeError("SENTINEL_PRIVATE_VALIDATION_FAILURE")

    _application()
    service = _UnexpectedValidationService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    failures: list[str] = []
    controller.capture_failed.connect(
        lambda _operation_id, message: failures.append(message)
    )

    with caplog.at_level(logging.ERROR, logger="automadin.ui.capture_controller"):
        controller.import_file("offline.png")
        assert service.entered.wait(1)
        service.release.set()
        _wait_until(lambda: not controller.is_busy)

    assert failures == [
        "The screenshot source could not be validated. Try again."
    ]
    assert "SENTINEL" not in failures[0]
    assert "SENTINEL_PRIVATE_VALIDATION_FAILURE" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_visual_rule_offline_import_needs_no_connected_session(
    tmp_path: Path,
) -> None:
    _application()
    source = tmp_path / "offline.jpg"
    source.write_bytes(_image_bytes("JPEG", size=(15, 10)))
    controller = CaptureOperationController(ScreenCaptureService(None))
    dialog = VisualRuleDialog(capture_controller=controller)
    try:
        assert not dialog._capture_btn.isEnabled()
        assert dialog._import_btn.isEnabled()
        with patch(
            "automadin.ui.visual_rule_dialog.QFileDialog.getOpenFileName",
            return_value=(str(source), "Images"),
        ):
            dialog._import_screenshot()
        _wait_until(lambda: dialog._capture_operation_id is None)

        assert dialog._current_screenshot is not None
        assert dialog._current_screenshot.startswith(b"\x89PNG\r\n\x1a\n")
        assert dialog._current_image.shape[:2] == (10, 15)
    finally:
        controller.shutdown(1000)
        dialog.close()
        dialog.deleteLater()


class _MutableSelector:
    def __init__(self, session: DeviceSession) -> None:
        self.session = session

    def get_selected_session(self) -> DeviceSession:
        return self.session


def test_vision_lab_discards_capture_after_selected_target_changes(
    tmp_path: Path,
) -> None:
    _application()
    first = DeviceSession(device=AndroidDevice("first"))
    second = DeviceSession(device=AndroidDevice("second"))
    frame = CaptureFrame(
        png_bytes=_image_bytes(),
        width=8,
        height=6,
        source=CaptureSource(
            kind=CaptureSourceKind.ANDROID,
            identifier="first",
            label="first",
            coordinate_space=CaptureCoordinateSpace.ANDROID_DISPLAY,
            session_id=first.session_id,
        ),
    )
    service = _BlockingCaptureService(frame=frame)
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    selector = _MutableSelector(first)
    database = DatabaseService(str(tmp_path / "vision.db"))
    view = UtilityView(
        database,
        None,  # type: ignore[arg-type]
        selector,
        capture_controller=controller,
    )
    try:
        view._capture_screenshot()
        assert service.entered.wait(1.0)
        selector.session = second
        service.release.set()
        _wait_until(lambda: not controller.is_busy)

        assert view._current_screenshot is None
        assert "selected target changed" in view._screenshot_widget.text()
    finally:
        service.release.set()
        controller.shutdown(1000)
        view.close()
        view.deleteLater()
        database.close()


def test_vision_lab_keeps_prior_visible_screenshot_after_failure_and_target_change(
    tmp_path: Path,
) -> None:
    _application()
    first = DeviceSession(device=AndroidDevice("first"))
    second = DeviceSession(device=AndroidDevice("second"))
    selector = _MutableSelector(first)
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    database = DatabaseService(str(tmp_path / "vision-preserve.db"))
    view = UtilityView(
        database,
        None,  # type: ignore[arg-type]
        selector,
        capture_controller=controller,
    )
    previous = _image_bytes(size=(17, 9), color=(30, 120, 60))
    try:
        view.set_screenshot(previous)
        original_pixmap = view._screenshot_widget._original_pixmap
        assert original_pixmap is not None

        view._capture_operation_id = "failed-operation"
        view._on_capture_failed("failed-operation", "target unavailable")
        assert view._current_screenshot == previous
        assert view._screenshot_widget._original_pixmap is original_pixmap
        assert "Previous screenshot kept" in view._match_label.text()

        view._capture_operation_id = "changed-operation"
        selector.session = second
        changed_frame = CaptureFrame(
            png_bytes=_image_bytes(),
            width=8,
            height=6,
            source=CaptureSource(
                kind=CaptureSourceKind.ANDROID,
                identifier="first",
                label="first",
                coordinate_space=CaptureCoordinateSpace.ANDROID_DISPLAY,
                session_id=first.session_id,
                session_token=id(first),
            ),
        )
        view._on_capture_succeeded("changed-operation", changed_frame)
        assert view._current_screenshot == previous
        assert view._screenshot_widget._original_pixmap is original_pixmap
        assert "selected target changed" in view._match_label.text()
        assert "Previous screenshot kept" in view._match_label.text()
    finally:
        controller.shutdown(1000)
        view.close()
        view.deleteLater()
        database.close()


def test_interactive_screenshot_clear_resets_every_image_interaction_state() -> None:
    _application()
    widget = InteractiveScreenshotWidget()
    pixmap = QPixmap()
    assert pixmap.loadFromData(_image_bytes(size=(20, 10)))
    widget.set_screenshot_pixmap(pixmap)
    widget._zoom = 2.5
    widget._pan_offset = QPoint(12, 8)
    widget._panning = True
    widget._pan_start = QPoint(3, 4)
    widget._pan_start_offset = QPoint(1, 2)
    widget._is_cropping = True
    widget._crop_start = QPoint(2, 2)
    widget._crop_current = QPoint(10, 8)
    widget._crop_rect_display = QRect(2, 2, 8, 6)
    cleared: list[bool] = []
    widget.coords_cleared.connect(lambda: cleared.append(True))

    widget.clear_screenshot()

    assert widget._original_pixmap is None
    assert widget._clean_pixmap is None
    assert widget._original_size is None
    assert widget._display_pixmap is None
    assert widget._display_offset == QPoint(0, 0)
    assert widget._zoom == 1.0
    assert widget._pan_offset == QPoint(0, 0)
    assert widget._panning is False
    assert widget._pan_start is None
    assert widget._pan_start_offset is None
    assert widget._is_cropping is False
    assert widget._crop_start is None
    assert widget._crop_current is None
    assert widget._crop_rect_display is None
    assert widget.pixmap().isNull()
    assert cleared == [True]
    widget.deleteLater()


def test_visual_rule_failure_status_is_truthful_before_warning() -> None:
    _application()
    controller = CaptureOperationController(ScreenCaptureService(None))
    dialog = VisualRuleDialog(capture_controller=controller)
    assert dialog._status_label.wordWrap()
    dialog._capture_operation_id = "operation"
    observed_status: list[str] = []

    def observe_warning(*_args, **_kwargs) -> None:
        observed_status.append(dialog._status_label.text())

    try:
        with patch.object(QMessageBox, "warning", side_effect=observe_warning):
            dialog._on_capture_failed("operation", "Device disconnected.")

        assert observed_status == [
            "Screenshot operation failed: Device disconnected."
        ]
        assert dialog._status_label.text() == observed_status[0]
    finally:
        controller.shutdown(1000)
        dialog.close()
        dialog.deleteLater()


def test_visual_rule_reject_closes_immediately_while_app_controller_stays_safe() -> None:
    _application()
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    selector = _MutableSelector(session)
    dialog = VisualRuleDialog(
        device_selector=selector,
        capture_controller=controller,
    )
    dialog.show()
    QApplication.processEvents()
    try:
        dialog._capture_screenshot()
        assert service.entered.wait(1)
        operation_id = dialog._capture_operation_id
        assert operation_id is not None

        dialog.reject()
        QApplication.processEvents()

        assert not dialog.isVisible()
        assert dialog._capture_operation_id is None
        assert controller.is_operation_active(operation_id)
        assert controller.is_busy
    finally:
        service.release.set()
        controller.shutdown(1000)
        dialog.close()
        dialog.deleteLater()


def test_visual_rule_accept_cancels_its_capture_and_ignores_late_result() -> None:
    _application()
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    service = _BlockingCaptureService()
    controller = CaptureOperationController(service)  # type: ignore[arg-type]
    successes: list[str] = []
    cancelled: list[str] = []
    controller.capture_succeeded.connect(
        lambda operation_id, _frame: successes.append(operation_id)
    )
    controller.capture_cancelled.connect(cancelled.append)
    dialog = VisualRuleDialog(
        device_selector=_MutableSelector(session),
        capture_controller=controller,
    )
    dialog._name_edit.setText("Accepted Rule")
    dialog.show()
    QApplication.processEvents()
    try:
        dialog._capture_screenshot()
        assert service.entered.wait(1)
        operation_id = dialog._capture_operation_id
        assert operation_id is not None

        dialog.accept()
        QApplication.processEvents()

        assert not dialog.isVisible()
        assert dialog._capture_operation_id is None
        assert controller.is_operation_active(operation_id)
        service.release.set()
        assert controller.shutdown(1000)
        assert successes == []
        assert cancelled == [operation_id]
    finally:
        service.release.set()
        controller.shutdown(1000)
        dialog.close()
        dialog.deleteLater()


def test_management_releases_rejected_visual_rule_dialog(tmp_path: Path) -> None:
    _application()
    database = DatabaseService(str(tmp_path / "management-dialog.db"))
    released: list[bool] = []

    class _RejectedDialog:
        def __init__(self, **_kwargs) -> None:
            pass

        @staticmethod
        def exec() -> int:
            return 0

        @staticmethod
        def deleteLater() -> None:
            released.append(True)

    with patch(
        "automadin.ui.management.management_view.VisualRuleDialog",
        _RejectedDialog,
    ):
        view = ManagementView(database)
        try:
            assert view._visual_rules_panel._import_fn is not None
            view._visual_rules_panel._import_fn()
            assert released == [True]
        finally:
            view.close()
            view.deleteLater()
            database.close()


def test_capture_controller_retains_lease_after_release_exception() -> None:
    registry = DeviceOperationLeaseRegistry()
    controller = CaptureOperationController(
        ScreenCaptureService(None),
        device_operation_registry=registry,
    )
    lease = registry.acquire(
        "android:capture-lease-retry",
        DeviceOperationUse(
            "screenshot-capture",
            "Screenshot capture",
            "capture-lease-retry",
        ),
    )
    controller._device_operation_lease = lease

    with patch.object(
        registry,
        "_release",
        side_effect=RuntimeError("transient registry failure"),
    ):
        controller._release_device_operation_lease(context="test")

    assert controller._device_operation_lease is lease
    assert controller.is_busy
    assert registry.active_count == 1
    assert controller.shutdown()
    assert controller._device_operation_lease is None
    assert registry.active_count == 0
    controller.deleteLater()


@pytest.mark.parametrize(
    ("widget_type", "parent_index"),
    [
        (ManagementView, 4),
        (VisualRuleDialog, 6),
    ],
)
def test_capture_controller_remains_keyword_only_after_legacy_positional_parent(
    widget_type: type,
    parent_index: int,
) -> None:
    parameters = list(inspect.signature(widget_type.__init__).parameters.values())

    assert parameters[parent_index].name == "parent"
    assert parameters[parent_index].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    capture_parameter = next(
        parameter
        for parameter in parameters
        if parameter.name == "capture_controller"
    )
    assert capture_parameter.kind is inspect.Parameter.KEYWORD_ONLY
