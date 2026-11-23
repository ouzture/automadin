from __future__ import annotations

import os
from pathlib import Path
from threading import get_ident
from unittest.mock import patch
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QApplication

from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceType,
)
from automadin.runtime import RuntimeComponentStatus
from automadin.services.device_choices import DeviceChoice
from automadin.services.device_choices import (
    browser_health_target_for_device,
    runtime_family_for_device,
)
from automadin.services.browser_health import BrowserHealthResult, BrowserHealthState
from automadin.ui.device_selector import (
    DeviceSelectorWidget,
    _CHOICE_ROLE,
    _DiscoveryOutcome,
)


def _browser(engine: BrowserEngine) -> BrowserDevice:
    return BrowserDevice(BrowserDeviceConfig(engine=engine))


def test_companion_is_not_assessed_as_a_new_browser_launch() -> None:
    companion = BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.MSEDGE,
            companion_client_id="client-1",
            companion_target_id="7:9",
            companion_title="Orders",
        )
    )

    assert runtime_family_for_device(companion) is None
    assert browser_health_target_for_device(companion) is None


class _Db:
    db_path = Path("selector-runtime.db")

    @staticmethod
    def profile_engine_specs():
        return []

    @staticmethod
    def profile_display_labels():
        return {}


class _SessionService:
    def __init__(self) -> None:
        self.session_lookups = 0
        self.created = 0

    def get_session_by_device(self, _device_id: str):
        self.session_lookups += 1
        return None

    def create_session(self, _device):
        self.created += 1
        raise AssertionError("Unavailable choice must not start a session")


@pytest.fixture(scope="module", autouse=True)
def _application():
    return QApplication.instance() or QApplication([])


def _selector(service: _SessionService | None = None) -> DeviceSelectorWidget:
    selector = DeviceSelectorWidget(service or _SessionService(), _Db())  # type: ignore[arg-type]
    selector._type_combo.setCurrentIndex(
        selector._type_combo.findData(DeviceType.BROWSER)
    )
    return selector


def test_browser_choices_keep_unavailable_runtime_visible_but_nonactivatable() -> None:
    selector = _selector()
    chromium = _browser(BrowserEngine.CHROMIUM)
    edge = _browser(BrowserEngine.MSEDGE)
    chrome = _browser(BrowserEngine.CHROME)
    detected = RuntimeComponentStatus("managed", Path("browsers"), True)
    missing_edge = RuntimeComponentStatus(
        "unavailable",
        None,
        False,
        error="Microsoft Edge was not detected.",
    )

    selector._on_discovered(
        _DiscoveryOutcome(
            DeviceType.BROWSER,
            [chromium, edge, chrome],
            choices=[
                DeviceChoice(chromium, detected),
                DeviceChoice(edge, missing_edge),
                DeviceChoice(chrome, None, assessment_error="registry denied"),
            ],
        )
    )

    assert selector._device_combo.count() == 3
    assert selector._device_combo.currentData() is chromium
    assert selector._connect_btn.isEnabled()
    assert selector._device_combo.itemText(1).endswith("— Unavailable")
    assert "Microsoft Edge was not detected" in selector._device_combo.itemData(
        1, Qt.ItemDataRole.ToolTipRole
    )
    assert "Detected — not launch tested" in selector._device_combo.itemData(
        0, Qt.ItemDataRole.ToolTipRole
    )
    assert "Could not check runtime availability" in selector._device_combo.itemData(
        2, Qt.ItemDataRole.AccessibleDescriptionRole
    )

    model = selector._device_combo.model()
    assert isinstance(model, QStandardItemModel)
    edge_item = model.item(1)
    assert edge_item is not None
    assert edge_item.isEnabled()
    assert edge_item.flags() & Qt.ItemFlag.ItemIsSelectable
    assert "1 browser choice needs attention" in selector._status_label.text()
    selector._device_combo.setCurrentIndex(1)
    assert not selector._connect_btn.isEnabled()
    assert "Unavailable" in selector._status_label.text()
    assert "Runtime Components" in selector._status_label.text()
    selector.deleteLater()


def test_no_available_browser_leaves_connect_disabled_with_repair_guidance() -> None:
    selector = _selector()
    chrome = _browser(BrowserEngine.CHROME)
    edge = _browser(BrowserEngine.MSEDGE)
    unavailable = RuntimeComponentStatus(
        "unavailable",
        None,
        False,
        error="Browser not detected.",
    )

    selector._on_discovered(
        _DiscoveryOutcome(
            DeviceType.BROWSER,
            [chrome, edge],
            choices=[
                DeviceChoice(chrome, unavailable),
                DeviceChoice(edge, unavailable),
            ],
        )
    )

    assert selector._device_combo.currentIndex() == -1
    assert not selector._connect_btn.isEnabled()
    assert "No browser runtime is available" in selector._status_label.text()
    assert "Runtime Components" in selector._status_label.text()
    selector.deleteLater()


def test_defensive_connect_gate_rejects_programmatic_unavailable_choice() -> None:
    service = _SessionService()
    selector = _selector(service)
    edge = _browser(BrowserEngine.MSEDGE)
    choice = DeviceChoice(
        edge,
        RuntimeComponentStatus(
            "unavailable",
            None,
            False,
            error="Microsoft Edge was not detected.",
        ),
    )
    selector._device_combo.addItem("MS Edge Desktop — Unavailable", edge)
    selector._device_combo.setItemData(0, choice, _CHOICE_ROLE)
    selector._device_combo.setCurrentIndex(0)

    with patch("automadin.ui.device_selector.QMessageBox.warning") as warning:
        selector._connect()

    warning.assert_called_once()
    assert service.session_lookups == 0
    assert service.created == 0
    assert not selector._connect_btn.isEnabled()
    selector.deleteLater()


