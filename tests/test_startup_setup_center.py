from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMainWindow

from automadin import app
from automadin.ui import main_window
from automadin.models.device import DeviceType
from automadin.services.database import DatabaseService
from automadin.services.setup_center import (
    CURRENT_SETUP_VERSION,
    SETUP_ACKNOWLEDGED_VERSION_SETTING,
    SETUP_SELECTED_TRACK_SETTING,
    SetupTrack,
)
from automadin.ui.main_window import MainWindow
from automadin.ui.setup_center_view import SetupCenterView


def test_active_import_resets_only_setup_acknowledgement(tmp_path: Path) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"))
    try:
        database.set_app_setting(
            SETUP_ACKNOWLEDGED_VERSION_SETTING,
            str(CURRENT_SETUP_VERSION),
        )
        database.set_app_setting(
            SETUP_SELECTED_TRACK_SETTING,
            SetupTrack.ANDROID.value,
        )
        database.set_app_setting("theme", "Midnight")
        database.set_app_setting("profile_identity_v2_notice_shown", "fingerprint")

        app._reset_setup_acknowledgement_after_import(
            database,
            SimpleNamespace(applied=True),
        )

        assert (
            database.get_app_setting(SETUP_ACKNOWLEDGED_VERSION_SETTING) is None
        )
        assert (
            database.get_app_setting(SETUP_SELECTED_TRACK_SETTING)
            == SetupTrack.ANDROID.value
        )
        assert database.get_app_setting("theme") == "Midnight"
        assert (
            database.get_app_setting("profile_identity_v2_notice_shown")
            == "fingerprint"
        )
    finally:
        database.close()


def test_failed_import_does_not_reset_setup_acknowledgement(tmp_path: Path) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"))
    try:
        database.set_app_setting(
            SETUP_ACKNOWLEDGED_VERSION_SETTING,
            str(CURRENT_SETUP_VERSION),
        )

        app._reset_setup_acknowledgement_after_import(
            database,
            SimpleNamespace(
                applied=False,
                database_activated=False,
                active_database_matches_import=False,
            ),
        )

        assert database.get_app_setting(
            SETUP_ACKNOWLEDGED_VERSION_SETTING
        ) == str(CURRENT_SETUP_VERSION)
    finally:
        database.close()


def test_startup_guidance_opens_setup_when_unacknowledged() -> None:
    events: list[str] = []
    window = SimpleNamespace(
        _setup_center=SimpleNamespace(should_auto_open=lambda: True),
        show_setup_center=lambda: events.append("setup"),
        show_profile_setup_notice=lambda: events.append("profile-notice"),
    )

    MainWindow.show_startup_guidance(window)

    assert events == ["setup"]


def test_acknowledged_startup_retains_profile_guidance() -> None:
    events: list[str] = []
    window = SimpleNamespace(
        _setup_center=SimpleNamespace(should_auto_open=lambda: False),
        show_setup_center=lambda: events.append("setup"),
        show_profile_setup_notice=lambda: events.append("profile-notice"),
    )

    MainWindow.show_startup_guidance(window)

    assert events == ["profile-notice"]


def test_setup_actions_route_to_stable_window_commands() -> None:
    events: list[str] = []
    device_selector = SimpleNamespace(
        discover_device_type=lambda device_type: events.append(
            f"discover:{device_type.value}"
        )
    )
    window = SimpleNamespace(
        _device_selector=device_selector,
        _setup_center=SimpleNamespace(
            acknowledge_current=lambda: events.append("acknowledge")
        ),
        _show_page=lambda page_key: events.append(f"page:{page_key}"),
        _show_runtime_manager=lambda: events.append("runtime"),
        _show_browser_profile_import_dialog=lambda: events.append("profiles"),
        _show_backup_notes=lambda: events.append("backup-help"),
        _show_import_portable_backup=lambda: events.append("restore-portable"),
        _import_database_backup=lambda: events.append("import-database"),
    )

    actions = (
        "flows",
        "runtime",
        "profiles",
        "backup_help",
        "restore_portable",
        "import_database",
        "target_browser",
        "target_android",
        "target_windows",
    )
    for action in actions:
        MainWindow._handle_setup_action(window, action)

    assert events == [
        "page:flows",
        "runtime",
        "profiles",
        "backup-help",
        "restore-portable",
        "import-database",
        f"discover:{DeviceType.BROWSER.value}",
        f"discover:{DeviceType.ANDROID.value}",
        f"discover:{DeviceType.WINDOWS.value}",
    ]


def test_continue_to_flows_acknowledges_only_after_navigation() -> None:
    events: list[str] = []
    window = SimpleNamespace(
        _setup_center=SimpleNamespace(
            acknowledge_current=lambda: events.append("acknowledge")
        ),
        _show_page=lambda page_key: events.append(f"page:{page_key}"),
    )

    MainWindow._handle_setup_action(window, "flows")
    assert events == ["page:flows"]

    events.clear()
    MainWindow._handle_setup_action(window, "continue_flows")
    assert events == ["page:flows", "acknowledge"]


def test_show_setup_center_and_page_keys_select_stable_widgets(
    tmp_path: Path,
) -> None:
    qt_app = QApplication.instance() or QApplication([])
    database = DatabaseService(str(tmp_path / "automadin.db"))
    window = MainWindow(database)
    try:
        window.show_setup_center()
        qt_app.processEvents()

        assert window._tabs.currentWidget() is window._setup_center
        assert window._nav_group.checkedId() == window._tabs.currentIndex()

        window._show_page("flows")

        assert window._tabs.currentWidget() is window._flow_editor
        assert window._nav_group.checkedId() == window._tabs.currentIndex()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()
        database.close()


