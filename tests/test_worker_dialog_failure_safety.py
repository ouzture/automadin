from __future__ import annotations

import logging
import threading
import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from automadin.engine.adb_engine import AdbDeviceEngine
from automadin.models.device import DeviceType
from automadin.services.database import DatabaseService
from automadin.ui.adb_connect_dialog import AdbConnectDialog, _AdbConnectThread
from automadin.ui.launcher.device_picker_dialog import (
    DevicePickerDialog,
    _DiscoverThread,
)


@pytest.fixture
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(
    application: QApplication,
    predicate,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for dialog worker lifecycle")
        application.processEvents()
        time.sleep(0.005)
    application.processEvents()


class _EmptyHealthCache:
    @staticmethod
    def check_many(_statuses, _data_dir, cancel):
        assert not cancel()
        return {}


class _PickerDatabase:
    def __init__(self, *, release_error: BaseException | None = None) -> None:
        self.release_error = release_error
        self.release_calls = 0

    @staticmethod
    def profile_engine_specs() -> list[object]:
        return []

    @staticmethod
    def profile_display_labels() -> dict[str, str]:
        return {}

    def release_current_thread_connection(self) -> None:
        self.release_calls += 1
        if self.release_error is not None:
            raise self.release_error


class _EmptyDiscovery:
    @staticmethod
    def list_devices(_device_type: DeviceType, _profiles):
        return []


def test_adb_worker_logs_private_exception_and_emits_fixed_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingEngine:
        def __init__(self, *, adb_path: str) -> None:
            self.adb_path = adb_path

        @staticmethod
        def connect_tcp(_host: str, _port: int) -> str:
            raise RuntimeError("private adb executable output")

    caplog.set_level(logging.ERROR, logger="automadin.ui.adb_connect_dialog")
    failures: list[str] = []
    thread = _AdbConnectThread("localhost", 5555, "adb")
    thread.failed.connect(
        failures.append,
        Qt.ConnectionType.DirectConnection,
    )

    with patch(
        "automadin.ui.adb_connect_dialog.AdbDeviceEngine",
        _FailingEngine,
    ):
        thread.run()

    assert failures == [
        "ADB could not connect to this target. Check the host, port, and device "
        "network settings, then try again."
    ]
    assert "private adb executable output" not in failures[0]
    assert "private adb executable output" in caplog.text


def test_adb_start_failure_restores_reference_controls_and_safe_error(
    application: QApplication,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = DatabaseService(str(tmp_path / "adb-start.db"))
    dialog = AdbConnectDialog(database)
    warnings: list[str] = []
    caplog.set_level(logging.ERROR, logger="automadin.ui.adb_connect_dialog")
    try:
        with (
            patch.object(
                _AdbConnectThread,
                "start",
                side_effect=OSError("private thread launch detail"),
            ),
            patch(
                "automadin.ui.adb_connect_dialog.QMessageBox.warning",
                side_effect=lambda _parent, _title, message: warnings.append(message),
            ),
        ):
            dialog._connect()

        assert dialog._thread is None
        assert dialog._connect_btn.isEnabled()
        assert dialog._connect_btn.text() == "Connect"
        assert dialog._close_btn.isEnabled()
        assert warnings == [
            "ADB Connect could not start. Close this dialog, reopen it, and try "
            "again."
        ]
        assert dialog._status_label.text() == warnings[0]
        assert "private thread launch detail" not in warnings[0]
        assert "private thread launch detail" in caplog.text
    finally:
        dialog.deleteLater()
        application.processEvents()
        database.close()


def test_adb_start_raise_after_native_launch_retains_exact_worker(
    application: QApplication,
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "adb-ambiguous-start.db"))
    dialog = AdbConnectDialog(database)
    entered = threading.Event()
    release = threading.Event()
    original_start = _AdbConnectThread.start

    def blocking_connect(_engine, _host: str, _port: int) -> str:
        entered.set()
        release.wait(2)
        return "connected"

    def start_then_raise(worker) -> None:
        original_start(worker)
        assert entered.wait(1)
        raise OSError("private post-native ADB start detail")

    try:
        with (
            patch.object(AdbDeviceEngine, "connect_tcp", blocking_connect),
            patch.object(_AdbConnectThread, "start", start_then_raise),
            patch("automadin.ui.adb_connect_dialog.QMessageBox.warning"),
            patch("automadin.ui.adb_connect_dialog.QMessageBox.information"),
        ):
            dialog._connect()
            worker = dialog._thread
            assert worker is not None
            assert worker.isRunning()
            assert worker.isInterruptionRequested()
            assert not dialog._connect_btn.isEnabled()

            release.set()
            assert worker.wait(2000)
            _wait_until(application, lambda: dialog._thread is None)

        assert dialog._connect_btn.isEnabled()
        assert dialog._close_btn.isEnabled()
    finally:
        release.set()
        worker = dialog._thread
        if worker is not None:
            worker.wait(2000)
        dialog.deleteLater()
        application.processEvents()
        database.close()


def test_adb_close_is_nonblocking_and_retains_live_thread(
    application: QApplication,
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "adb-close.db"))
    dialog = AdbConnectDialog(database)
    entered = threading.Event()
    release = threading.Event()
    thread = None

    def blocking_connect(_engine, _host: str, _port: int) -> str:
        entered.set()
        release.wait(2)
        return "connected"

    try:
        with (
            patch.object(AdbDeviceEngine, "connect_tcp", blocking_connect),
            patch("automadin.ui.adb_connect_dialog.QMessageBox.information"),
        ):
            dialog._connect()
            thread = dialog._thread
            assert thread is not None
            assert entered.wait(1)

            event = QCloseEvent()
            started_at = time.monotonic()
            dialog.closeEvent(event)
            elapsed = time.monotonic() - started_at

            assert elapsed < 0.25
            assert not event.isAccepted()
            assert dialog._thread is thread
            assert thread.isRunning()

            release.set()
            assert thread.wait(2000)
            _wait_until(application, lambda: dialog._thread is None)

            assert dialog._connect_btn.isEnabled()
            assert dialog._close_btn.isEnabled()
    finally:
        release.set()
        thread = dialog._thread
        if thread is not None:
            thread.wait(2000)
        dialog.deleteLater()
        application.processEvents()
        database.close()


