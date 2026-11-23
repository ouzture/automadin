from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
    DeviceType,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    ProfileLeaseSnapshot,
    ProfileUse,
)
from automadin.services.device_operation_leases import (
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    device_operation_key,
)
from automadin.services.device_choices import DeviceChoice
from automadin.ui.device_selector import (
    DeviceSelectorWidget,
    _ConnectThread,
    _DisconnectThread,
    _DiscoverThread,
    _DiscoveryOutcome,
)


def test_connected_companion_label_survives_late_discovery_result() -> None:
    selector = _selector()
    selector._type_combo.setCurrentIndex(selector._type_combo.findData(DeviceType.BROWSER))
    companion = BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.MSEDGE,
            companion_client_id="client-1",
            companion_target_id="7:9",
            companion_title="Session timeout",
        )
    )
    selector._device_combo.addItem(companion.name, companion)
    selector._current_session = DeviceSession(device=companion)
    selector._show_connected()

    assert selector._device_combo.currentText() == (
        "CONNECTED · Browser Companion · MS Edge · Session timeout"
    )

    selector._on_discovered(
        _DiscoveryOutcome(
            DeviceType.BROWSER,
            [_browser := BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.MSEDGE))],
            choices=[DeviceChoice(_browser, None)],
        )
    )

    assert selector._device_combo.currentData() is companion
    assert selector._device_combo.currentText().startswith(
        "CONNECTED · Browser Companion ·"
    )
    selector.deleteLater()


class _Db:
    db_path = Path("selector-safety.db")

    @staticmethod
    def profile_engine_specs() -> list[tuple[str, str]]:
        return []

    @staticmethod
    def profile_display_labels() -> dict[str, str]:
        return {}

    @staticmethod
    def release_current_thread_connection() -> None:
        return None


class _Discovery:
    @staticmethod
    def list_devices(_device_type, _profiles):
        return []


class _Service:
    discovery = _Discovery()

    @staticmethod
    def get_session_by_device(_device_id: str):
        return None

    @staticmethod
    def create_session(_device):
        return DeviceSession(device=_device)

    @staticmethod
    def close_session(_session_id: str) -> None:
        return None


@pytest.fixture(scope="module", autouse=True)
def _application():
    return QApplication.instance() or QApplication([])


def _selector(
    service: object | None = None,
    db: object | None = None,
    registry: DeviceOperationLeaseRegistry | None = None,
):
    selector = DeviceSelectorWidget(  # type: ignore[arg-type]
        service or _Service(),
        db or _Db(),
        device_operation_registry=registry,
    )
    selector._type_combo.setCurrentIndex(selector._type_combo.findData(DeviceType.ANDROID))
    return selector


def _add_android_device(selector: DeviceSelectorWidget) -> AndroidDevice:
    device = AndroidDevice(device_id="safe-device", model="Safe Device")
    selector._device_combo.addItem(device.name, device)
    selector._device_combo.setCurrentIndex(0)
    return device


def test_connect_worker_logs_traceback_but_emits_fixed_safe_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private connection internals"

    class Service(_Service):
        @staticmethod
        def create_session(_device):
            raise RuntimeError(secret)

    worker = _ConnectThread(Service(), AndroidDevice(device_id="failed"))
    failures: list[object] = []
    worker.failed.connect(failures.append)

    with caplog.at_level(logging.ERROR):
        worker.run()

    assert len(failures) == 1
    failure = failures[0]
    assert secret not in str(failure)
    assert "could not connect" in failure.message.lower()  # type: ignore[attr-defined]
    assert secret in caplog.text
    assert "Traceback" in caplog.text


