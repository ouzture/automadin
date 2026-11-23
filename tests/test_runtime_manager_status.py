from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.runtime import (
    BrowserPayloadStatus,
    RuntimeComponentStatus,
    RuntimeConfiguration,
)
from automadin.ui import runtime_manager


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _unavailable() -> RuntimeComponentStatus:
    return RuntimeComponentStatus(source="unavailable", path=None, usable=False)


def test_incomplete_installed_browser_package_is_directed_to_repair(
    monkeypatch, tmp_path: Path
) -> None:
    _app()
    managed_root = tmp_path / "runtime" / "browsers" / "playwright-test"
    managed_root.mkdir(parents=True)
    incomplete = BrowserPayloadStatus(
        root=managed_root,
        manifest_present=True,
        markers_complete=True,
        usable=False,
        executable_paths=(),
        missing_engines=("firefox",),
    )
    monkeypatch.setattr(
        runtime_manager,
        "configure_runtime",
        lambda _data_dir: RuntimeConfiguration(
            browsers=_unavailable(),
            adb=_unavailable(),
        ),
    )
    monkeypatch.setattr(runtime_manager, "managed_browsers_dir", lambda _data_dir: managed_root)
    monkeypatch.setattr(runtime_manager, "inspect_browser_payload", lambda _root: incomplete)
    monkeypatch.setattr(runtime_manager, "managed_adb_path", lambda _data_dir: None)
    monkeypatch.setattr(runtime_manager, "managed_runtime_dir", lambda _data_dir: tmp_path / "runtime")

    dialog = runtime_manager.RuntimeManagerDialog(str(tmp_path))
    try:
        assert "installed but needs Repair" in dialog._browser_package_status.text()
        assert "Firefox" in dialog._browser_package_status.text()
        assert dialog._browser_button.text() == "Repair"
        assert "Usable browser runtime not detected" in dialog._browser_status.text()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_runtime_manager_separates_external_capability_from_managed_package(
    monkeypatch, tmp_path: Path
) -> None:
    _app()
    managed_root = tmp_path / "runtime" / "browsers" / "playwright-test"
    external_root = tmp_path / "external-browsers"
    external_chromium = external_root / "chromium-1" / "chrome-win64" / "chrome.exe"
    browser_status = RuntimeComponentStatus(
        source="external", path=external_root, usable=True
    )
    adb_path = tmp_path / "external" / "adb.exe"
    adb_status = RuntimeComponentStatus(
        source="external",
        path=adb_path,
        usable=True,
        version="Android Debug Bridge version 1.0.41",
    )
    empty_managed = BrowserPayloadStatus(
        root=managed_root,
        manifest_present=False,
        markers_complete=False,
        usable=False,
        executable_paths=(),
        missing_engines=("chromium", "firefox", "webkit"),
    )
    external_payload = BrowserPayloadStatus(
        root=external_root,
        manifest_present=False,
        markers_complete=True,
        usable=True,
        executable_paths=(("chromium", external_chromium),),
        missing_engines=(),
    )
    monkeypatch.setattr(
        runtime_manager,
        "configure_runtime",
        lambda _data_dir: RuntimeConfiguration(browsers=browser_status, adb=adb_status),
    )
    monkeypatch.setattr(runtime_manager, "managed_browsers_dir", lambda _data_dir: managed_root)
    monkeypatch.setattr(
        runtime_manager,
        "inspect_browser_payload",
        lambda root: external_payload if Path(root) == external_root else empty_managed,
    )
    monkeypatch.setattr(runtime_manager, "managed_adb_path", lambda _data_dir: None)
    monkeypatch.setattr(runtime_manager, "managed_runtime_dir", lambda _data_dir: tmp_path / "runtime")

    dialog = runtime_manager.RuntimeManagerDialog(str(tmp_path))
    try:
        assert dialog._browser_package_status.text() == "Managed package not installed."
        assert "Usable browser runtime detected (external)" in dialog._browser_status.text()
        assert str(external_root) in dialog._browser_status.text()
        assert dialog._adb_package_status.text() == "Managed package not installed."
        assert "Usable ADB detected (external)" in dialog._adb_status.text()
        assert "Android Debug Bridge version 1.0.41" in dialog._adb_status.text()
    finally:
        dialog.close()
        dialog.deleteLater()
