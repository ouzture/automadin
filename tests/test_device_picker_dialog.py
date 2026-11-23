from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceType,
    WindowsDevice,
)
from automadin.runtime import RuntimeComponentStatus
from automadin.services.browser_health import BrowserHealthResult, BrowserHealthState
from automadin.services.device_choices import DeviceChoice
from automadin.ui.launcher.device_picker_dialog import DevicePickerDialog, _PickerItem


class _Database:
    @staticmethod
    def profile_engine_specs() -> list[object]:
        return []

    @staticmethod
    def profile_display_labels() -> dict[str, str]:
        return {}


class _SuccessfulDiscovery:
    def __init__(self) -> None:
        self.calls: list[DeviceType] = []

    def list_devices(self, device_type: DeviceType, _profiles: list[object]):
        self.calls.append(device_type)
        if device_type is DeviceType.BROWSER:
            return [BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM))]
        if device_type is DeviceType.ANDROID:
            return [AndroidDevice(device_id="emulator-5554", model="Pixel")]
        return [WindowsDevice(hwnd=123, title="Calculator")]


class _PartialFailureDiscovery(_SuccessfulDiscovery):
    def list_devices(self, device_type: DeviceType, profiles: list[object]):
        if device_type is DeviceType.ANDROID:
            self.calls.append(device_type)
            raise RuntimeError("ADB server is not available\nstart it and retry")
        return super().list_devices(device_type, profiles)


class _SlowDiscovery(_SuccessfulDiscovery):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def list_devices(self, device_type: DeviceType, profiles: list[object]):
        self.calls.append(device_type)
        self.entered.set()
        self.release.wait(2)
        if device_type is DeviceType.BROWSER:
            return [BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM))]
        return super().list_devices(device_type, profiles)


@pytest.fixture(scope="module", autouse=True)
def _application():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _detected_browser_runtime(monkeypatch: pytest.MonkeyPatch):
    status = RuntimeComponentStatus("managed", Path("browsers"), True)

    def assess(devices, _data_dir):
        return [DeviceChoice(device, status) for device in devices]

    monkeypatch.setattr(
        "automadin.ui.launcher.device_picker_dialog.assess_device_choices",
        assess,
    )

    class HealthyCache:
        @staticmethod
        def check_many(statuses, _data_dir, cancel):
            assert not cancel()
            return {
                target: BrowserHealthResult(
                    target,
                    BrowserHealthState.HEALTHY,
                    "complete",
                    datetime.now(UTC),
                    0.01,
                    "The isolated headless launch check completed.",
                )
                for target in statuses
            }

    monkeypatch.setattr(
        "automadin.ui.launcher.device_picker_dialog.default_browser_health_cache",
        lambda: HealthyCache(),
    )


def _show_dialog(discovery) -> DevicePickerDialog:
    dialog = DevicePickerDialog(discovery, _Database())  # type: ignore[arg-type]
    dialog.show()
    QApplication.processEvents()
    return dialog


def _delete_dialog(dialog: DevicePickerDialog) -> None:
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def test_picker_scopes_discovery_and_rows_to_one_requested_capability() -> None:
    discovery = _SuccessfulDiscovery()
    dialog = DevicePickerDialog(
        discovery,  # type: ignore[arg-type]
        _Database(),  # type: ignore[arg-type]
        initial_type=DeviceType.ANDROID,
        allowed_types=(DeviceType.ANDROID,),
    )
    dialog.show()
    thread = dialog._thread
    assert thread is not None
    assert thread.wait(2000)
    QApplication.processEvents()
    try:
        assert discovery.calls == [DeviceType.ANDROID]
        assert dialog._type_tabs.count() == 1
        assert dialog._type_tabs.tabData(0) is DeviceType.ANDROID
        assert dialog._list.count() == 2
        assert dialog._list.item(0).text() == "ANDROID"
        assert "Pixel" in dialog._list.item(1).text()
        assert "browser" not in dialog._scope_hint.text().casefold()
    finally:
        _delete_dialog(dialog)