def test_profile_conflict_is_actionable_without_raw_profile_internals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile_id = "private-profile-id"
    current = ProfileLeaseSnapshot(
        profile_id,
        1,
        "owner",
        ProfileUse("browser", "Design browser", "private-operation-id"),
    )
    conflict = BrowserProfileInUseError(
        current,
        SimpleNamespace(
            profile_id=profile_id,
            mode="owner",
            use=ProfileUse("browser", "new connection", "request-id"),
            owner_generation=None,
        ),
    )

    class Service(_Service):
        @staticmethod
        def create_session(_device):
            raise conflict

    worker = _ConnectThread(Service(), AndroidDevice(device_id="failed"))
    failures: list[object] = []
    worker.failed.connect(failures.append)

    with caplog.at_level(logging.ERROR):
        worker.run()

    failure = failures[0]
    visible = f"{failure.title}\n{failure.message}\n{failure.hint}"  # type: ignore[attr-defined]
    assert "Design browser" in visible
    assert "Stop that browser activity" in visible
    assert profile_id not in visible
    assert "private-operation-id" not in visible
    assert profile_id in caplog.text
    assert "Traceback" in caplog.text


def test_discovery_worker_sanitizes_failure_and_contains_db_release_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Discovery:
        @staticmethod
        def list_devices(_device_type, _profiles):
            raise RuntimeError("private discovery internals")

    class Service(_Service):
        discovery = Discovery()

    class Db(_Db):
        @staticmethod
        def release_current_thread_connection() -> None:
            raise RuntimeError("private release internals")

    worker = _DiscoverThread(
        Service(),  # type: ignore[arg-type]
        DeviceType.ANDROID,
        Db(),  # type: ignore[arg-type]
        SimpleNamespace(),
    )
    failures: list[tuple[DeviceType, str]] = []
    worker.failed.connect(lambda device_type, message: failures.append((device_type, message)))

    with caplog.at_level(logging.ERROR):
        worker.run()

    assert len(failures) == 1
    assert failures[0][0] is DeviceType.ANDROID
    assert "private" not in failures[0][1]
    assert "could not discover" in failures[0][1].lower()
    assert "private discovery internals" in caplog.text
    assert "private release internals" in caplog.text
    assert caplog.text.count("Traceback") >= 2


