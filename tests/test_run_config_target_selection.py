from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceType,
    WindowsDevice,
    saved_device_id_for_device,
)
from automadin.models.flow import AutomationFlow
from automadin.models.run_config import BrowserRunMode, SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.ui.launcher.run_config_dialog import RunConfigDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _database(path: Path) -> DatabaseService:
    database = DatabaseService(str(path / "target-selection.db"))
    database.save_flow("Flow", AutomationFlow("Flow"))
    return database


def test_saved_windows_target_restores_capability_without_showing_android_source(
    tmp_path: Path,
) -> None:
    _app()
    database = _database(tmp_path)
    target = WindowsDevice(10, "Calculator", r"C:\Windows\calc.exe")
    dialog = RunConfigDialog(
        database,
        existing=SavedRunConfig(
            name="Windows run",
            flow_name="Flow",
            device_id=saved_device_id_for_device(target),
        ),
    )
    try:
        assert dialog._capability_combo.currentData() is DeviceType.WINDOWS
        assert dialog._android_source_label.isHidden()
        assert dialog._target_type_combo.isHidden()
        assert dialog._device_label.text() == "Windows target:"
        assert not dialog._windows_section.isHidden()
    finally:
        dialog.deleteLater()
        database.close()


def test_changing_capability_clears_an_incompatible_saved_target(
    tmp_path: Path,
) -> None:
    _app()
    database = _database(tmp_path)
    browser = BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    dialog = RunConfigDialog(database, current_device_id=browser.id)
    try:
        assert dialog._capability_combo.currentData() is DeviceType.BROWSER
        assert dialog._selected_device_id == browser.id

        dialog._capability_combo.setCurrentIndex(
            dialog._capability_combo.findData(DeviceType.ANDROID)
        )

        assert dialog._selected_device_id == ""
        assert not dialog._android_source_label.isHidden()
        assert not dialog._target_type_combo.isHidden()
        assert dialog._device_label.text() == "Android target:"
    finally:
        dialog.deleteLater()
        database.close()


def test_headless_compatibility_identity_is_default_on_and_saved(
    tmp_path: Path,
) -> None:
    _app()
    database = _database(tmp_path)
    browser = BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    dialog = RunConfigDialog(database, current_device_id=browser.id)
    try:
        dialog._headless_combo.setCurrentIndex(1)

        assert dialog._headless_identity_cb.isChecked()
        assert dialog._headless_identity_cb.isEnabled()
        saved = dialog.get_saved_run_config()
        assert saved.browser_options == {
            "headless": True,
            "headless_uses_headed_user_agent": True,
        }

        dialog._headless_identity_cb.setChecked(False)
        saved = dialog.get_saved_run_config()
        assert not saved.browser_options["headless_uses_headed_user_agent"]
    finally:
        dialog.deleteLater()
        database.close()


def test_connected_browser_mode_is_restored_and_disables_launch_options(
    tmp_path: Path,
) -> None:
    _app()
    database = _database(tmp_path)
    browser = BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    dialog = RunConfigDialog(
        database,
        existing=SavedRunConfig(
            name="Connected browser run",
            flow_name="Flow",
            device_id=browser.id,
            browser_options={"headless": True},
            browser_run_mode=BrowserRunMode.CONNECTED,
        ),
    )
    try:
        assert dialog._browser_run_mode_combo.currentData() is BrowserRunMode.CONNECTED
        assert not dialog._headless_combo.isEnabled()
        assert not dialog._headless_identity_cb.isEnabled()
        assert dialog._device_widget.isHidden()
        saved = dialog.get_saved_run_config()
        assert saved.browser_run_mode is BrowserRunMode.CONNECTED
        assert saved.device_id == ""
    finally:
        dialog.deleteLater()
        database.close()


def test_connected_browser_mode_does_not_require_a_saved_target(tmp_path: Path) -> None:
    _app()
    database = _database(tmp_path)
    dialog = RunConfigDialog(database)
    try:
        dialog._name_edit.setText("Connected run")
        dialog._browser_run_mode_combo.setCurrentIndex(
            dialog._browser_run_mode_combo.findData(BrowserRunMode.CONNECTED)
        )

        dialog._on_accept()

        assert dialog.result() == dialog.DialogCode.Accepted
        assert dialog.get_saved_run_config().device_id == ""
    finally:
        dialog.deleteLater()
        database.close()


def test_editing_run_setup_preserves_pin_state(tmp_path: Path) -> None:
    _app()
    database = _database(tmp_path)
    browser = BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    dialog = RunConfigDialog(
        database,
        existing=SavedRunConfig(
            name="Pinned run",
            flow_name="Flow",
            device_id=browser.id,
            is_pinned=True,
        ),
    )
    try:
        assert dialog.get_saved_run_config().is_pinned
    finally:
        dialog.deleteLater()
        database.close()


def test_editing_run_setup_preserves_value_prompt_policy(tmp_path: Path) -> None:
    _app()
    database = _database(tmp_path)
    browser = BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    dialog = RunConfigDialog(
        database,
        existing=SavedRunConfig(
            name="Quiet run",
            flow_name="Flow",
            device_id=browser.id,
            ask_for_values=False,
        ),
    )
    try:
        assert not dialog._ask_for_values_cb.isChecked()
        assert not dialog.get_saved_run_config().ask_for_values
    finally:
        dialog.deleteLater()
        database.close()