def test_picker_capability_tabs_never_mix_target_rows() -> None:
    dialog = DevicePickerDialog(  # type: ignore[arg-type]
        _SuccessfulDiscovery(),
        _Database(),
    )
    dialog._all_items = [
        _PickerItem(
            "BROWSER",
            "Chromium Desktop",
            "chromium-desktop",
            DeviceChoice(
                BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM)),
                None,
            ),
        ),
        _PickerItem(
            "ANDROID",
            "Pixel",
            "usb-1",
            DeviceChoice(AndroidDevice("usb-1", "Pixel"), None),
        ),
    ]

    dialog._filter("")
    assert dialog._list.count() == 2
    assert dialog._list.item(0).text() == "BROWSER"

    android_index = next(
        index
        for index in range(dialog._type_tabs.count())
        if dialog._type_tabs.tabData(index) is DeviceType.ANDROID
    )
    dialog._type_tabs.setCurrentIndex(android_index)

    assert dialog._list.count() == 2
    assert dialog._list.item(0).text() == "ANDROID"
    assert dialog._list.item(1).text() == "Pixel"
    dialog.deleteLater()


@pytest.mark.parametrize("close_action", ("reject", "close", "escape"))
def test_close_is_deferred_until_slow_discovery_finishes(close_action: str) -> None:
    discovery = _SlowDiscovery()
    dialog = _show_dialog(discovery)
    assert discovery.entered.wait(1)
    thread = dialog._thread
    assert thread is not None

    try:
        if close_action == "escape":
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            getattr(dialog, close_action)()
        QApplication.processEvents()

        assert dialog.isVisible()
        assert thread.isInterruptionRequested()
        assert dialog._thread is thread
        assert "before closing" in dialog._warning.text()

        discovery.release.set()
        assert thread.wait(1000)
        QApplication.processEvents()

        assert not dialog.isVisible()
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert dialog._thread is None
    finally:
        discovery.release.set()
        thread.wait(1000)
        _delete_dialog(dialog)


def test_late_discovery_result_is_suppressed_after_reject() -> None:
    discovery = _SlowDiscovery()
    dialog = _show_dialog(discovery)
    assert discovery.entered.wait(1)
    thread = dialog._thread
    assert thread is not None

    try:
        dialog.reject()
        discovery.release.set()
        assert thread.wait(1000)
        QApplication.processEvents()

        assert dialog._all_items == []
        assert dialog._list.count() == 0
        assert discovery.calls == [DeviceType.BROWSER]
    finally:
        discovery.release.set()
        thread.wait(1000)
        _delete_dialog(dialog)


def test_partial_discovery_failure_keeps_results_and_shows_actionable_warning() -> None:
    discovery = _PartialFailureDiscovery()
    dialog = _show_dialog(discovery)
    thread = dialog._thread
    assert thread is not None

    try:
        assert thread.wait(1000)
        QApplication.processEvents()

        assert len(dialog._all_items) == 2
        assert {item.group for item in dialog._all_items} == {"BROWSER", "WINDOWS"}
        assert dialog._stack.currentIndex() == 1
        assert dialog._warning.isVisible()
        warning = dialog._warning.text()
        assert "Android: Discovery is unavailable." in warning
        assert "ADB server is not available" not in warning
        assert "Check the affected runtime or connection" in warning
    finally:
        dialog.close()
        _delete_dialog(dialog)


def test_health_coordinator_failure_keeps_browser_group_selectable() -> None:
    class RaisingHealth:
        @staticmethod
        def check_many(_statuses, _data_dir, cancel):
            assert not cancel()
            raise RuntimeError("health coordinator unavailable")

    dialog = DevicePickerDialog(  # type: ignore[arg-type]
        _SuccessfulDiscovery(),
        _Database(),
        browser_health_cache=RaisingHealth(),  # type: ignore[arg-type]
    )
    dialog.show()
    QApplication.processEvents()
    thread = dialog._thread
    assert thread is not None

    try:
        assert thread.wait(1000)
        QApplication.processEvents()

        browser_item = dialog._list.item(1)
        assert browser_item.text().endswith("— Could not check")
        assert browser_item.flags() & Qt.ItemFlag.ItemIsEnabled
        assert browser_item.flags() & Qt.ItemFlag.ItemIsSelectable
        dialog._list.setCurrentItem(browser_item)
        assert dialog.selected_device_id() == "chromium-desktop"
        assert "browser launch check could not be completed" in dialog._details.text()
        assert "health coordinator unavailable" not in dialog._details.text()
    finally:
        dialog.close()
        _delete_dialog(dialog)


