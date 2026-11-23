from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.engine.adb_engine import (
    AdbDiscoveryDiagnostic,
    AdbDiscoveryDiagnosticKind,
    AdbDiscoveryResult,
)
from automadin.models.device import AndroidDevice, DeviceType
from automadin.ui.device_selector import DeviceSelectorWidget, _DiscoveryOutcome


class _Db:
    @staticmethod
    def profile_engine_specs() -> list[tuple[str, str]]:
        return []

    @staticmethod
    def profile_display_labels() -> dict[str, str]:
        return {}


class _SessionService:
    pass


@pytest.fixture(scope="module", autouse=True)
def _application():
    return QApplication.instance() or QApplication([])


def _selector() -> DeviceSelectorWidget:
    selector = DeviceSelectorWidget(_SessionService(), _Db())  # type: ignore[arg-type]
    selector._type_combo.setCurrentIndex(
        selector._type_combo.findData(DeviceType.ANDROID)
    )
    return selector


def test_device_selector_guides_unauthorized_android_device() -> None:
    selector = _selector()
    result = AdbDiscoveryResult(
        devices=(),
        diagnostics=(
            AdbDiscoveryDiagnostic(
                kind=AdbDiscoveryDiagnosticKind.UNAUTHORIZED,
                message="1 Android device is waiting for USB debugging authorization.",
                action="Unlock the device and accept the USB debugging prompt.",
                device_ids=("locked-1",),
            ),
            AdbDiscoveryDiagnostic(
                kind=AdbDiscoveryDiagnosticKind.SUCCESS,
                message="ADB is ready; no Android devices are available.",
            ),
        ),
    )

    selector._on_discovered(
        _DiscoveryOutcome(DeviceType.ANDROID, [], adb_result=result)
    )

    assert "Authorize USB debugging" in selector._device_combo.placeholderText()
    assert "authorization" in selector._status_label.text()
    assert "Unlock the device" in selector._status_label.toolTip()
    selector.deleteLater()


@pytest.mark.parametrize(
    ("kind", "placeholder"),
    [
        (AdbDiscoveryDiagnosticKind.MISSING_EXECUTABLE, "ADB missing"),
        (AdbDiscoveryDiagnosticKind.TIMEOUT, "ADB timed out"),
        (AdbDiscoveryDiagnosticKind.COMMAND_FAILED, "ADB failed"),
        (AdbDiscoveryDiagnosticKind.OFFLINE, "Device offline"),
    ],
)
def test_device_selector_maps_adb_failures_to_actionable_state(
    kind: AdbDiscoveryDiagnosticKind, placeholder: str
) -> None:
    selector = _selector()
    result = AdbDiscoveryResult(
        devices=(),
        diagnostics=(
            AdbDiscoveryDiagnostic(
                kind=kind,
                message=f"Diagnostic: {kind.value}",
                action="Action the user can take.",
                detail="Technical detail.",
            ),
        ),
    )

    selector._on_discovered(
        _DiscoveryOutcome(DeviceType.ANDROID, [], adb_result=result)
    )

    assert placeholder in selector._device_combo.placeholderText()
    assert selector._status_label.text() == f"Diagnostic: {kind.value}"
    assert "Action the user can take" in selector._status_label.toolTip()
    selector.deleteLater()


def test_device_selector_shows_success_and_ready_devices() -> None:
    selector = _selector()
    device = AndroidDevice(device_id="ready-1", model="Pixel 7")
    result = AdbDiscoveryResult(
        devices=(device,),
        diagnostics=(
            AdbDiscoveryDiagnostic(
                kind=AdbDiscoveryDiagnosticKind.SUCCESS,
                message="ADB is ready; 1 Android device is available.",
            ),
        ),
    )

    selector._on_discovered(
        _DiscoveryOutcome(DeviceType.ANDROID, [device], adb_result=result)
    )

    assert selector._device_combo.count() == 1
    assert "Pixel 7" in selector._device_combo.itemText(0)
    assert selector._status_label.text() == "ADB is ready; 1 Android device is available."
    selector.deleteLater()


def test_device_selector_ignores_stale_discovery_for_previous_type() -> None:
    selector = _selector()
    selector._type_combo.setCurrentIndex(
        selector._type_combo.findData(DeviceType.BROWSER)
    )
    selector._device_combo.setPlaceholderText("Browser discovery not started")
    stale_device = AndroidDevice(device_id="stale-1")
    stale_result = AdbDiscoveryResult(
        devices=(stale_device,),
        diagnostics=(
            AdbDiscoveryDiagnostic(
                kind=AdbDiscoveryDiagnosticKind.SUCCESS,
                message="stale Android result",
            ),
        ),
    )

    selector._on_discovered(
        _DiscoveryOutcome(
            DeviceType.ANDROID,
            [stale_device],
            adb_result=stale_result,
        )
    )

    assert selector._device_combo.count() == 0
    assert selector._device_combo.placeholderText() == "Browser discovery not started"
    selector.deleteLater()


def test_explicit_target_discovery_selects_requested_device_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = _selector()
    discovered: list[DeviceType] = []
    monkeypatch.setattr(
        selector,
        "_discover_devices",
        lambda: discovered.append(selector.get_selected_device_type()),
    )

    selector.discover_device_type(DeviceType.BROWSER)

    assert selector.get_selected_device_type() is DeviceType.BROWSER
    assert discovered == [DeviceType.BROWSER]
    with pytest.raises(ValueError, match="Unsupported device type"):
        selector.discover_device_type("browser")  # type: ignore[arg-type]
    selector.deleteLater()


def test_explicit_target_discovery_queues_behind_an_active_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RunningThread:
        @staticmethod
        def isRunning() -> bool:
            return True

    selector = _selector()
    selector._discover_thread = _RunningThread()  # type: ignore[assignment]

    selector.discover_device_type(DeviceType.BROWSER)

    assert selector._pending_discovery_type is DeviceType.BROWSER
    assert "queued" in selector._status_label.text().casefold()

    discovered: list[DeviceType] = []
    selector._discover_thread = None
    monkeypatch.setattr(
        selector,
        "_discover_devices",
        lambda: discovered.append(selector.get_selected_device_type()),
    )
    selector._on_discovery_thread_finished()

    assert selector._pending_discovery_type is None
    assert discovered == [DeviceType.BROWSER]
    selector.deleteLater()


def test_explicit_target_discovery_requires_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = _selector()
    original_type = selector.get_selected_device_type()
    discovered: list[DeviceType] = []
    selector._current_session = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        selector,
        "_discover_devices",
        lambda: discovered.append(selector.get_selected_device_type()),
    )

    selector.discover_device_type(DeviceType.BROWSER)

    assert selector.get_selected_device_type() is original_type
    assert discovered == []
    assert "Disconnect the current target" in selector._status_label.text()
    selector.deleteLater()