def test_threaded_browser_scan_attaches_exact_launch_health_off_the_ui_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chromium = _browser(BrowserEngine.CHROMIUM)
    firefox = _browser(BrowserEngine.FIREFOX)
    detected = RuntimeComponentStatus("managed", tmp_path / "browsers", True)
    main_thread = get_ident()

    class Database(_Db):
        db_path = tmp_path / "automadin.db"

        def __init__(self) -> None:
            self.profile_thread: int | None = None
            self.label_thread: int | None = None
            self.release_thread: int | None = None

        def profile_engine_specs(self):
            self.profile_thread = get_ident()
            return []

        def profile_display_labels(self):
            self.label_thread = get_ident()
            return {}

        def release_current_thread_connection(self):
            self.release_thread = get_ident()

    class Discovery:
        @staticmethod
        def list_devices(_device_type, _profiles):
            return [chromium, firefox]

    class Service(_SessionService):
        discovery = Discovery()

    class HealthCache:
        def __init__(self) -> None:
            self.thread: int | None = None
            self.status_targets: tuple[str, ...] = ()

        def check_many(self, statuses, _data_dir, cancel):
            self.thread = get_ident()
            self.status_targets = tuple(statuses)
            assert not cancel()
            checked_at = datetime.now(UTC)
            return {
                "chromium": BrowserHealthResult(
                    "chromium",
                    BrowserHealthState.HEALTHY,
                    "complete",
                    checked_at,
                    0.01,
                    "Launch completed.",
                ),
                "firefox": BrowserHealthResult(
                    "firefox",
                    BrowserHealthState.FAILED,
                    "launch",
                    checked_at,
                    0.01,
                    "Launch failed.",
                ),
            }

    monkeypatch.setattr(
        "automadin.ui.device_selector.assess_device_choices",
        lambda devices, _data_dir: [
            DeviceChoice(device, detected) for device in devices
        ],
    )
    database = Database()
    cache = HealthCache()
    selector = DeviceSelectorWidget(  # type: ignore[arg-type]
        Service(),
        database,
        browser_health_cache=cache,  # type: ignore[arg-type]
    )
    selector._type_combo.setCurrentIndex(
        selector._type_combo.findData(DeviceType.BROWSER)
    )

    selector._discover_devices()
    thread = selector._discover_thread
    assert thread is not None
    assert thread.wait(2000)
    QApplication.processEvents()

    assert database.profile_thread is not None
    assert database.profile_thread != main_thread
    assert database.label_thread == database.profile_thread
    assert database.release_thread == database.profile_thread
    assert cache.thread == database.profile_thread
    assert cache.status_targets == ("chromium", "firefox")
    assert selector._device_combo.itemText(0).endswith("— Engine launch tested")
    assert selector._device_combo.itemText(1).endswith(
        "— Engine check needs attention"
    )
    model = selector._device_combo.model()
    assert isinstance(model, QStandardItemModel)
    assert model.item(1).isEnabled()
    selector._device_combo.setCurrentIndex(1)
    assert selector._connect_btn.isEnabled()
    assert "headed launch remains available" in selector._status_label.text()
    selector.deleteLater()


def test_health_service_failure_preserves_detected_browser_choices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chromium = _browser(BrowserEngine.CHROMIUM)
    detected = RuntimeComponentStatus("managed", tmp_path / "browsers", True)

    class Database(_Db):
        db_path = tmp_path / "automadin.db"

        @staticmethod
        def release_current_thread_connection():
            return None

    class Discovery:
        @staticmethod
        def list_devices(_device_type, _profiles):
            return [chromium]

    class Service(_SessionService):
        discovery = Discovery()

    class RaisingHealthCache:
        @staticmethod
        def check_many(_statuses, _data_dir, cancel):
            assert not cancel()
            raise RuntimeError("health coordinator unavailable")

    monkeypatch.setattr(
        "automadin.ui.device_selector.assess_device_choices",
        lambda devices, _data_dir: [
            DeviceChoice(device, detected) for device in devices
        ],
    )
    selector = DeviceSelectorWidget(  # type: ignore[arg-type]
        Service(),
        Database(),
        browser_health_cache=RaisingHealthCache(),  # type: ignore[arg-type]
    )
    selector._type_combo.setCurrentIndex(
        selector._type_combo.findData(DeviceType.BROWSER)
    )

    selector._discover_devices()
    thread = selector._discover_thread
    assert thread is not None
    assert thread.wait(2000)
    QApplication.processEvents()

    assert selector._device_combo.count() == 1
    assert selector._device_combo.itemText(0).endswith("— Could not check")
    assert selector._connect_btn.isEnabled()
    tooltip = selector._device_combo.itemData(
        0,
        Qt.ItemDataRole.ToolTipRole,
    )
    assert "Could not check isolated headless engine launch health" in tooltip
    assert "actual browser launch is the final check" in tooltip
    assert "health coordinator unavailable" not in tooltip
    assert "health coordinator unavailable" in caplog.text
    selector.deleteLater()