def test_close_stops_setup_check_before_other_shutdown_mutations() -> None:
    events: list[str] = []
    window = SimpleNamespace(
        _database_import_thread=None,
        _setup_center=SimpleNamespace(
            shutdown=lambda: events.append("setup") or False
        ),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *_args: None),
    )
    event = QCloseEvent()

    MainWindow.closeEvent(window, event)

    assert events == ["setup"]
    assert not event.isAccepted()


def test_accepted_close_quits_application_after_tray_disabled_implicit_exit(
    monkeypatch,
) -> None:
    qt_app = QApplication.instance() or QApplication([])
    events: list[str] = []
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._exit_requested = False
    window._scheduler_host = SimpleNamespace(
        has_enabled_schedules=lambda: False,
        stop=lambda: events.append("scheduler-stop"),
    )
    window._tray_icon = SimpleNamespace(hide=lambda: events.append("tray-hide"))
    window._database_import_thread = None
    window._setup_center = SimpleNamespace(shutdown=lambda: True)
    window._flow_editor = SimpleNamespace(
        confirm_discard_changes=lambda: True,
        shutdown=lambda: True,
    )
    window._capture_controller = SimpleNamespace(shutdown=lambda: True)
    window._flow_runner = SimpleNamespace(shutdown=lambda: True)
    window._launcher = SimpleNamespace(shutdown=lambda: True)
    window._device_selector = SimpleNamespace(shutdown=lambda: True)
    window._android_files = SimpleNamespace(shutdown=lambda: True)
    window._session_service = SimpleNamespace(close_all=lambda: None)
    window._db = SimpleNamespace(close=lambda: events.append("database-close"))
    fake_application = SimpleNamespace(quit=lambda: events.append("application-quit"))
    monkeypatch.setattr(
        main_window,
        "QApplication",
        SimpleNamespace(instance=lambda: fake_application),
    )
    event = QCloseEvent()
    try:
        MainWindow.closeEvent(window, event)

        assert event.isAccepted()
        assert events == [
            "scheduler-stop",
            "tray-hide",
            "database-close",
            "application-quit",
        ]
    finally:
        window.deleteLater()
        qt_app.processEvents()


def test_close_can_keep_enabled_schedules_running_in_tray() -> None:
    events: list[str] = []
    window = SimpleNamespace(
        _exit_requested=False,
        _scheduler_host=SimpleNamespace(has_enabled_schedules=lambda: True),
        _tray_icon=SimpleNamespace(
            show=lambda: events.append("tray"),
            showMessage=lambda *_args: events.append("message"),
        ),
        _scheduler_close_choice=lambda: "tray",
        showMinimized=lambda: events.append("minimize"),
    )
    event = QCloseEvent()

    MainWindow.closeEvent(window, event)

    assert events == ["tray", "message", "minimize"]
    assert not event.isAccepted()


def test_scheduler_tray_is_registered_before_window_is_minimized() -> None:
    events: list[str] = []
    window = SimpleNamespace(
        _tray_icon=SimpleNamespace(
            show=lambda: events.append("tray"),
            showMessage=lambda *_args: events.append("message"),
        ),
        showMinimized=lambda: events.append("minimize"),
    )

    MainWindow.start_hidden(window)

    assert events == ["tray", "message", "minimize"]


def test_system_shutdown_requests_cleanup_without_prompts_or_waiting() -> None:
    events: list[str] = []
    window = SimpleNamespace(
        _system_shutdown_requested=False,
        _exit_requested=False,
        _scheduler_host=SimpleNamespace(stop=lambda: events.append("scheduler")),
        _tray_icon=SimpleNamespace(hide=lambda: events.append("tray")),
    )
    for name in (
        "_setup_center",
        "_flow_editor",
        "_capture_controller",
        "_flow_runner",
        "_launcher",
        "_device_selector",
        "_android_files",
    ):
        setattr(
            window,
            name,
            SimpleNamespace(shutdown=lambda identity=name: events.append(identity) or False),
        )

    MainWindow.prepare_for_system_shutdown(window)  # type: ignore[arg-type]

    assert window._system_shutdown_requested
    assert window._exit_requested
    assert events[:2] == ["scheduler", "tray"]
    assert len(events) == 9


def test_system_shutdown_close_is_never_refused() -> None:
    qt_app = QApplication.instance() or QApplication([])
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._system_shutdown_requested = True
    event = QCloseEvent()
    try:
        MainWindow.closeEvent(window, event)
        assert event.isAccepted()
    finally:
        window.deleteLater()
        qt_app.processEvents()


def test_later_close_refusal_keeps_idle_setup_center_usable(
    tmp_path: Path,
) -> None:
    qt_app = QApplication.instance() or QApplication([])
    database = DatabaseService(str(tmp_path / "automadin.db"))
    setup_center = SetupCenterView(database, tmp_path)
    window = SimpleNamespace(
        _database_import_thread=None,
        _setup_center=setup_center,
        _flow_editor=SimpleNamespace(confirm_discard_changes=lambda: False),
    )
    event = QCloseEvent()
    try:
        MainWindow.closeEvent(window, event)

        assert not event.isAccepted()
        assert setup_center.isEnabled()
        assert not setup_center._shutting_down
    finally:
        setup_center.close()
        setup_center.deleteLater()
        qt_app.processEvents()
        database.close()
