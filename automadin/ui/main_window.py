"""Main application window for Automadin."""

from __future__ import annotations

import importlib.resources
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.adb_engine import AdbDeviceEngine, adb_path_from_db
from automadin.engine.session import DeviceSessionService
from automadin.models.device import DeviceType
from automadin.models.run_config import RunStatus
from automadin.models.schedule import RunHistoryOutcome, RunHistoryRecord
from automadin.services.database import DatabaseService
from automadin.services.database_transfer import (
    DatabaseInspection,
    DatabaseInspectionCancelledError,
    inspect_database,
    stage_database_import,
)
from automadin.services.screen_capture import ScreenCaptureService
from automadin.services.ocr import configured_tesseract_engine
from automadin.services.scheduler_host import SchedulerHost
from automadin.services.windows_startup import (
    scheduler_startup_enabled,
    set_scheduler_startup_enabled,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.services.browser_profile_metadata import (
    StoredBrowserProfile,
    compatible_engine_for_browser,
)
from automadin.services.browser_profile_storage import (
    BlockedPendingProfileDelete,
    ManagedProfileStorage,
    PendingProfileDelete,
    ProfileStagingRemnant,
    ProfileStorageStatus,
    inspect_profile_storage,
    inspect_pending_profile_deletes,
    list_managed_profile_storage,
    list_profile_staging_remnants,
)
from automadin.runtime_components import launch_runtime_manager
from automadin.ui.adb_connect_dialog import AdbConnectDialog
from automadin.ui.adb_path_dialog import AdbPathDialog
from automadin.ui.ocr_setup_dialog import OcrSetupDialog
from automadin.ui.android_file_browser import AndroidFileBrowserView
from automadin.ui.browser_profile_import_dialog import BrowserProfileImportDialog
from automadin.ui.capture_controller import CaptureOperationController
from automadin.ui.device_selector import DeviceSelectorWidget
from automadin.ui.diagnostics_dialog import (
    DiagnosticsDialog,
    DiagnosticsReportLoaderDialog,
)
from automadin.ui.flow_editor.editor_view import FlowEditorView
from automadin.ui.flow_runner.runner_view import FlowRunnerView
from automadin.ui.launcher.launcher_view import LauncherView
from automadin.ui.launcher.mini_run_window import MiniRunWindow
from automadin.ui.managed_emulators_dialog import ManagedEmulatorsDialog
from automadin.ui.management.management_view import ManagementView
from automadin.ui.portability_dialog import (
    ExportPortableBackupDialog,
    ImportPortableBackupDialog,
)
from automadin.ui.pushover_setup_dialog import PushoverSetupDialog
from automadin.ui.telegram_setup_dialog import TelegramSetupDialog
from automadin.ui.setup_center_view import SetupCenterView
from automadin.ui.theme import (
    COLORS,
    available_themes,
    get_active_theme_name,
    get_stylesheet,
)
from automadin.ui.utility.utility_view import UtilityView

logger = logging.getLogger(__name__)
PROFILE_SETUP_NOTICE_SETTING = "profile_identity_v2_notice_shown"
RETIRED_COMMAND_NOTICE_SETTING = "retired_command_v8_report"


class _DatabaseInspectionThread(QThread):
    """Review a database-only import without blocking the GUI thread."""

    def __init__(self, source: Path, parent=None) -> None:
        super().__init__(parent)
        self.source = source
        self.inspection: DatabaseInspection | None = None
        self.error: str | None = None
        self.cancelled = False

    def run(self) -> None:
        try:
            self.inspection = inspect_database(
                self.source,
                cancel=self.isInterruptionRequested,
            )
        except DatabaseInspectionCancelledError:
            self.cancelled = True
        except Exception:
            logger.exception("Database-only import inspection failed")
            self.error = (
                "The selected database could not be inspected safely. Review "
                "Diagnostics for details."
            )


class _DatabaseStagingThread(QThread):
    """Stage a reviewed database without blocking the GUI thread."""

    def __init__(self, source: Path, active_database: Path, parent=None) -> None:
        super().__init__(parent)
        self.source = source
        self.active_database = active_database
        self.error: str | None = None
        self.staged = False

    def run(self) -> None:
        try:
            stage_database_import(self.source, self.active_database)
        except Exception:
            logger.exception("Database-only import staging failed")
            self.error = "The database could not be staged safely. Review Diagnostics for details."
        else:
            self.staged = True


@dataclass(frozen=True, slots=True)
class ProfileRecoveryAttention:
    """Read-only startup inventory of profile data requiring a user decision."""

    pending_deletes: tuple[PendingProfileDelete, ...] = ()
    unlinked_storage: tuple[ManagedProfileStorage, ...] = ()
    staging_remnants: tuple[ProfileStagingRemnant, ...] = ()
    blocked_deletes: tuple[BlockedPendingProfileDelete, ...] = ()
    scan_errors: tuple[str, ...] = ()

    @property
    def issue_count(self) -> int:
        return (
            len(self.pending_deletes)
            + len(self.unlinked_storage)
            + len(self.staging_remnants)
            + len(self.blocked_deletes)
            + len(self.scan_errors)
        )


def _profiles_requiring_storage_setup(
    database: DatabaseService,
) -> list[tuple[StoredBrowserProfile, str]]:
    """Return every profile whose local manifest-backed storage is not ready."""
    needs_setup: list[tuple[StoredBrowserProfile, str]] = []
    for profile in database.list_profile_records():
        engine = profile.engine or compatible_engine_for_browser(profile.identity.browser)
        try:
            inspection = inspect_profile_storage(
                profile.profile_id,
                engine or None,
                legacy_storage_name=profile.legacy_storage_name,
            )
            if inspection.status is ProfileStorageStatus.READY:
                continue
            status = inspection.status.value
        except Exception:
            status = "inspection_failed"
        needs_setup.append((profile, status))
    return needs_setup


def _profile_setup_notice_fingerprint(
    profiles: list[tuple[StoredBrowserProfile, str]],
    recovery: ProfileRecoveryAttention | None = None,
) -> str:
    entries = [f"profile:{profile.profile_id}:{status}" for profile, status in profiles]
    recovery = recovery or ProfileRecoveryAttention()
    entries.extend(
        f"delete:{pending.profile_id}:{pending.engine}:{pending.tombstone_path}"
        for pending in recovery.pending_deletes
    )
    entries.extend(
        f"unlinked:{item.profile_id}:{item.engine}:{item.path}"
        for item in recovery.unlinked_storage
    )
    entries.extend(
        "staging:"
        f"{item.operation.value}:{item.profile_id}:{item.path}:"
        f"{item.has_matching_manifest}:{item.layout_ready}"
        for item in recovery.staging_remnants
    )
    entries.extend(
        "blocked:"
        f"{item.profile_id}:{item.reason.value}:"
        + "|".join(str(path) for path in item.candidate_paths)
        for item in recovery.blocked_deletes
    )
    entries.extend(f"scan-error:{error}" for error in recovery.scan_errors)
    payload = "\n".join(sorted(entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _profile_recovery_attention(
    database: DatabaseService,
) -> ProfileRecoveryAttention:
    """Inventory recovery data without attaching or deleting anything."""
    profile_ids = {profile.profile_id for profile in database.list_profile_records()}
    errors: list[str] = []
    pending: tuple[PendingProfileDelete, ...] = ()
    blocked: tuple[BlockedPendingProfileDelete, ...] = ()
    unlinked: tuple[ManagedProfileStorage, ...] = ()
    staging: tuple[ProfileStagingRemnant, ...] = ()

    try:
        delete_inventory = inspect_pending_profile_deletes()
        pending = tuple(
            item for item in delete_inventory.pending if item.profile_id not in profile_ids
        )
        blocked = delete_inventory.blocked
    except Exception as exc:
        logger.exception("Could not inspect interrupted browser-profile deletions")
        errors.append(str(exc) or type(exc).__name__)

    try:
        unlinked = tuple(
            item for item in list_managed_profile_storage() if item.profile_id not in profile_ids
        )
    except Exception as exc:
        logger.exception("Could not inventory managed browser-profile storage")
        errors.append(str(exc) or type(exc).__name__)

    try:
        staging = tuple(list_profile_staging_remnants())
    except Exception as exc:
        logger.exception("Could not inventory browser-profile staging data")
        errors.append(str(exc) or type(exc).__name__)

    return ProfileRecoveryAttention(
        pending_deletes=pending,
        unlinked_storage=unlinked,
        staging_remnants=staging,
        blocked_deletes=blocked,
        scan_errors=tuple(dict.fromkeys(errors)),
    )


def _navigation_stylesheet() -> str:
    return f"""
        QFrame#primaryNavigation {{
            background-color: {COLORS["bg_surface"]};
            border-right: 1px solid {COLORS["border_light"]};
        }}
        QFrame#primaryNavigation QLabel#brandLabel {{
            color: {COLORS["text_primary"]};
            font-size: 16pt;
            font-weight: 700;
            padding: 2px 0px;
        }}
        QFrame#primaryNavigation QLabel#navSection {{
            color: {COLORS["text_tertiary"]};
            font-size: 10pt;
            font-weight: 700;
            padding: 14px 10px 5px 10px;
        }}
        QFrame#primaryNavigation QPushButton[navButton="true"] {{
            text-align: left;
            border: none;
            background-color: transparent;
            color: {COLORS["text_secondary"]};
            padding: 9px 12px;
            border-radius: 6px;
            font-size: 10.5pt;
        }}
        QFrame#primaryNavigation QPushButton[navButton="true"]:hover {{
            background-color: {COLORS["bg_hover"]};
            color: {COLORS["text_primary"]};
        }}
        QFrame#primaryNavigation QPushButton[navButton="true"]:checked {{
            background-color: {COLORS["bg_elevated"]};
            color: {COLORS["text_primary"]};
            border-left: 3px solid {COLORS["primary"]};
            padding-left: 9px;
            font-weight: 600;
        }}
    """


class MainWindow(QMainWindow):
    """Top-level application window with tabbed interface."""

    def __init__(self, db: DatabaseService | None = None) -> None:
        super().__init__()
        self._db = db or DatabaseService()
        self._database_import_thread: QThread | None = None
        self._database_import_progress: QProgressDialog | None = None
        self._exit_requested = False
        self._system_shutdown_requested = False
        self._tray_icon: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self._tray_next_action: QAction | None = None
        self._tray_pause_action: QAction | None = None
        self._screen_text_ocr_engine = configured_tesseract_engine(self._db)
        self._session_service = DeviceSessionService(
            adb_engine=AdbDeviceEngine(db=self._db),
            screen_text_ocr_engine=self._screen_text_ocr_engine,
        )
        self._capture_controller = CaptureOperationController(
            ScreenCaptureService(self._session_service),
            parent=self,
        )

        self.setWindowTitle("Automadin")
        try:
            _ico = importlib.resources.files("automadin.resources").joinpath("icon.ico")
            self.setWindowIcon(QIcon(str(_ico)))
        except Exception:
            pass
        self.setMinimumSize(1280, 800)
        self.resize(1600, 960)
        self.setStyleSheet(get_stylesheet())

        self._build_menu_bar()
        self._build_central()
        self._mini_run_window = MiniRunWindow(
            self._db,
            self._launcher,
            open_runs_page=self._open_automadin_runs,
            parent=self,
        )
        self._launcher.mini_player_requested.connect(self._show_mini_run_window)
        self._build_status_bar()
        self._scheduler_host = SchedulerHost(
            self._db,
            self._launcher.scheduler_coordinator(),
            parent=self,
        )
        self._build_scheduler_tray()
        self._launcher.schedule_changed.connect(self._scheduler_host.refresh)
        self._scheduler_host.state_changed.connect(self._refresh_scheduler_tray)
        self._scheduler_host.state_changed.connect(
            self._launcher.refresh_scheduling_views
        )
        self._scheduler_host.run_finished.connect(self._scheduled_run_finished)
        self._scheduler_host.start()

    def activate_from_external_launch(self) -> None:
        """Restore and focus this application after a second launch request."""
        from automadin.instance_activation import activate_application_window

        activate_application_window(self)

    # ── Menu bar ──────────────────────────────────────────────────

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")

        export_portable_action = QAction("Export Portable Backup...", self)
        export_portable_action.triggered.connect(self._show_export_portable_backup)
        file_menu.addAction(export_portable_action)

        import_portable_action = QAction("Import Portable Backup...", self)
        import_portable_action.triggered.connect(self._show_import_portable_backup)
        file_menu.addAction(import_portable_action)

        file_menu.addSeparator()

        export_db_action = QAction("Export Database Only...", self)
        export_db_action.triggered.connect(self._export_database_backup)
        file_menu.addAction(export_db_action)

        import_db_action = QAction("Import Database Only...", self)
        import_db_action.triggered.connect(self._import_database_backup)
        file_menu.addAction(import_db_action)

        backup_notes_action = QAction("Backup Help...", self)
        backup_notes_action.triggered.connect(self._show_backup_notes)
        file_menu.addAction(backup_notes_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.request_exit)
        file_menu.addAction(exit_action)

        view_menu = menu_bar.addMenu("&View")
        theme_menu = view_menu.addMenu("Theme")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        active_theme = get_active_theme_name()

        for theme_name in available_themes():
            action = QAction(theme_name, self)
            action.setCheckable(True)
            action.setChecked(theme_name == active_theme)
            action.triggered.connect(
                lambda checked=False, name=theme_name: (
                    self._save_theme_preference(name) if checked else None
                )
            )
            theme_group.addAction(action)
            theme_menu.addAction(action)

        tools_menu = menu_bar.addMenu("&Tools")
        setup_action = QAction("Setup Center", self)
        setup_action.triggered.connect(self.show_setup_center)
        tools_menu.addAction(setup_action)

        tools_menu.addSeparator()

        if sys.platform == "win32":
            self._scheduler_startup_action = QAction(
                "Start scheduler when I sign in",
                self,
            )
            self._scheduler_startup_action.setCheckable(True)
            data_dir = Path(self._db.db_path).resolve().parent
            self._scheduler_startup_action.setChecked(
                scheduler_startup_enabled(data_dir)
            )
            self._scheduler_startup_action.toggled.connect(
                self._set_scheduler_startup
            )
            tools_menu.addAction(self._scheduler_startup_action)
            tools_menu.addSeparator()
        runtime_action = QAction("Runtime Components...", self)
        runtime_action.triggered.connect(self._show_runtime_manager)
        tools_menu.addAction(runtime_action)

        tools_menu.addSeparator()
        adb_connect_action = QAction("ADB Connect...", self)
        adb_connect_action.triggered.connect(self._show_adb_connect_dialog)
        tools_menu.addAction(adb_connect_action)

        adb_location_action = QAction("ADB Location...", self)
        adb_location_action.triggered.connect(self._show_adb_path_dialog)
        tools_menu.addAction(adb_location_action)

        ocr_setup_action = QAction("Screen Text Recognition...", self)
        ocr_setup_action.triggered.connect(self._show_ocr_setup_dialog)
        tools_menu.addAction(ocr_setup_action)

        pushover_setup_action = QAction("Pushover Notifications...", self)
        pushover_setup_action.triggered.connect(self._show_pushover_setup_dialog)
        tools_menu.addAction(pushover_setup_action)

        telegram_setup_action = QAction("Telegram Notifications...", self)
        telegram_setup_action.triggered.connect(self._show_telegram_setup_dialog)
        tools_menu.addAction(telegram_setup_action)

        tools_menu.addSeparator()

        import_profile_action = QAction("Browser Profiles...", self)
        import_profile_action.triggered.connect(self._show_browser_profile_import_dialog)
        tools_menu.addAction(import_profile_action)

        managed_emulators_action = QAction("Managed Emulators...", self)
        managed_emulators_action.triggered.connect(
            self._show_managed_emulators_dialog
        )
        tools_menu.addAction(managed_emulators_action)

        help_menu = menu_bar.addMenu("&Help")
        diagnostics_action = QAction("Diagnostics...", self)
        diagnostics_action.setObjectName("diagnosticsAction")
        diagnostics_action.triggered.connect(self._show_diagnostics)
        help_menu.addAction(diagnostics_action)

    # ── Central widget ────────────────────────────────────────────

    def _build_central(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Shared device selector bar — visible across all tabs
        self._device_selector = DeviceSelectorWidget(self._session_service, self._db, self)
        self._device_selector.setStyleSheet(
            f"QWidget#deviceSelectorBar {{ background-color: {COLORS['bg_surface']}; "
            f"border-bottom: 1px solid {COLORS['border']}; }}"
        )
        layout.addWidget(self._device_selector)

        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        navigation = QFrame()
        navigation.setObjectName("primaryNavigation")
        navigation.setFixedWidth(208)
        navigation.setStyleSheet(_navigation_stylesheet())
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(12, 16, 12, 14)
        nav_layout.setSpacing(3)
        brand = QLabel("AUTOMADIN")
        brand.setObjectName("brandLabel")
        nav_layout.addWidget(brand)
        subtitle = QLabel("Automation workspace")
        subtitle.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; padding: 0 0 10px 1px;"
        )
        nav_layout.addWidget(subtitle)

        self._tabs = QStackedWidget()
        self._tabs.setObjectName("workspaceStack")
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._page_indexes: dict[str, int] = {}
        self._nav_buttons: dict[str, QPushButton] = {}

        def add_section(label: str) -> None:
            section = QLabel(label.upper())
            section.setObjectName("navSection")
            nav_layout.addWidget(section)

        def add_page(page_key: str, label: str, widget: QWidget) -> None:
            if page_key in self._page_indexes:
                raise ValueError(f"Duplicate workspace page key: {page_key}")
            index = self._tabs.addWidget(widget)
            button = QPushButton(label)
            button.setProperty("navButton", True)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, key=page_key: self._show_page(key))
            self._nav_group.addButton(button, index)
            nav_layout.addWidget(button)
            self._page_indexes[page_key] = index
            self._nav_buttons[page_key] = button

        data_dir = Path(
            os.environ.get(
                "AUTOMADIN_DATA_DIR",
                os.path.dirname(os.path.abspath(self._db.db_path)),
            )
        )
        self._setup_center = SetupCenterView(
            self._db,
            data_dir,
            configured_adb_path=adb_path_from_db(self._db),
            parent=self,
        )
        self._setup_center.action_requested.connect(self._handle_setup_action)
        self._device_selector.session_connected.connect(self._sync_setup_connected_device)
        self._device_selector.session_disconnected.connect(self._sync_setup_connected_device)
        add_section("Start")
        add_page("setup", "Setup center", self._setup_center)

        # Flow Editor tab (pass shared device_selector)
        self._flow_editor = FlowEditorView(
            self._db,
            self._session_service,
            self._device_selector,
            capture_controller=self._capture_controller,
        )
        add_section("Build")
        add_page("flows", "Flows", self._flow_editor)

        # Flow Runner tab (pass shared device_selector)
        self._flow_runner = FlowRunnerView(self._db, self._session_service, self._device_selector)
        add_page("debugger", "Flow debugger", self._flow_runner)

        # Utility tab (pass shared device_selector)
        self._utility = UtilityView(
            self._db,
            self._session_service,
            self._device_selector,
            capture_controller=self._capture_controller,
        )
        add_section("Tools")
        add_page("vision", "Vision lab", self._utility)

        # Android file browser tab (pass shared device_selector)
        self._android_files = AndroidFileBrowserView(
            self._db, self._session_service, self._device_selector
        )
        add_page("android_files", "Android files", self._android_files)

        # Launcher tab (pass shared device_selector)
        self._launcher = LauncherView(self._db, self._session_service, self._device_selector)
        add_section("Run")
        add_page("runs", "Runs", self._launcher)

        # Management tab
        self._management = ManagementView(
            self._db,
            self._session_service,
            self._device_selector,
            capture_controller=self._capture_controller,
        )
        add_section("Library")
        add_page("library", "Automation library", self._management)

        # Keep cached selectors and open documents aligned with resource moves.
        def handle_resource_renamed(kind: str, old_name: str, new_name: str) -> None:
            self._flow_editor.on_resource_renamed(kind, old_name, new_name)
            self._management.refresh_resource_list(kind)
            if kind == "image":
                self._utility.refresh_image_library()
            if kind == "flow":
                self._launcher.refresh_run_list()
                self._flow_runner.refresh_flow_list()

        def handle_resources_renamed(
            changes: dict[str, list[tuple[str, str]]],
        ) -> None:
            self._flow_editor.on_resources_renamed(changes)
            for kind in ("flow", "image", "visual_rule"):
                if changes.get(kind):
                    self._management.refresh_resource_list(kind)
            if changes.get("image"):
                self._utility.refresh_image_library()
            if changes.get("flow"):
                self._launcher.refresh_run_list()
                self._flow_runner.refresh_flow_list()

        def handle_resource_deleted(kind: str, name: str) -> None:
            self._flow_editor.on_resource_deleted(kind, name)
            self._management.refresh_resource_list(kind)
            if kind == "image":
                self._utility.refresh_image_library()
            if kind == "flow":
                self._launcher.refresh_run_list()
                self._flow_runner.refresh_flow_list()

        def handle_resource_saved(kind: str, _name: str) -> None:
            self._management.refresh_resource_list(kind)
            if kind == "image":
                self._utility.refresh_image_library()

        self._management.resource_renamed.connect(handle_resource_renamed)
        self._management.resources_renamed.connect(handle_resources_renamed)
        self._utility.resource_renamed.connect(handle_resource_renamed)
        self._flow_editor.resource_renamed.connect(handle_resource_renamed)
        self._management.resource_deleted.connect(handle_resource_deleted)
        self._utility.resource_deleted.connect(handle_resource_deleted)
        self._flow_editor.resource_deleted.connect(handle_resource_deleted)
        self._flow_editor.resource_saved.connect(handle_resource_saved)
        self._management.run_config_changed.connect(self._launcher.refresh_run_list)
        self._launcher.run_config_changed.connect(self._management.refresh_run_list)

        nav_layout.addStretch(1)
        data_hint = QLabel("Using your current\nAutomadin data")
        data_hint.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; padding: 8px 10px;"
        )
        nav_layout.addWidget(data_hint)

        self._tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        shell_layout.addWidget(navigation)
        shell_layout.addWidget(self._tabs, 1)
        layout.addWidget(shell, 1)
        self.setCentralWidget(central)
        self._show_page("flows")

    # ── Status bar ────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        status_bar = self.statusBar()
        status_bar.setSizeGripEnabled(False)

    def _show_run_status_detail(self, run_id: str, source: str) -> None:
        self.activate_from_external_launch()
        if source == "debugger":
            self._show_page("debugger")
            return
        self._show_page("runs")
        self._launcher.focus_run(run_id, source)

    def _build_scheduler_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        QApplication.instance().setQuitOnLastWindowClosed(False)
        # The window remains alive while scheduler hosting is minimized, so it can
        # own the native tray object.  This also guarantees that the icon is
        # destroyed before QApplication tears down the Windows platform plugin.
        tray = QSystemTrayIcon(self.windowIcon(), self)
        tray.setToolTip("Automadin")
        menu = QMenu(self)
        menu.aboutToShow.connect(self._rebuild_tray_menu)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        self._tray_icon = tray
        self._tray_menu = menu
        self._rebuild_tray_menu()
        self._refresh_scheduler_tray()
        # Register immediately. A tray icon created only while handling Close
        # can be reported visible by Qt without ever appearing in Explorer.
        self._show_scheduler_tray()
        logger.info(
            "Scheduler tray registered: visible=%s icon_null=%s",
            tray.isVisible(),
            tray.icon().isNull(),
        )

    def _show_scheduler_tray(self, *, announce: bool = False) -> bool:
        """Register the tray icon before the main window can be hidden."""
        tray = getattr(self, "_tray_icon", None)
        if tray is None:
            return False
        tray.show()
        if announce:
            show_message = getattr(tray, "showMessage", None)
            if callable(show_message):
                show_message(
                    "Automadin is still running",
                    "Scheduled flows remain active. Use the Automadin icon "
                    "in the notification area (sometimes under ^) to open "
                    "or exit the app.",
                    QSystemTrayIcon.MessageIcon.Information,
                    7000,
                )
        return True

    def _refresh_scheduler_tray(self) -> None:
        if self._scheduler_host.has_enabled_schedules():
            self._show_scheduler_tray()

    def _rebuild_tray_menu(self) -> None:
        menu = self._tray_menu
        if menu is None:
            return
        menu.clear()

        running_title = menu.addAction("Running flows")
        running_title.setEnabled(False)
        active: list[tuple[str, object, object]] = []
        for service in (
            self._launcher.runner_service,
            self._flow_runner.runner_service,
        ):
            for run_id, worker in service.run_items():
                if not worker.isFinished():
                    active.append((run_id, worker, service))
        active.sort(key=lambda item: item[1].registered_monotonic)
        if not active:
            empty = menu.addAction("  No running flows")
            empty.setEnabled(False)
        for run_id, worker, service in active:
            status = worker.status
            run_menu = menu.addMenu(
                f"{worker.run_display_name} · {status.value.title()} · "
                f"{worker.target_display_name}"
            )
            if status is RunStatus.PAUSED:
                control = run_menu.addAction("Resume")
                control.triggered.connect(
                    lambda _checked=False, identity=run_id, owner=service: owner.resume_run(identity)
                )
            elif status is RunStatus.PAUSING:
                control = run_menu.addAction("Pausing…")
                control.setEnabled(False)
            elif status not in {
                RunStatus.STOPPING,
                RunStatus.CLEANING_UP,
                RunStatus.CLEANUP_PENDING,
                RunStatus.REQUIRES_ATTENTION,
                RunStatus.STOPPED,
                RunStatus.ERROR,
                RunStatus.COMPLETED,
            }:
                control = run_menu.addAction("Pause")
                control.triggered.connect(
                    lambda _checked=False, identity=run_id, owner=service: owner.pause_run(identity)
                )
            if status not in {
                RunStatus.STOPPING,
                RunStatus.CLEANING_UP,
                RunStatus.CLEANUP_PENDING,
                RunStatus.STOPPED,
                RunStatus.ERROR,
                RunStatus.COMPLETED,
            }:
                stop = run_menu.addAction("Stop")
                stop.triggered.connect(
                    lambda _checked=False, identity=run_id, owner=service: owner.stop_run(identity)
                )
            details = run_menu.addAction("Open details")
            details.triggered.connect(
                lambda _checked=False, identity=run_id, source=worker.run_source: self._show_run_status_detail(identity, source)
            )

        menu.addSeparator()
        pinned_title = menu.addAction("Pinned run setups")
        pinned_title.setEnabled(False)
        pinned = self._launcher.pinned_run_setups()
        if not pinned:
            empty = menu.addAction("  No pinned run setups")
            empty.setEnabled(False)
        for config_id, name in pinned:
            action = menu.addAction(f"▶ {name}")
            action.triggered.connect(
                lambda _checked=False, identity=config_id: self._start_pinned_run(identity)
            )

        menu.addSeparator()
        next_schedule = self._scheduler_host.next_schedule()
        if self._scheduler_host.paused:
            schedule_label = "Schedules paused"
        elif next_schedule is None or next_schedule.next_due_at_utc is None:
            schedule_label = "No scheduled runs"
        else:
            schedule_label = (
                f"Next: {next_schedule.name} · "
                f"{next_schedule.next_due_at_utc.astimezone():%Y-%m-%d %H:%M}"
            )
        self._tray_next_action = menu.addAction(schedule_label)
        self._tray_next_action.setEnabled(False)
        pause_action = menu.addAction("Pause all schedules")
        pause_action.setCheckable(True)
        pause_action.setChecked(self._scheduler_host.paused)
        pause_action.toggled.connect(self._scheduler_host.set_paused)
        self._tray_pause_action = pause_action

        menu.addSeparator()
        mini_action = menu.addAction("Open mini player")
        mini_action.triggered.connect(self._show_mini_run_window)
        open_action = menu.addAction("Open Automadin")
        open_action.triggered.connect(self.activate_from_external_launch)
        exit_action = menu.addAction("Exit Automadin")
        exit_action.triggered.connect(self.request_exit)

    def _start_pinned_run(self, run_config_id: int) -> None:
        self.activate_from_external_launch()
        self._show_page("runs")
        self._launcher.start_run_config_by_id(run_config_id)

    def _show_mini_run_window(self) -> None:
        self._mini_run_window.refresh_run_setups()
        self._mini_run_window.refresh_runs()
        self._mini_run_window.show_player()

    def _open_automadin_runs(self) -> None:
        self.activate_from_external_launch()
        self._show_page("runs")

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.activate_from_external_launch()

    def _scheduled_run_finished(self, record: RunHistoryRecord) -> None:
        self._launcher.refresh_scheduling_views()
        tray = self._tray_icon
        if tray is None or record.outcome not in {
            RunHistoryOutcome.FAILED,
            RunHistoryOutcome.BLOCKED,
            RunHistoryOutcome.REQUIRES_ATTENTION,
        }:
            return
        detail = record.message or "Open History for details."
        tray.showMessage(
            f"Scheduled flow {record.outcome.value}",
            f"{record.schedule_name}: {detail}",
            QSystemTrayIcon.MessageIcon.Warning,
            8000,
        )

    def _set_scheduler_startup(self, enabled: bool) -> None:
        action = self._scheduler_startup_action
        try:
            set_scheduler_startup_enabled(
                Path(self._db.db_path).resolve().parent,
                enabled,
            )
        except Exception:
            logger.exception("Could not update scheduler sign-in launch")
            action.blockSignals(True)
            action.setChecked(not enabled)
            action.blockSignals(False)
            QMessageBox.warning(
                self,
                "Scheduler Startup",
                "Automadin could not update the current-user sign-in startup "
                "setting. Review Diagnostics for details.",
            )

    def can_start_hidden(self) -> bool:
        return self._tray_icon is not None

    def has_enabled_schedules(self) -> bool:
        return self._scheduler_host.has_enabled_schedules()

    def start_hidden(self) -> None:
        MainWindow._show_scheduler_tray(self, announce=True)
        # Never leave a scheduler host reachable only through the notification
        # area: Windows may suppress an icon even when Qt reports it visible.
        self.showMinimized()

    def request_exit(self) -> None:
        self._exit_requested = True
        self.close()

    def prepare_for_system_shutdown(self) -> None:
        """Request bounded cleanup without interacting with or blocking Windows."""
        if self._system_shutdown_requested:
            return
        self._system_shutdown_requested = True
        self._exit_requested = True

        modal = QApplication.activeModalWidget()
        if modal is not None:
            reject = getattr(modal, "reject", None)
            if callable(reject):
                reject()
            else:
                modal.hide()

        scheduler_host = getattr(self, "_scheduler_host", None)
        if scheduler_host is not None:
            scheduler_host.stop()
        tray_icon = getattr(self, "_tray_icon", None)
        if tray_icon is not None:
            tray_icon.hide()
        mini_run_window = getattr(self, "_mini_run_window", None)
        if mini_run_window is not None:
            mini_run_window.persist_state()
            mini_run_window.hide()

        for owner_name in (
            "_setup_center",
            "_flow_editor",
            "_capture_controller",
            "_flow_runner",
            "_launcher",
            "_device_selector",
            "_android_files",
        ):
            owner = getattr(self, owner_name, None)
            shutdown = getattr(owner, "shutdown", None)
            if not callable(shutdown):
                continue
            try:
                shutdown()
            except BaseException:
                logger.exception(
                    "Best-effort system shutdown request failed for %s",
                    owner_name,
                )

    def _show_page(self, page_key: str) -> None:
        """Select one workspace page using its stable semantic key."""
        try:
            index = self._page_indexes[page_key]
            button = self._nav_buttons[page_key]
        except KeyError as exc:
            raise ValueError(f"Unknown workspace page: {page_key}") from exc
        self._tabs.setCurrentIndex(index)
        button.setChecked(True)

    def show_setup_center(self) -> None:
        """Open the permanent readiness page and refresh stale visible facts."""
        had_snapshot = self._setup_center.snapshot is not None
        self._setup_center.set_configured_adb_path(adb_path_from_db(self._db))
        self._show_page("setup")
        if had_snapshot:
            self._setup_center.refresh()

    def show_startup_guidance(self) -> None:
        """Choose non-competing startup guidance from persisted setup state."""
        retired_notice = getattr(self, "show_retired_command_notice", None)
        if callable(retired_notice):
            retired_notice()
        if self._setup_center.should_auto_open():
            self.show_setup_center()
            return
        self.show_profile_setup_notice()

    def show_retired_command_notice(self) -> None:
        """Report actions intentionally removed by the schema-8 upgrade."""
        raw_report = self._db.get_app_setting(RETIRED_COMMAND_NOTICE_SETTING)
        if not raw_report:
            return
        try:
            report = json.loads(raw_report)
            flows = report.get("flows", {}) if isinstance(report, dict) else {}
        except (TypeError, json.JSONDecodeError):
            flows = {}
        if not isinstance(flows, dict):
            flows = {}

        rows = []
        total = 0
        for name, raw_count in sorted(flows.items(), key=lambda item: str(item[0]).casefold()):
            if not isinstance(name, str) or type(raw_count) is not int or raw_count <= 0:
                continue
            total += raw_count
            rows.append(f"• {name} — {raw_count} action{'s' if raw_count != 1 else ''}")
        details = "\n".join(rows[:12])
        if len(rows) > 12:
            details += f"\n• …and {len(rows) - 12} more flows"

        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Information)
        message.setWindowTitle("Moving Target Actions Removed")
        message.setText(
            f"Automadin removed {total} retired Click Moving Target "
            f"action{'s' if total != 1 else ''}."
        )
        message.setInformativeText(
            (details + "\n\n" if details else "")
            + "The affected flows remain loadable. Add Click Dynamic Target where "
            "you still need this behavior."
        )
        message.addButton(QMessageBox.StandardButton.Ok)
        message.exec()
        self._db.delete_app_setting(RETIRED_COMMAND_NOTICE_SETTING)

    def _sync_setup_connected_device(self, *_args: object) -> None:
        session = self._device_selector.get_selected_session()
        self._setup_center.set_connected_device(session.device if session is not None else None)

    def _handle_setup_action(self, action_id: str) -> None:
        """Delegate explicit Setup actions to the existing workflow owners."""
        if action_id == "flows":
            self._show_page("flows")
            return
        if action_id == "continue_flows":
            self._show_page("flows")
            self._setup_center.acknowledge_current()
            return
        if action_id == "runtime":
            self._show_runtime_manager()
            return
        if action_id == "ocr":
            self._show_ocr_setup_dialog()
            return
        if action_id == "profiles":
            self._show_browser_profile_import_dialog()
            return
        if action_id == "backup_help":
            self._show_backup_notes()
            return
        if action_id == "restore_portable":
            self._show_import_portable_backup()
            return
        if action_id == "import_database":
            self._import_database_backup()
            return
        target_types = {
            "target_browser": DeviceType.BROWSER,
            "target_android": DeviceType.ANDROID,
            "target_windows": DeviceType.WINDOWS,
        }
        device_type = target_types.get(action_id)
        if device_type is not None:
            self._device_selector.discover_device_type(device_type)
            return
        logger.warning("Ignored unknown Setup Center action: %s", action_id)

    def show_profile_setup_notice(self) -> None:
        """Explain why imported or migrated profiles need a local storage choice."""
        profiles = _profiles_requiring_storage_setup(self._db)
        recovery = _profile_recovery_attention(self._db)
        if not profiles and not recovery.issue_count:
            return
        fingerprint = _profile_setup_notice_fingerprint(
            profiles,
            recovery,
        )
        if self._db.get_app_setting(PROFILE_SETUP_NOTICE_SETTING) == fingerprint:
            return

        count = len(profiles)
        labels = "\n".join(
            f"• {profile.display_label} — {status.replace('_', ' ')}"
            for profile, status in profiles[:5]
        )
        remaining = count - min(count, 5)
        if remaining:
            labels += f"\n• …and {remaining} more"

        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Information)
        if profiles:
            message.setWindowTitle("Browser Profiles Need Setup")
            message.setText(
                f"{count} browser profile{'s' if count != 1 else ''} need"
                " an explicit storage choice."
            )
            details = (
                "Automadin kept the imported names and saved-run references, but did "
                "not connect them to same-name folders automatically. This prevents a "
                "database from opening an unrelated local browser account.\n\n"
                f"{labels}\n\n"
                "Review each profile to use its existing Automadin folder or import "
                "a copy."
            )
        else:
            message.setWindowTitle("Browser Profile Recovery Needed")
            recovery_count = recovery.issue_count
            message.setText(
                f"{recovery_count} browser-profile recovery "
                f"item{'s' if recovery_count != 1 else ''} need review."
            )
            details = (
                "Automadin found managed, interrupted, or partially copied browser "
                "data that is not safe to attach or delete automatically. This can "
                "happen after a database import or an interrupted profile operation."
                "\n\nReview the Recovery tab in Browser Profiles. Keeping data is the "
                "default."
            )
        if recovery.issue_count and profiles:
            recovery_count = recovery.issue_count
            details += (
                "\n\n"
                f"{recovery_count} profile recovery item"
                f"{'s also need' if recovery_count != 1 else ' also needs'} review."
            )
        message.setInformativeText(details)
        review_button = message.addButton(
            "Review Browser Profiles",
            QMessageBox.ButtonRole.ActionRole,
        )
        message.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        self._db.set_app_setting(PROFILE_SETUP_NOTICE_SETTING, fingerprint)

        if message.clickedButton() is review_button:
            self._show_browser_profile_import_dialog()
        else:
            self.statusBar().showMessage(
                "Browser profiles need attention under Tools > Browser Profiles.",
                9000,
            )

    def _export_database_backup(self) -> None:
        data_dir = os.path.dirname(os.path.abspath(self._db.db_path))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default_path = os.path.join(data_dir, f"automadin-backup-{stamp}.db")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Database Only",
            default_path,
            "SQLite Database (*.db);;All Files (*)",
        )
        if not path:
            return
        try:
            self._db.export_database(path)
        except Exception as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
            return
        QMessageBox.information(
            self,
            "Database Exported",
            f"Database backup exported to:\n{path}",
        )

    def _show_export_portable_backup(self) -> None:
        dialog = ExportPortableBackupDialog(
            self._db,
            self,
            lease_registry=self._session_service.browser_profile_lease_registry,
        )
        dialog.exec()

    def _show_import_portable_backup(self) -> None:
        dialog = ImportPortableBackupDialog(
            self._db,
            self,
            lease_registry=self._session_service.browser_profile_lease_registry,
        )
        dialog.exec()
        if dialog.staged_result is None:
            return
        close_reply = QMessageBox.question(
            self,
            "Restart to Restore Portable Backup",
            "The portable backup is staged safely. Close Automadin now, then start "
            "it again to activate the restored workspace?\n\n"
            "Any edits you make before restarting will not become part of the "
            "restored workspace. The current database will instead be preserved in "
            "the automatic safety backup created during activation.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if close_reply == QMessageBox.StandardButton.Yes:
            self.close()

    def _import_database_backup(self) -> None:
        if self._database_import_thread is not None:
            self.statusBar().showMessage(
                "A database-only import operation is already active.",
                7000,
            )
            return
        data_dir = os.path.dirname(os.path.abspath(self._db.db_path))
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Database Only",
            data_dir,
            "SQLite Database (*.db);;All Files (*)",
        )
        if not path:
            return
        self._start_database_inspection(Path(path))

    def _database_import_progress_dialog(
        self,
        label: str,
        *,
        cancellable: bool,
    ) -> QProgressDialog:
        progress = QProgressDialog(label, "Cancel" if cancellable else "", 0, 0, self)
        progress.setWindowTitle("Import Database Only")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        if not cancellable:
            progress.setCancelButton(None)
        progress.show()
        return progress

    def _start_database_inspection(self, source: Path) -> None:
        thread = _DatabaseInspectionThread(source, self)
        progress = self._database_import_progress_dialog(
            "Reviewing and validating the selected database…",
            cancellable=True,
        )
        progress.canceled.connect(thread.requestInterruption)
        thread.finished.connect(lambda: self._finish_database_inspection(thread, progress))
        self._database_import_thread = thread
        self._database_import_progress = progress
        try:
            thread.start()
        except BaseException:
            logger.exception("Could not start database inspection worker")
            startup_uncertain = qthread_start_may_have_succeeded(thread)
            if startup_uncertain:
                progress.setLabelText(
                    "Database review started but startup could not be confirmed. "
                    "Wait for it to finish before closing Automadin."
                )
            else:
                self._database_import_thread = None
                self._database_import_progress = None
                try:
                    progress.close()
                    progress.deleteLater()
                    thread.deleteLater()
                except Exception:
                    logger.exception("Could not release failed database inspection worker")
            QMessageBox.critical(
                self,
                (
                    "Database Review Startup Uncertain"
                    if startup_uncertain
                    else "Database Review Could Not Start"
                ),
                (
                    "Database review may have begun, but startup could not be "
                    "confirmed. Wait for its final result before trying again."
                    if startup_uncertain
                    else "The database review worker could not be started. Review "
                    "Diagnostics for details, then try again."
                ),
            )

    def _finish_database_inspection(
        self,
        thread: _DatabaseInspectionThread,
        progress: QProgressDialog,
    ) -> None:
        if self._database_import_thread is not thread:
            return
        self._database_import_thread = None
        self._database_import_progress = None
        try:
            progress.close()
            progress.deleteLater()
            thread.deleteLater()
        except Exception:
            logger.exception("Could not release database inspection UI resources")
        # Honour a cancellation that arrived after validation returned but before
        # Qt delivered the worker's finished signal.
        if thread.cancelled or thread.isInterruptionRequested():
            self.statusBar().showMessage(
                "Database-only import review cancelled. Nothing was staged.",
                7000,
            )
            return
        if thread.error is not None or thread.inspection is None:
            QMessageBox.warning(
                self,
                "Invalid Database",
                thread.error or "The selected database could not be inspected.",
            )
            return
        self._confirm_database_import(thread.source, thread.inspection)

    def _confirm_database_import(
        self,
        source: Path,
        inspection: DatabaseInspection,
    ) -> None:
        reply = QMessageBox.question(
            self,
            "Import Database Only",
            "The imported database will replace the active database after Automadin "
            "is closed and started again. A safety backup of the current database "
            "will be created automatically.\n\n"
            f"Flows: {inspection.flow_count}\n"
            f"Images: {inspection.image_count}\n"
            f"Saved profile names: {inspection.profile_count}\n\n"
            "Browser profile folders are not contained in the database and will not "
            "be changed.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._start_database_staging(source)

    def _start_database_staging(self, source: Path) -> None:
        thread = _DatabaseStagingThread(
            source,
            Path(self._db.db_path),
            self,
        )
        progress = self._database_import_progress_dialog(
            "Staging the validated database safely. This step cannot be interrupted…",
            cancellable=False,
        )
        thread.finished.connect(lambda: self._finish_database_staging(thread, progress))
        self._database_import_thread = thread
        self._database_import_progress = progress
        try:
            thread.start()
        except BaseException:
            logger.exception("Could not start database staging worker")
            startup_uncertain = qthread_start_may_have_succeeded(thread)
            if startup_uncertain:
                progress.setLabelText(
                    "Database staging started but startup could not be confirmed. "
                    "Wait for it to finish before closing Automadin."
                )
            else:
                self._database_import_thread = None
                self._database_import_progress = None
                try:
                    progress.close()
                    progress.deleteLater()
                    thread.deleteLater()
                except Exception:
                    logger.exception("Could not release failed database staging worker")
            QMessageBox.critical(
                self,
                (
                    "Database Staging Startup Uncertain"
                    if startup_uncertain
                    else "Database Staging Could Not Start"
                ),
                (
                    "Database staging may have begun, but startup could not be "
                    "confirmed. Wait for its final result before trying again."
                    if startup_uncertain
                    else "The database staging worker could not be started. Review "
                    "Diagnostics for details, then try again."
                ),
            )

    def _finish_database_staging(
        self,
        thread: _DatabaseStagingThread,
        progress: QProgressDialog,
    ) -> None:
        if self._database_import_thread is not thread:
            return
        self._database_import_thread = None
        self._database_import_progress = None
        try:
            progress.close()
            progress.deleteLater()
            thread.deleteLater()
        except Exception:
            logger.exception("Could not release database staging UI resources")
        if thread.error is not None or not thread.staged:
            QMessageBox.critical(
                self,
                "Import Failed",
                thread.error or "The database could not be staged.",
            )
            return
        close_reply = QMessageBox.question(
            self,
            "Import Ready",
            "The database has been validated and staged safely. It will be imported "
            "the next time Automadin starts.\n\nAny edits you make before restarting "
            "will not become part of the imported workspace. The current database "
            "will instead be preserved in the automatic safety backup created "
            "during import.\n\nClose Automadin now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if close_reply == QMessageBox.StandardButton.Yes:
            self.close()

    def _show_backup_notes(self) -> None:
        data_dir = os.path.dirname(os.path.abspath(self._db.db_path))
        profiles_dir = os.environ.get("AUTOMADIN_PROFILES_DIR", os.path.join(data_dir, "profiles"))
        QMessageBox.information(
            self,
            "Backup Help",
            "Use File > Export Portable Backup when moving a workspace to another "
            "Automadin installation. It includes the database and any ready browser "
            "profiles you select. Portable backups contain sensitive browser data "
            "and are not encrypted.\n\n"
            "Use the database-only commands when you intentionally want flows, "
            "images, run setups, settings, and profile records without browser "
            "profile folders.\n\n"
            f"Database:\n{self._db.db_path}\n\n"
            "Contains flows, flow versions, template images, run setups, and "
            "browser profile names.\n\n"
            f"Optional but important for browser logins/session state:\n{profiles_dir}\n\n"
            "No other Automadin user-data files are required by the current code. "
            "If a flow saves screenshots or outputs to a custom path, back up "
            "those output folders separately only if you need those files.",
        )

    def _show_diagnostics(self) -> None:
        data_dir = Path(os.path.dirname(os.path.abspath(self._db.db_path)))
        loader = DiagnosticsReportLoaderDialog(
            self._db.db_path,
            data_dir,
            self,
        )
        loader_result = loader.exec()
        if loader_result and loader.report is not None:
            DiagnosticsDialog(loader.report, self).exec()

    def _save_theme_preference(self, theme_name: str) -> None:
        saved_theme = self._db.get_app_setting("theme", get_active_theme_name())
        if theme_name == saved_theme:
            return
        self._db.set_app_setting("theme", theme_name)
        self.statusBar().showMessage(
            f"Theme saved: {theme_name}. Restart Automadin to apply it.", 7000
        )
        QMessageBox.information(
            self,
            "Theme Saved",
            f"Theme set to {theme_name}.\n\nRestart Automadin to apply it.",
        )

    def _show_adb_connect_dialog(self) -> None:
        dlg = AdbConnectDialog(self._db, self)
        dlg.exec()
        if dlg.connected:
            self._device_selector.refresh_android_devices()
            self._setup_center.refresh()
            self.statusBar().showMessage(
                "ADB target connected. Refreshing Android devices...", 7000
            )

    def _show_runtime_manager(self) -> None:
        data_dir = os.environ.get(
            "AUTOMADIN_DATA_DIR", os.path.dirname(os.path.abspath(self._db.db_path))
        )
        try:
            launch_runtime_manager(data_dir, adb_path_from_db(self._db))
        except Exception as exc:
            QMessageBox.critical(self, "Runtime Manager", str(exc))
            return
        self.statusBar().showMessage("Runtime Manager opened in a separate process.", 5000)

    def _show_adb_path_dialog(self) -> None:
        dlg = AdbPathDialog(self._db, self)
        dlg.exec()
        if not dlg.saved:
            return
        self._session_service.set_adb_engine(AdbDeviceEngine(db=self._db))
        self._setup_center.set_configured_adb_path(adb_path_from_db(self._db))
        self._setup_center.refresh()
        self._device_selector.refresh_android_devices()
        self.statusBar().showMessage("ADB location updated. Refreshing Android devices...", 7000)

    def _show_ocr_setup_dialog(self) -> None:
        dlg = OcrSetupDialog(self._db, self)
        dlg.exec()
        if not dlg.saved:
            return
        self._screen_text_ocr_engine.invalidate()
        self._setup_center.refresh()
        self.statusBar().showMessage("Screen text recognition updated.", 7000)

    def _show_pushover_setup_dialog(self) -> None:
        dlg = PushoverSetupDialog(self._db, self)
        dlg.exec()
        if dlg.saved:
            self.statusBar().showMessage("Pushover notification settings updated.", 7000)

    def _show_telegram_setup_dialog(self) -> None:
        dlg = TelegramSetupDialog(self._db, self)
        dlg.exec()
        if dlg.saved:
            self.statusBar().showMessage("Telegram notification settings updated.", 7000)

    def _show_browser_profile_import_dialog(self) -> None:
        dlg = BrowserProfileImportDialog(
            self._db,
            self,
            lease_registry=self._session_service.browser_profile_lease_registry,
            data_dir=Path(self._db.db_path).resolve().parent,
            open_runtime_components=self._show_runtime_manager,
        )
        dlg.exec()
        if not dlg.profiles_changed:
            return
        self._device_selector.refresh_profiles()
        self._setup_center.refresh()
        message = (
            f"Browser profile ready: {dlg.imported_profile}"
            if dlg.imported_profile
            else "Browser profiles updated"
        )
        self.statusBar().showMessage(message, 7000)

    def _show_managed_emulators_dialog(self) -> None:
        dlg = ManagedEmulatorsDialog(self._db, self)
        dlg.exec()
        if dlg.profiles_changed:
            self.statusBar().showMessage("Managed emulator profiles updated", 7000)

    # ── Shutdown ──────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        """Clean up resources on window close."""
        application = QApplication.instance()
        saving_session = bool(
            application is not None
            and callable(getattr(application, "isSavingSession", None))
            and application.isSavingSession()
        )
        if getattr(self, "_system_shutdown_requested", False) or saving_session:
            self.prepare_for_system_shutdown()
            event.accept()
            QMainWindow.closeEvent(self, event)
            return
        scheduler_host = getattr(self, "_scheduler_host", None)
        tray_icon = getattr(self, "_tray_icon", None)
        if (
            not getattr(self, "_exit_requested", False)
            and scheduler_host is not None
            and scheduler_host.has_enabled_schedules()
            and tray_icon is not None
        ):
            choice = self._scheduler_close_choice()
            if choice == "tray":
                MainWindow._show_scheduler_tray(self, announce=True)
                self.showMinimized()
                event.ignore()
                return
            if choice == "cancel":
                event.ignore()
                return
            self._exit_requested = True
        if event.spontaneous():
            # A native title-bar close arrives inside Qt's Windows callback.  Tearing
            # down the complete PySide widget tree from that callback can fault in
            # Qt's platform cleanup.  Re-enter through the same queued exit path as
            # the File > Exit action after any scheduler choice has been resolved.
            event.ignore()
            QTimer.singleShot(0, self.request_exit)
            return
        database_import_thread = self._database_import_thread
        if database_import_thread is not None:
            if isinstance(database_import_thread, _DatabaseInspectionThread):
                database_import_thread.requestInterruption()
                detail = (
                    "Database review cancellation was requested. Please wait for "
                    "validation to stop before closing Automadin."
                )
            else:
                detail = (
                    "Database staging is finishing a safe copy step and cannot be "
                    "interrupted. Please wait before closing Automadin."
                )
            self.statusBar().showMessage(detail, 8000)
            event.ignore()
            return
        if not self._setup_center.shutdown():
            self.statusBar().showMessage(
                "Stopping the Setup Center readiness check. Please wait a moment, "
                "then close Automadin again.",
                8000,
            )
            event.ignore()
            return
        if not self._flow_editor.confirm_discard_changes():
            event.ignore()
            return
        if not self._flow_editor.shutdown():
            self.statusBar().showMessage(
                "Finishing the current Run Step. Please wait a moment, then close Automadin again.",
                8000,
            )
            event.ignore()
            return
        if not self._capture_controller.shutdown():
            self.statusBar().showMessage(
                "Finishing screenshot capture. Please wait a moment, then close Automadin again.",
                8000,
            )
            event.ignore()
            return
        if not self._flow_runner.shutdown():
            self.statusBar().showMessage(
                "Stopping the flow debugger. Please wait a moment, then close Automadin again.",
                8000,
            )
            event.ignore()
            return
        if not self._launcher.shutdown():
            self.statusBar().showMessage(
                "Stopping saved runs. Please wait a moment, then close Automadin again.",
                8000,
            )
            event.ignore()
            return
        if not self._device_selector.shutdown():
            self.statusBar().showMessage(
                "Finishing device discovery or connection. Please wait a moment, "
                "then close Automadin again.",
                8000,
            )
            event.ignore()
            return
        if not self._android_files.shutdown():
            abort_selector_shutdown = getattr(
                self._device_selector,
                "abort_shutdown",
                None,
            )
            if callable(abort_selector_shutdown):
                abort_selector_shutdown()
            abort_android_shutdown = getattr(
                self._android_files,
                "abort_shutdown",
                None,
            )
            if callable(abort_android_shutdown):
                abort_android_shutdown()
            self.statusBar().showMessage(
                "Stopping an Android file operation. Please wait a moment, then "
                "close Automadin again.",
                8000,
            )
            event.ignore()
            return
        try:
            self._session_service.close_all()
        except Exception:
            logger.exception("Could not close all device sessions")
            abort_selector_shutdown = getattr(
                self._device_selector,
                "abort_shutdown",
                None,
            )
            if callable(abort_selector_shutdown):
                abort_selector_shutdown()
            abort_android_shutdown = getattr(
                self._android_files,
                "abort_shutdown",
                None,
            )
            if callable(abort_android_shutdown):
                abort_android_shutdown()
            self.statusBar().showMessage(
                "A device session is still closing. Please wait and try again.",
                8000,
            )
            QMessageBox.warning(
                self,
                "Device Still Closing",
                "Automadin kept the window open because a device session could "
                "not be closed safely. Review Diagnostics for details, then try "
                "again.",
            )
            event.ignore()
            return
        if scheduler_host is not None:
            scheduler_host.stop()
        if tray_icon is not None:
            tray_icon.hide()
        mini_run_window = getattr(self, "_mini_run_window", None)
        if mini_run_window is not None:
            mini_run_window.persist_state()
            mini_run_window.hide()
        self._db.close()
        super().closeEvent(event)
        if event.isAccepted():
            # The scheduler tray disables Qt's implicit last-window shutdown so
            # Automadin can remain active while minimized. Once the guarded
            # shutdown path succeeds, explicitly end that application lifetime.
            application = QApplication.instance()
            if application is not None:
                application.quit()

    def _scheduler_close_choice(self) -> str:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Scheduled flows are enabled")
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setText("Keep Automadin running so scheduled flows can start?")
        dialog.setInformativeText(
            "Automadin cannot run schedules after it fully exits. If kept "
            "running, it remains minimized on the taskbar and also registers "
            "a notification-area icon (sometimes under ^)."
        )
        tray_button = dialog.addButton(
            "Keep running minimized",
            QMessageBox.ButtonRole.AcceptRole,
        )
        exit_button = dialog.addButton(
            "Exit and pause schedules",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel_button = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel_button)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is tray_button:
            return "tray"
        if clicked is exit_button:
            return "exit"
        return "cancel"

    # ── Public accessors ──────────────────────────────────────────

    @property
    def db(self) -> DatabaseService:
        return self._db

    @property
    def tabs(self) -> QStackedWidget:
        return self._tabs
