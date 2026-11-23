from __future__ import annotations

import os
from threading import Event

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from automadin.models.device import AndroidDevice, DeviceSession, DeviceType
from automadin.ui.device_selector import DeviceSelectorWidget


class _Db:
    @staticmethod
    def profile_engine_specs() -> list[tuple[str, str]]:
        return []

    @staticmethod
    def profile_display_labels() -> dict[str, str]:
        return {}


class _SlowDiscovery:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0

    def list_devices(self, _device_type, _profiles):
        self.calls += 1
        self.entered.set()
        self.release.wait(2)
        return [AndroidDevice(device_id="late-device", model="Late Pixel")]


class _SlowSessionService:
    def __init__(self) -> None:
        self.discovery = _SlowDiscovery()
        self.connect_entered = Event()
        self.connect_release = Event()
        self.connect_calls = 0
        self.closed_sessions: list[str] = []

    @staticmethod
    def get_session_by_device(_device_id: str):
        return None

    def create_session(self, device: AndroidDevice) -> DeviceSession:
        self.connect_calls += 1
        self.connect_entered.set()
        self.connect_release.wait(2)
        return DeviceSession(device=device)

    def close_session(self, session_id: str) -> None:
        self.closed_sessions.append(session_id)


@pytest.fixture(scope="module", autouse=True)
def _application():
    return QApplication.instance() or QApplication([])


def _android_selector(service: _SlowSessionService) -> DeviceSelectorWidget:
    selector = DeviceSelectorWidget(service, _Db())  # type: ignore[arg-type]
    selector._type_combo.setCurrentIndex(
        selector._type_combo.findData(DeviceType.ANDROID)
    )
    return selector


def _delete_selector(selector: DeviceSelectorWidget) -> None:
    selector.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def test_shutdown_waits_for_slow_discovery_and_ignores_late_result() -> None:
    service = _SlowSessionService()
    selector = _android_selector(service)
    qt_messages: list[str] = []
    previous_handler = qInstallMessageHandler(
        lambda _kind, _context, message: qt_messages.append(message)
    )

    try:
        selector._discover_devices()
        assert service.discovery.entered.wait(1)
        thread = selector._discover_thread
        assert thread is not None

        assert selector.shutdown(wait_ms=5) is False
        assert thread.isInterruptionRequested()
        assert "Please wait" in selector._status_label.text()

        selector._discover_devices()
        assert service.discovery.calls == 1

        service.discovery.release.set()
        assert thread.wait(1000)
        QApplication.processEvents()

        assert selector._device_combo.count() == 0
        assert "late-device" not in selector._device_combo.currentText()
        assert selector.isEnabled()
        assert selector._shutting_down is False
        assert "continue or close safely" in selector._status_label.text()
        assert selector.shutdown(wait_ms=50) is True
        _delete_selector(selector)
    finally:
        service.discovery.release.set()
        if selector._discover_thread is not None:
            selector._discover_thread.wait(1000)
        qInstallMessageHandler(previous_handler)

    assert not any(
        "QThread: Destroyed while thread is still running" in message
        for message in qt_messages
    )


def test_shutdown_waits_for_slow_connect_and_suppresses_session_signal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _SlowSessionService()
    selector = _android_selector(service)
    device = AndroidDevice(device_id="connect-late", model="Late Pixel")
    selector._device_combo.addItem(device.name, device)
    selector._device_combo.setCurrentIndex(0)
    connected_sessions: list[str] = []
    selector.session_connected.connect(connected_sessions.append)
    try:
        selector._connect()
        assert service.connect_entered.wait(1)
        thread = selector._connect_thread
        assert thread is not None

        assert selector.shutdown(wait_ms=5) is False
        assert thread.isInterruptionRequested()

        selector._connect()
        assert service.connect_calls == 1

        service.connect_release.set()
        assert thread.wait(1000)
        QApplication.processEvents()

        assert selector.get_selected_session() is None
        assert connected_sessions == []
        assert len(service.closed_sessions) == 1
        assert selector.isEnabled()
        assert selector._shutting_down is False
        assert "continue or close safely" in selector._status_label.text()
        assert selector.shutdown(wait_ms=50) is True
        _delete_selector(selector)
    finally:
        service.connect_release.set()
        if selector._connect_thread is not None:
            selector._connect_thread.wait(1000)

    assert "QThread: Destroyed while thread is still running" not in capsys.readouterr().err