def test_unavailable_browser_is_visible_readable_and_nonactivatable_when_filtered() -> None:
    dialog = DevicePickerDialog(_SuccessfulDiscovery(), _Database())  # type: ignore[arg-type]
    chromium = BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM))
    edge = BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.MSEDGE))
    detected = RuntimeComponentStatus("managed", Path("browsers"), True)
    unavailable = RuntimeComponentStatus(
        "unavailable",
        None,
        False,
        error="Microsoft Edge was not detected.",
    )
    items = [
        _PickerItem(
            "BROWSER",
            chromium.name,
            chromium.id,
            DeviceChoice(chromium, detected),
        ),
        _PickerItem(
            "BROWSER",
            edge.name,
            edge.id,
            DeviceChoice(edge, unavailable),
        ),
    ]

    dialog._populate(items, [])

    assert dialog._list.count() == 3
    edge_item = dialog._list.item(2)
    assert edge_item.text().endswith("— Unavailable")
    assert edge_item.flags() & Qt.ItemFlag.ItemIsEnabled
    assert edge_item.flags() & Qt.ItemFlag.ItemIsSelectable
    assert "Microsoft Edge was not detected" in edge_item.toolTip()
    assert "1 browser choice needs attention" in dialog._warning.text()
    dialog._list.setCurrentItem(edge_item)
    assert not dialog._details.isHidden()
    assert "Microsoft Edge was not detected" in dialog._details.text()
    assert dialog.selected_device_id() is None
    assert not dialog._buttons.button(
        dialog._buttons.StandardButton.Ok
    ).isEnabled()

    dialog._filter("edge")

    assert dialog._list.count() == 2
    filtered_edge = dialog._list.item(1)
    assert filtered_edge.text().endswith("— Unavailable")
    assert filtered_edge.flags() & Qt.ItemFlag.ItemIsEnabled
    assert not dialog._buttons.button(
        dialog._buttons.StandardButton.Ok
    ).isEnabled()
    _delete_dialog(dialog)


def test_unknown_browser_assessment_remains_selectable_with_literal_warning() -> None:
    dialog = DevicePickerDialog(_SuccessfulDiscovery(), _Database())  # type: ignore[arg-type]
    chrome = BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.CHROME))
    item = _PickerItem(
        "BROWSER",
        chrome.name,
        chrome.id,
        DeviceChoice(chrome, None, assessment_error="App Paths access denied"),
    )

    dialog._populate([item], [])
    browser_item = dialog._list.item(1)
    dialog._list.setCurrentItem(browser_item)

    assert browser_item.flags() & Qt.ItemFlag.ItemIsEnabled
    assert browser_item.flags() & Qt.ItemFlag.ItemIsSelectable
    assert "Could not check runtime availability" in browser_item.toolTip()
    assert dialog.selected_device_id() == chrome.id
    assert dialog._buttons.button(
        dialog._buttons.StandardButton.Ok
    ).isEnabled()
    _delete_dialog(dialog)


def test_reject_cooperatively_cancels_a_running_launch_health_check() -> None:
    class CancellableHealth:
        def __init__(self) -> None:
            self.entered = Event()
            self.cancelled = Event()

        def check_many(self, statuses, _data_dir, cancel):
            self.entered.set()
            deadline = time.monotonic() + 2
            while not cancel():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            self.cancelled.set()
            checked_at = datetime.now(UTC)
            return {
                target: BrowserHealthResult(
                    target,
                    BrowserHealthState.CANCELLED,
                    "cancel",
                    checked_at,
                    0.01,
                    "Launch check cancelled.",
                )
                for target in statuses
            }

    health = CancellableHealth()
    dialog = DevicePickerDialog(  # type: ignore[arg-type]
        _SuccessfulDiscovery(),
        _Database(),
        browser_health_cache=health,  # type: ignore[arg-type]
    )
    dialog.show()
    QApplication.processEvents()
    assert health.entered.wait(1)
    thread = dialog._thread
    assert thread is not None

    try:
        dialog.reject()
        assert thread.isInterruptionRequested()
        assert thread.wait(1000)
        QApplication.processEvents()

        assert health.cancelled.is_set()
        assert not dialog.isVisible()
        assert dialog._all_items == []
    finally:
        if thread.isRunning():
            thread.requestInterruption()
            thread.wait(1000)
        _delete_dialog(dialog)