def test_discovery_worker_logs_details_but_emits_only_fixed_failures(
    caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    class _FailingDiscovery:
        @staticmethod
        def list_devices(device_type: DeviceType, _profiles):
            raise RuntimeError(f"private {device_type.value} discovery detail")

    database = _PickerDatabase()
    ready: list[tuple[list[object], list[tuple[str, str]]]] = []
    thread = _DiscoverThread(
        _FailingDiscovery(),  # type: ignore[arg-type]
        database,  # type: ignore[arg-type]
        _EmptyHealthCache(),  # type: ignore[arg-type]
        tmp_path,
        (DeviceType.BROWSER, DeviceType.ANDROID, DeviceType.WINDOWS),
    )
    thread.discovery_ready.connect(
        lambda items, failures: ready.append((items, failures)),
        Qt.ConnectionType.DirectConnection,
    )
    caplog.set_level(
        logging.ERROR,
        logger="automadin.ui.launcher.device_picker_dialog",
    )

    thread.run()

    assert len(ready) == 1
    items, failures = ready[0]
    assert items == []
    assert [group for group, _message in failures] == [
        "Browser",
        "Android",
        "Windows",
    ]
    assert all(
        message
        == "Discovery is unavailable. Check this device type's runtime or "
        "connection, then reopen the dialog."
        for _group, message in failures
    )
    assert all("private" not in message for _group, message in failures)
    assert "private browser discovery detail" in caplog.text
    assert "private android discovery detail" in caplog.text
    assert "private windows discovery detail" in caplog.text
    assert database.release_calls == 1


def test_discovery_database_release_failure_is_logged_and_contained(
    caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    database = _PickerDatabase(
        release_error=RuntimeError("private database release detail")
    )
    ready: list[tuple[list[object], list[tuple[str, str]]]] = []
    thread = _DiscoverThread(
        _EmptyDiscovery(),  # type: ignore[arg-type]
        database,  # type: ignore[arg-type]
        _EmptyHealthCache(),  # type: ignore[arg-type]
        tmp_path,
        (DeviceType.BROWSER, DeviceType.ANDROID, DeviceType.WINDOWS),
    )
    thread.discovery_ready.connect(
        lambda items, failures: ready.append((items, failures)),
        Qt.ConnectionType.DirectConnection,
    )
    caplog.set_level(
        logging.ERROR,
        logger="automadin.ui.launcher.device_picker_dialog",
    )

    thread.run()

    assert ready == [([], [])]
    assert database.release_calls == 1
    assert "private database release detail" in caplog.text


def test_device_picker_start_failure_rolls_back_and_shows_safe_warning(
    application: QApplication,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dialog = DevicePickerDialog(
        _EmptyDiscovery(),  # type: ignore[arg-type]
        _PickerDatabase(),  # type: ignore[arg-type]
        browser_health_cache=_EmptyHealthCache(),  # type: ignore[arg-type]
    )
    caplog.set_level(
        logging.ERROR,
        logger="automadin.ui.launcher.device_picker_dialog",
    )
    try:
        with patch.object(
            _DiscoverThread,
            "start",
            side_effect=OSError("private picker thread launch detail"),
        ):
            dialog.show()
            application.processEvents()

        assert dialog._thread is None
        assert not dialog._discovery_started
        assert dialog._stack.currentIndex() == 1
        assert dialog._warning.isVisible()
        assert (
            dialog._warning.text()
            == "Device discovery could not start. Close this dialog, reopen it, "
            "and try again."
        )
        assert "private picker thread launch detail" not in dialog._warning.text()
        assert "private picker thread launch detail" in caplog.text
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_device_picker_start_raise_after_native_launch_retains_exact_worker(
    application: QApplication,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingDiscovery:
        @staticmethod
        def list_devices(_device_type: DeviceType, _profiles):
            entered.set()
            release.wait(2)
            return []

    dialog = DevicePickerDialog(
        BlockingDiscovery(),  # type: ignore[arg-type]
        _PickerDatabase(),  # type: ignore[arg-type]
        browser_health_cache=_EmptyHealthCache(),  # type: ignore[arg-type]
    )
    original_start = _DiscoverThread.start

    def start_then_raise(worker) -> None:
        original_start(worker)
        assert entered.wait(1)
        raise OSError("private post-native picker start detail")

    try:
        with patch.object(_DiscoverThread, "start", start_then_raise):
            dialog.show()
            application.processEvents()

        worker = dialog._thread
        assert worker is not None
        assert worker.isRunning()
        assert worker.isInterruptionRequested()
        assert dialog._discovery_started
        assert "waiting for the background scan" in dialog._warning.text()

        release.set()
        assert worker.wait(2000)
        _wait_until(application, lambda: dialog._thread is None)
    finally:
        release.set()
        worker = dialog._thread
        if worker is not None:
            worker.wait(2000)
        dialog.close()
        dialog.deleteLater()
        application.processEvents()