def test_discovery_start_failure_rolls_back_controls_and_reference(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    selector = _selector()

    def fail_start(_worker) -> None:
        raise RuntimeError("private discovery startup internals")

    monkeypatch.setattr(_DiscoverThread, "start", fail_start)
    with (
        caplog.at_level(logging.ERROR),
        patch("automadin.ui.device_selector.QMessageBox.warning") as warning,
    ):
        selector._discover_devices()

    assert selector._discover_thread is None
    assert selector._type_combo.isEnabled()
    assert selector._device_combo.isEnabled()
    assert selector._refresh_btn.isEnabled()
    assert selector._refresh_btn.text() == "Refresh"
    assert "private" not in selector._status_label.text()
    assert warning.call_args.args[2] == (
        "Device discovery could not start. Review Diagnostics, then try again."
    )
    assert "private discovery startup internals" in caplog.text
    selector.deleteLater()


def test_connect_start_failure_rolls_back_controls_and_reference(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    selector = _selector()
    _add_android_device(selector)

    def fail_start(_worker) -> None:
        raise RuntimeError("private connection startup internals")

    monkeypatch.setattr(_ConnectThread, "start", fail_start)
    with (
        caplog.at_level(logging.ERROR),
        patch("automadin.ui.device_selector.QMessageBox.critical") as critical,
    ):
        selector._connect()

    assert selector._connect_thread is None
    assert selector._type_combo.isEnabled()
    assert selector._device_combo.isEnabled()
    assert selector._refresh_btn.isEnabled()
    assert selector._connect_btn.text() == "Connect"
    assert selector._connect_btn.isEnabled()
    assert "private" not in selector._status_label.text()
    assert critical.call_args.args[2] == (
        "Device connection could not start. Review Diagnostics, then try again."
    )
    assert "private connection startup internals" in caplog.text
    selector.deleteLater()


def test_discovery_reference_is_retained_if_start_raises_after_native_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()

    class Discovery:
        @staticmethod
        def list_devices(_device_type, _profiles):
            entered.set()
            release.wait(2)
            return []

    class Service(_Service):
        discovery = Discovery()

    selector = _selector(Service())
    original_start = _DiscoverThread.start

    def start_then_raise(worker) -> None:
        original_start(worker)
        assert entered.wait(1)
        raise RuntimeError("ambiguous native start")

    monkeypatch.setattr(_DiscoverThread, "start", start_then_raise)
    with patch("automadin.ui.device_selector.QMessageBox.warning"):
        selector._discover_devices()

    worker = selector._discover_thread
    assert worker is not None
    assert worker.isRunning()
    assert worker.isInterruptionRequested()
    assert not selector._refresh_btn.isEnabled()

    release.set()
    assert worker.wait(2000)
    QApplication.processEvents()
    assert selector._discover_thread is None
    assert selector._refresh_btn.isEnabled()
    assert selector._type_combo.isEnabled()
    selector.deleteLater()


def test_connect_reference_is_retained_if_start_raises_after_native_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    closed: list[str] = []

    class Service(_Service):
        @staticmethod
        def create_session(device):
            entered.set()
            release.wait(2)
            return DeviceSession(device=device)

        @staticmethod
        def close_session(session_id: str) -> None:
            closed.append(session_id)

    selector = _selector(Service())
    _add_android_device(selector)
    original_start = _ConnectThread.start

    def start_then_raise(worker) -> None:
        original_start(worker)
        assert entered.wait(1)
        raise RuntimeError("ambiguous native start")

    monkeypatch.setattr(_ConnectThread, "start", start_then_raise)
    with patch("automadin.ui.device_selector.QMessageBox.critical"):
        selector._connect()

    worker = selector._connect_thread
    assert worker is not None
    assert worker.isRunning()
    assert worker.isInterruptionRequested()
    assert not selector._connect_btn.isEnabled()

    release.set()
    assert worker.wait(2000)
    QApplication.processEvents()
    assert selector._connect_thread is None
    assert selector.get_selected_session() is None
    assert selector._connect_btn.isEnabled()
    assert closed
    selector.deleteLater()


def test_disconnect_returns_promptly_holds_exact_lease_and_ignores_duplicate() -> None:
    entered = Event()
    release = Event()
    calls: list[str] = []

    class Service(_Service):
        @staticmethod
        def close_session(session_id: str) -> None:
            calls.append(session_id)
            entered.set()
            release.wait(2)

    registry = DeviceOperationLeaseRegistry()
    selector = _selector(Service(), registry=registry)
    device = _add_android_device(selector)
    session = DeviceSession(device=device)
    selector._current_session = session
    selector._show_connected()
    disconnected: list[bool] = []
    selector.session_disconnected.connect(lambda: disconnected.append(True))

    started = time.monotonic()
    selector._disconnect()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert entered.wait(1)
    worker = selector._disconnect_thread
    assert worker is not None
    assert selector.get_selected_session() is session
    assert selector._disconnect_btn.text() == "Disconnecting..."
    assert not selector._disconnect_btn.isEnabled()
    assert registry.snapshot(device_operation_key(device)) is not None

    selector._disconnect()
    assert calls == [session.session_id]

    release.set()
    assert worker.wait(2000)
    QApplication.processEvents()
    assert selector._disconnect_thread is None
    assert selector.get_selected_session() is None
    assert registry.snapshot(device_operation_key(device)) is None
    assert disconnected == [True]
    selector.deleteLater()


def test_disconnect_conflict_blocks_close_and_keeps_session() -> None:
    calls: list[str] = []

    class Service(_Service):
        @staticmethod
        def close_session(session_id: str) -> None:
            calls.append(session_id)

    registry = DeviceOperationLeaseRegistry()
    selector = _selector(Service(), registry=registry)
    device = _add_android_device(selector)
    session = DeviceSession(device=device)
    selector._current_session = session
    selector._show_connected()
    existing = registry.acquire(
        device_operation_key(device),
        DeviceOperationUse("flow-run", "Active flow", "run-1"),
    )

    with patch("automadin.ui.device_selector.QMessageBox.warning") as warning:
        selector._disconnect()

    assert calls == []
    assert selector._disconnect_thread is None
    assert selector.get_selected_session() is session
    assert selector._disconnect_btn.isEnabled()
    assert warning.call_args.args[1] == "Device Is Busy"
    assert "busy with another Automadin operation" in warning.call_args.args[2]
    assert registry.snapshot(device_operation_key(device)) is not None
    existing.release()
    selector.deleteLater()


def test_disconnect_failure_logs_traceback_releases_lease_and_retains_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Service(_Service):
        @staticmethod
        def close_session(_session_id: str) -> None:
            raise RuntimeError("private close internals")

    registry = DeviceOperationLeaseRegistry()
    selector = _selector(Service(), registry=registry)
    device = _add_android_device(selector)
    session = DeviceSession(device=device)
    selector._current_session = session
    selector._show_connected()

    with (
        caplog.at_level(logging.ERROR),
        patch("automadin.ui.device_selector.QMessageBox.critical") as critical,
    ):
        selector._disconnect()
        worker = selector._disconnect_thread
        assert worker is not None
        assert worker.wait(2000)
        QApplication.processEvents()

    assert selector._disconnect_thread is None
    assert selector.get_selected_session() is session
    assert registry.snapshot(device_operation_key(device)) is None
    assert selector._disconnect_btn.text() == "Disconnect"
    assert selector._disconnect_btn.isEnabled()
    assert "private" not in critical.call_args.args[2]
    assert "remains connected" in critical.call_args.args[2]
    assert "private close internals" in caplog.text
    assert "Traceback" in caplog.text
    selector.deleteLater()


def test_disconnect_start_failure_rolls_back_lease_controls_and_reference(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = DeviceOperationLeaseRegistry()
    selector = _selector(registry=registry)
    device = _add_android_device(selector)
    session = DeviceSession(device=device)
    selector._current_session = session
    selector._show_connected()

    def fail_start(_worker) -> None:
        raise RuntimeError("private disconnect startup internals")

    monkeypatch.setattr(_DisconnectThread, "start", fail_start)
    with (
        caplog.at_level(logging.ERROR),
        patch("automadin.ui.device_selector.QMessageBox.critical") as critical,
    ):
        selector._disconnect()

    assert selector._disconnect_thread is None
    assert selector.get_selected_session() is session
    assert registry.snapshot(device_operation_key(device)) is None
    assert selector._disconnect_btn.text() == "Disconnect"
    assert selector._disconnect_btn.isEnabled()
    assert "private" not in critical.call_args.args[2]
    assert "private disconnect startup internals" in caplog.text
    selector.deleteLater()


def test_disconnect_reference_is_retained_if_start_raises_after_native_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()

    class Service(_Service):
        @staticmethod
        def close_session(_session_id: str) -> None:
            entered.set()
            release.wait(2)

    registry = DeviceOperationLeaseRegistry()
    selector = _selector(Service(), registry=registry)
    device = _add_android_device(selector)
    session = DeviceSession(device=device)
    selector._current_session = session
    selector._show_connected()
    original_start = _DisconnectThread.start

    def start_then_raise(worker) -> None:
        original_start(worker)
        assert entered.wait(1)
        raise RuntimeError("ambiguous native disconnect start")

    monkeypatch.setattr(_DisconnectThread, "start", start_then_raise)
    with patch("automadin.ui.device_selector.QMessageBox.critical"):
        selector._disconnect()

    worker = selector._disconnect_thread
    assert worker is not None
    assert worker.isRunning()
    assert registry.snapshot(device_operation_key(device)) is not None
    assert not selector._disconnect_btn.isEnabled()

    release.set()
    assert worker.wait(2000)
    QApplication.processEvents()
    assert selector._disconnect_thread is None
    assert selector.get_selected_session() is None
    assert registry.snapshot(device_operation_key(device)) is None
    selector.deleteLater()


def test_shutdown_during_disconnect_is_nonblocking_and_restores_after_finish() -> None:
    entered = Event()
    release = Event()

    class Service(_Service):
        @staticmethod
        def close_session(_session_id: str) -> None:
            entered.set()
            release.wait(2)

    registry = DeviceOperationLeaseRegistry()
    selector = _selector(Service(), registry=registry)
    device = _add_android_device(selector)
    selector._current_session = DeviceSession(device=device)
    selector._show_connected()
    selector._disconnect()
    assert entered.wait(1)
    worker = selector._disconnect_thread
    assert worker is not None

    started = time.monotonic()
    assert selector.shutdown(wait_ms=5000) is False
    assert time.monotonic() - started < 0.25
    assert selector._disconnect_thread is worker
    assert worker.isInterruptionRequested()
    assert not selector.isEnabled()

    release.set()
    assert worker.wait(2000)
    QApplication.processEvents()
    assert selector._disconnect_thread is None
    assert selector.get_selected_session() is None
    assert selector.isEnabled()
    assert not selector._shutting_down
    assert "continue or close safely" in selector._status_label.text()
    assert registry.snapshot(device_operation_key(device)) is None
    selector.deleteLater()


def test_failed_disconnect_during_refused_close_restores_connected_failure() -> None:
    entered = Event()
    release = Event()

    class Service(_Service):
        @staticmethod
        def close_session(_session_id: str) -> None:
            entered.set()
            release.wait(2)
            raise RuntimeError("private shutdown close failure")

    registry = DeviceOperationLeaseRegistry()
    selector = _selector(Service(), registry=registry)
    device = _add_android_device(selector)
    session = DeviceSession(device=device)
    selector._current_session = session
    selector._show_connected()
    selector._disconnect()
    assert entered.wait(1)
    worker = selector._disconnect_thread
    assert worker is not None
    assert selector.shutdown() is False

    with patch("automadin.ui.device_selector.QMessageBox.critical") as critical:
        release.set()
        assert worker.wait(2000)
        QApplication.processEvents()

    assert selector._disconnect_thread is None
    assert selector.get_selected_session() is session
    assert selector.isEnabled()
    assert selector._disconnect_btn.isEnabled()
    assert "remains connected" in selector._status_label.text()
    assert "private" not in critical.call_args.args[2]
    assert registry.snapshot(device_operation_key(device)) is None
    selector.deleteLater()


def test_stale_disconnect_outcome_cannot_clear_session_or_current_worker() -> None:
    selector = _selector()
    device = _add_android_device(selector)
    session = DeviceSession(device=device)
    selector._current_session = session
    selector._show_connected()
    stale = _DisconnectThread(_Service(), session, None, selector)  # type: ignore[arg-type]
    current = _DisconnectThread(_Service(), session, None, selector)  # type: ignore[arg-type]
    selector._disconnect_thread = current
    stale.succeeded.connect(selector._on_disconnected)
    stale.finished.connect(selector._on_disconnect_thread_finished)

    stale.succeeded.emit(session)
    stale.finished.emit()

    assert selector.get_selected_session() is session
    assert selector._disconnect_thread is current
    selector._disconnect_thread = None
    selector.deleteLater()


def test_abort_shutdown_restores_idle_selector_after_later_stage_refuses() -> None:
    selector = _selector()

    assert selector.shutdown()
    assert selector._shutting_down
    assert not selector.isEnabled()

    assert selector.abort_shutdown()
    assert not selector._shutting_down
    assert selector.isEnabled()
    assert selector._refresh_btn.text() == "Refresh"
    assert selector._connect_btn.text() == "Connect"

    selector.deleteLater()