def test_successful_discovery_populates_once_without_warning() -> None:
    discovery = _SuccessfulDiscovery()
    dialog = _show_dialog(discovery)
    thread = dialog._thread
    assert thread is not None

    try:
        assert thread.wait(1000)
        QApplication.processEvents()

        assert len(dialog._all_items) == 3
        assert dialog._stack.currentIndex() == 1
        assert not dialog._warning.isVisible()
        assert discovery.calls == [
            DeviceType.BROWSER,
            DeviceType.ANDROID,
            DeviceType.WINDOWS,
        ]

        dialog.hide()
        dialog.show()
        QApplication.processEvents()
        assert discovery.calls == [
            DeviceType.BROWSER,
            DeviceType.ANDROID,
            DeviceType.WINDOWS,
        ]

        first_device = dialog._list.item(1)
        assert first_device.text().endswith("— Engine launch tested")
        dialog._list.setCurrentItem(first_device)
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.selected_device_id() == "chromium-desktop"
    finally:
        dialog.close()
        _delete_dialog(dialog)


def test_launch_failure_is_engine_scoped_visible_and_remains_selectable() -> None:
    dialog = DevicePickerDialog(_SuccessfulDiscovery(), _Database())  # type: ignore[arg-type]
    profile_id = "31132eb4-1dec-4fab-9728-2e02901e3996"
    chrome = BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.CHROME,
            profile_id=profile_id,
        )
    )
    detected = RuntimeComponentStatus("system", Path("chrome.exe"), True)
    failed = BrowserHealthResult(
        "chrome",
        BrowserHealthState.FAILED,
        "launch",
        datetime.now(UTC),
        0.01,
        "The isolated headless launch check failed.",
    )
    picker_item = _PickerItem(
        "BROWSER",
        "Chrome Desktop — Example User / Work",
        chrome.id,
        DeviceChoice(chrome, detected, launch_health=failed),
    )

    dialog._populate([picker_item], [])
    browser_item = dialog._list.item(1)
    dialog._list.setCurrentItem(browser_item)

    assert browser_item.text().endswith("— Engine check needs attention")
    assert browser_item.flags() & Qt.ItemFlag.ItemIsEnabled
    assert browser_item.flags() & Qt.ItemFlag.ItemIsSelectable
    assert dialog.selected_device_id() == chrome.id
    assert dialog._buttons.button(
        dialog._buttons.StandardButton.Ok
    ).isEnabled()
    assert "headed/profile launch" in dialog._details.text()
    _delete_dialog(dialog)


def test_profile_row_states_that_engine_not_profile_was_tested() -> None:
    dialog = DevicePickerDialog(_SuccessfulDiscovery(), _Database())  # type: ignore[arg-type]
    profile_id = "31132eb4-1dec-4fab-9728-2e02901e3996"
    chromium = BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_id=profile_id,
        )
    )
    detected = RuntimeComponentStatus("managed", Path("browsers"), True)
    healthy = BrowserHealthResult(
        "chromium",
        BrowserHealthState.HEALTHY,
        "complete",
        datetime.now(UTC),
        0.01,
        "The isolated headless launch check completed.",
    )

    dialog._populate(
        [
            _PickerItem(
                "BROWSER",
                "Chromium Desktop — Example User / Work",
                chromium.id,
                DeviceChoice(chromium, detected, launch_health=healthy),
            )
        ],
        [],
    )

    assert dialog._list.item(1).text().endswith(
        "— Engine tested · profile not checked"
    )
    _delete_dialog(dialog)
