"""Launcher view — manage and run multiple saved run configurations."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from automadin.execution.context import FlowContext
from automadin.execution.build_executor import build_executor
from automadin.execution.flow_values import (
    FlowInvocationValues,
    FlowRuntimeValueError,
    resolve_flow_inputs,
)
from automadin.execution.run_coordinator import (
    RunCapacityError,
    RunCoordinator,
    RunPreparationError,
    acquire_profile_lease as _acquire_launcher_profile_lease,
    browser_device_with_headless as _browser_device_with_headless,
    device_from_run_config as _device_from_id,
    launcher_preflight_report as _launcher_preflight_report,
    profile_reference_error as _profile_reference_error,
)
from automadin.execution.runner import (
    BrowserAwareRunWorker,
    FlowRunWorker,
    RunnerService,
)
from automadin.engine.browser_engine import BrowserSessionTeardownError
from automadin.engine.adb_engine import AndroidTargetReadinessError
from automadin.engine.windows_engine import WindowsDeviceEngine
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    Device,
    DeviceSession,
    DeviceType,
    WindowsDevice,
    device_from_saved_id,
    display_name_for_saved_device_id,
    saved_device_id_for_device,
)
from automadin.models.managed_emulator import (
    EmulatorCleanupPolicy,
    ManagedEmulatorProfile,
)
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow
from automadin.models.flow_parameter import ParameterValue
from automadin.models.run_config import (
    RunConfig,
    RunStatus,
    SavedRunConfig,
)
from automadin.models.typed_value import TypedValue
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    OwnerProfileLease,
)
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import DeviceOperationConflictError
from automadin.services.device_operation_leases import DeviceOperationLease
from automadin.services.flow_preflight import (
    PreflightMode,
    preflight_flow,
    target_kind_for_device,
)
from automadin.services.managed_emulator_lifecycle import (
    ManagedEmulatorAcquisition,
    ManagedEmulatorCancelled,
    ManagedEmulatorLifecycle,
    ManagedEmulatorLifecycleError,
)
from automadin.services.browser_profile_metadata import profile_aware_device_name
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.launcher.run_config_dialog import RunConfigDialog
from automadin.ui.launcher.quick_run_dialog import QuickRunDialog
from automadin.ui.launcher.parameter_overrides import (
    ManualParameterOverridesDialog,
)
from automadin.ui.launcher.input_overrides import ManualInputOverridesDialog
from automadin.ui.launcher.run_panel import RunPanel, RunPanelSnapshot
from automadin.ui.launcher.schedule_views import (
    ScheduleHistoryView,
    ScheduleListView,
)
from automadin.ui.preflight_dialog import confirm_execution_preflight
from automadin.ui.theme import COLORS
from automadin.ui.widgets.buttons import configure_button
from automadin.ui.widgets.wrapping_layout import WrappingLayout

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectedTargetRunWorker",
    "LauncherRunWorker",
    "LauncherView",
    "_acquire_launcher_profile_lease",
    "_browser_device_with_headless",
    "_device_from_id",
    "_launcher_preflight_report",
    "_profile_reference_error",
]


def _launcher_context(
    session: DeviceSession,
    variables: Mapping[str, Any] | object | None = None,
    parameters: Mapping[str, ParameterValue] | None = None,
    *,
    flow_values: FlowInvocationValues | None = None,
) -> FlowContext:
    """Create a launcher context seeded with one run setup's variables.

    ``FlowContext`` interpolation is string based, while saved run setups are
    deserialized from JSON and can therefore contain scalar values.  Normalize
    them once at the launcher boundary so every handler sees consistent text.
    """
    ctx = FlowContext(session, parameters=parameters, flow_values=flow_values)
    safe_variables = variables if flow_values is None and isinstance(variables, Mapping) else {}
    ctx.variables.update(
        {str(name): "" if value is None else str(value) for name, value in safe_variables.items()}
    )
    return ctx


class ConnectedTargetRunWorker(FlowRunWorker):
    """Run against an existing connected session without owning its lifecycle."""

    def __init__(
        self,
        *args: Any,
        session_service: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._session_service = session_service

    def run(self) -> None:
        device = self._ctx.session.device
        if isinstance(device, AndroidDevice):
            self._set_status(RunStatus.CHECKING_ANDROID)
            try:
                self._ctx.session = self._session_service.ensure_android_session(device)
            except AndroidTargetReadinessError as exc:
                self.error_occurred.emit(exc.public_message)
                self._set_status(RunStatus.ERROR)
                self.completed.emit(exc.public_message)
                self.release_reserved_device_operation()
                return
            except BaseException:
                logger.exception("Quick Run Android readiness failed")
                message = (
                    "The connected Android target could not be verified. "
                    "Reconnect it and try Quick Run again."
                )
                self.error_occurred.emit(message)
                self._set_status(RunStatus.ERROR)
                self.completed.emit(message)
                self.release_reserved_device_operation()
                return
        super().run()


def _saved_device_display_name(db: DatabaseService, device_id: object) -> str:
    """Format a saved target without exposing a profile UUID as its main label."""
    try:
        device = device_from_saved_id(device_id)
    except (TypeError, ValueError, IndexError):
        return display_name_for_saved_device_id(device_id)
    if not isinstance(device, BrowserDevice):
        return display_name_for_saved_device_id(device_id)

    labels_provider = getattr(db, "profile_display_labels", None)
    if callable(labels_provider):
        labels = labels_provider()
    elif device.config.profile_id:
        labels = {device.config.profile_id: db.get_profile_display_label(device.config.profile_id)}
    else:
        labels = {}
    return profile_aware_device_name(device, labels)


class LauncherRunWorker(FlowRunWorker):
    """FlowRunWorker that creates the device session on the worker thread.

    Playwright's sync_api uses greenlets that are bound to the thread that
    created the browser.  By deferring session creation to ``run()`` (which
    executes on the QThread), the Playwright greenlet lives on the same
    thread that will later call browser methods — avoiding the
    "Cannot switch to a different thread" error.
    """

    def __init__(
        self,
        executor: Any,
        flow: AutomationFlow,
        config: RunConfig,
        device: Device,
        session_service: Any,
        profile_lease: OwnerProfileLease | None = None,
        variables: Mapping[str, Any] | object | None = None,
        parameter_values: Mapping[str, ParameterValue] | None = None,
        input_values: Mapping[str, TypedValue] | None = None,
        managed_emulator_profile: ManagedEmulatorProfile | None = None,
        emulator_cleanup_policy: EmulatorCleanupPolicy | None = None,
        managed_emulator_lifecycle: ManagedEmulatorLifecycle | None = None,
        parent: Any = None,
    ) -> None:
        # Keep one context for the worker lifetime; run() only replaces its
        # placeholder session so pause/stop requests cannot be lost.
        dummy_session = DeviceSession(device=device, data={})
        self._initial_variables = dict(variables) if isinstance(variables, Mapping) else {}
        invocation_values = (
            FlowInvocationValues(flow.flow_id, flow.value_catalog, input_values or {})
            if flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
            else None
        )
        super().__init__(
            executor,
            flow,
            _launcher_context(
                dummy_session,
                self._initial_variables,
                parameter_values,
                flow_values=invocation_values,
            ),
            config,
            parent,
        )
        self._device = device
        self._session_service = session_service
        self._profile_lease = profile_lease
        self._profile_cleanup_leases: list[OwnerProfileLease] = (
            [profile_lease] if profile_lease is not None else []
        )
        self._profile_release_permitted = True
        self._managed_emulator_profile = managed_emulator_profile
        self._emulator_cleanup_policy = emulator_cleanup_policy
        self._managed_emulator_lease: DeviceOperationLease | None = None
        discovery_service = getattr(self._session_service, "discovery", None)
        self._managed_emulator_lifecycle = managed_emulator_lifecycle or (
            ManagedEmulatorLifecycle(
                lambda: [
                    device
                    for device in discovery_service.list_devices(DeviceType.ANDROID)
                    if isinstance(device, AndroidDevice)
                ],
                discover_managed_target=getattr(
                    discovery_service,
                    "discover_managed_emulator_target",
                    None,
                ),
            )
        )

    def run(self) -> None:
        """Create the session on the worker thread, then run the flow.

        For browser devices we create a completely independent Playwright
        instance on THIS thread — no shared state with any other thread.
        Playwright's sync_api uses greenlets that are pinned to the OS thread
        that called ``sync_playwright().start()``, so the browser MUST be
        launched on the same thread that will later drive it.
        """
        session_data = None
        session: DeviceSession | None = None
        managed_acquisition: ManagedEmulatorAcquisition | None = None
        startup_teardown_confirmed = True
        try:
            if self._managed_emulator_profile is not None:
                managed_acquisition = self._managed_emulator_lifecycle.acquire(
                    self._managed_emulator_profile,
                    cancelled=lambda: self._ctx.is_stopped,
                    status=self._set_emulator_status,
                    log=self.log_message.emit,
                )
                self._device = managed_acquisition.device
                self._ctx.session.device = self._device
            if isinstance(self._device, BrowserDevice):
                from automadin.engine.browser_engine import BrowserDeviceEngine

                self._set_status(RunStatus.CONNECTING)
                self.log_message.emit("Connecting to browser target…")
                logger.debug("Creating browser session on worker thread")
                engine = BrowserDeviceEngine()
                session_data = engine.connect(
                    self._device,
                    profile_lease=self._profile_lease,
                )
                self.log_message.emit("Browser target connected")
                session = DeviceSession(
                    device=self._device,
                    data={"browser_data": session_data},
                )
                logger.debug("Browser session created successfully")
            elif isinstance(self._device, WindowsDevice):
                session = self._resolve_windows_session()
            elif isinstance(self._device, AndroidDevice):
                self._set_status(RunStatus.CHECKING_ANDROID)
                self.log_message.emit(f"Checking Android target '{self._device.name}'")
                session = self._session_service.ensure_android_session(self._device)
            else:
                session = self._session_service.create_session(self._device)

            # Keep one context for the worker's entire lifetime.  Stop may be
            # requested while a slow session connection is still in progress;
            # replacing the context here would discard that cancellation.
            self._ctx.session = session
        except BaseException as exc:
            if isinstance(exc, ManagedEmulatorLifecycleError):
                managed_acquisition = exc.partial_acquisition
            if isinstance(exc, BrowserSessionTeardownError):
                startup_teardown_confirmed = False
            logger.exception("LauncherRunWorker session creation failed")
            if isinstance(exc, ManagedEmulatorCancelled):
                message = str(exc)
                self._set_status(RunStatus.STOPPED)
            elif isinstance(exc, ManagedEmulatorLifecycleError):
                message = str(exc)
                self.error_occurred.emit(message)
                self._set_status(RunStatus.ERROR)
            elif isinstance(exc, AndroidTargetReadinessError):
                message = exc.public_message
                self.error_occurred.emit(message)
                self._set_status(RunStatus.ERROR)
            else:
                message = (
                    "Could not connect to the selected target. Check its runtime and "
                    "connection, then review Diagnostics for details."
                )
                self.error_occurred.emit(message)
                self._set_status(RunStatus.ERROR)
            self.completed.emit(message)
        else:
            try:
                super().run()
            except BaseException:
                # FlowRunWorker normally converts execution failures to ERROR,
                # but cleanup must also survive unexpected worker exceptions.
                logger.exception("LauncherRunWorker execution failed unexpectedly")
                self._set_status(RunStatus.ERROR)
                self.completed.emit("Execution failed unexpectedly")
        finally:
            # The browser must close before another owner can acquire its
            # persistent storage.  Lease release is cross-thread and idempotent.
            teardown_confirmed = startup_teardown_confirmed
            if session_data is not None:
                try:
                    session_data.close()
                except BaseException:
                    teardown_confirmed = False
                    logger.exception("Failed to confirm browser session teardown")
                    self._set_status(RunStatus.ERROR)
                    self.error_occurred.emit(
                        "Browser cleanup could not be confirmed. This profile "
                        "will remain locked until Automadin restarts. Review "
                        "Diagnostics for details."
                    )
            if not teardown_confirmed:
                self._profile_release_permitted = False
            if self._profile_lease is not None and teardown_confirmed:
                self.release_reserved_profile_lease()
            elif self._profile_lease is not None:
                logger.error(
                    "Retaining launcher profile lease %s after uncertain teardown",
                    self._profile_lease.profile_id,
                )
            self._cleanup_managed_emulator(managed_acquisition, session)
            self.release_reserved_device_operation()
            self.release_reserved_managed_emulator_lease()

    def _set_emulator_status(self, message: str) -> None:
        status_by_message = {
            "Acquiring emulator": RunStatus.ACQUIRING_EMULATOR,
            "Starting emulator": RunStatus.STARTING_EMULATOR,
            "Waiting for Android": RunStatus.WAITING_FOR_ANDROID,
            "Android unavailable": RunStatus.ERROR,
            "Cleaning up": RunStatus.CLEANING_UP,
        }
        status = status_by_message.get(message)
        if status is not None:
            self._set_status(status)
        self.log_message.emit(message)

    def _cleanup_managed_emulator(
        self,
        acquisition: ManagedEmulatorAcquisition | None,
        session: DeviceSession | None,
    ) -> None:
        policy = self._emulator_cleanup_policy
        if acquisition is None or policy is None:
            return
        prior_status = self.status
        should_close = policy is EmulatorCleanupPolicy.ALWAYS_CLOSE or (
            policy is EmulatorCleanupPolicy.CLOSE_IF_STARTED and acquisition.started_by_automadin
        )
        session_error = ""
        if should_close and session is not None and isinstance(session.device, AndroidDevice):
            try:
                self._session_service.close_session(session.session_id)
            except BaseException as exc:
                session_error = str(exc)
                logger.exception("Could not close managed Android session")
        try:
            result = self._managed_emulator_lifecycle.cleanup(
                acquisition,
                policy,
                status=self._set_emulator_status,
                log=self.log_message.emit,
            )
        except ManagedEmulatorLifecycleError as exc:
            message = f"Managed emulator cleanup failed: {exc}"
            if session_error:
                message += f" Android session cleanup also failed: {session_error}"
            logger.exception("Managed emulator cleanup failed")
            self.error_occurred.emit(message)
            self._set_status(RunStatus.ERROR)
            self.completed.emit(message)
            return
        if session_error:
            message = f"Android session cleanup failed: {session_error}"
            self.error_occurred.emit(message)
            self._set_status(RunStatus.ERROR)
            self.completed.emit(message)
        elif result.warning:
            self.log_message.emit(result.warning)
        if self.status is RunStatus.CLEANING_UP:
            self._set_status(prior_status)

    def attach_reserved_managed_emulator_lease(
        self,
        lease: DeviceOperationLease,
    ) -> None:
        if self._managed_emulator_lease is not None or self.isRunning():
            lease.release()
            raise RuntimeError("Managed emulator ownership is already established")
        self._managed_emulator_lease = lease

    def release_reserved_managed_emulator_lease(self) -> bool:
        lease = self._managed_emulator_lease
        if lease is None:
            return True
        try:
            lease.release()
        except BaseException:
            logger.exception("Failed to release managed emulator reservation")
            return False
        if lease is self._managed_emulator_lease:
            self._managed_emulator_lease = None
        return True

    def release_reserved_profile_lease(self) -> bool:
        """Release an unstarted/finished launcher's retryable profile ownership."""
        if not self._profile_cleanup_leases:
            return True
        if not self._profile_release_permitted:
            lease = self._profile_lease or self._profile_cleanup_leases[0]
            logger.error(
                "Refusing to release quarantined launcher profile lease %s",
                lease.profile_id,
            )
            return False
        retained: list[OwnerProfileLease] = []
        for lease in self._profile_cleanup_leases:
            try:
                lease.release()
            except BaseException:
                logger.exception("Failed to release browser profile reservation")
                retained.append(lease)
            else:
                if lease is self._profile_lease:
                    self._profile_lease = None
        self._profile_cleanup_leases = retained
        return not retained

    def attach_reserved_profile_lease(
        self,
        lease: OwnerProfileLease | None,
    ) -> None:
        """Transfer a newly acquired profile lease to this unstarted worker."""
        if lease is None:
            return
        # Retain cleanup ownership before any state inspection can fail.  If the
        # handoff invariant is violated, the existing panel can still retry
        # release through release_reserved_profile_lease().
        self._profile_cleanup_leases.append(lease)
        if self._profile_lease is not None or self.isRunning():
            raise RuntimeError("Launcher profile ownership is already established")
        self._profile_lease = lease

    def _resolve_windows_session(self) -> DeviceSession:
        """Reuse or create a Windows session, refreshing stale HWNDs by title."""
        engine = WindowsDeviceEngine()

        # First try an exact HWND match (legacy sessions).
        existing = self._session_service.get_session_by_device(self._device.id)
        if existing is None and self._device.title:
            for session in self._session_service.list_sessions():
                if not isinstance(session.device, WindowsDevice):
                    continue
                if session.device.title == self._device.title:
                    existing = session
                    break

        if existing is not None:
            win_data = existing.get_data("windows_data")
            if isinstance(win_data, dict):
                win_data = engine.resolve_session_data(win_data)
                existing.put_data("windows_data", win_data)
                existing.device = WindowsDevice(
                    hwnd=int(win_data["hwnd"]),
                    title=str(win_data.get("title", self._device.title)),
                    executable_path=str(
                        win_data.get(
                            "executable_path",
                            self._device.executable_path,
                        )
                    ),
                    auto_start=bool(win_data.get("auto_start", self._device.auto_start)),
                )
                return existing

            try:
                self._session_service.close_session(existing.session_id)
            except Exception:
                logger.warning("Failed to close stale Windows session", exc_info=True)

        return self._session_service.create_session(self._device)


def _launcher_worker_for_saved_config(
    executor: Any,
    flow: AutomationFlow,
    saved_config: SavedRunConfig,
    device: Device,
    session_service: Any,
    profile_lease: OwnerProfileLease | None = None,
    parameter_values: Mapping[str, ParameterValue] | None = None,
    input_values: Mapping[str, TypedValue] | None = None,
    connected_session: DeviceSession | None = None,
    managed_emulator_profile: ManagedEmulatorProfile | None = None,
    emulator_cleanup_policy: EmulatorCleanupPolicy | None = None,
) -> FlowRunWorker:
    """Build the worker used by the saved-run launch path."""
    if connected_session is not None:
        invocation_values = (
            FlowInvocationValues(flow.flow_id, flow.value_catalog, input_values or {})
            if flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
            else None
        )
        return BrowserAwareRunWorker(
            executor,
            flow,
            _launcher_context(
                connected_session,
                saved_config.variables,
                parameter_values,
                flow_values=invocation_values,
            ),
            saved_config.config,
            profile_use_kind="run-setup",
            profile_use_label=f"Run setup: {saved_config.name}",
            unavailable_context="run setup",
        )
    return LauncherRunWorker(
        executor=executor,
        flow=flow,
        config=saved_config.config,
        device=device,
        session_service=session_service,
        profile_lease=profile_lease,
        variables=saved_config.variables,
        parameter_values=parameter_values,
        input_values=input_values,
        managed_emulator_profile=managed_emulator_profile,
        emulator_cleanup_policy=emulator_cleanup_policy,
    )


class LauncherView(QWidget):
    """Left: saved run config tree + CRUD.  Right: active run panels."""

    run_config_changed = Signal()
    schedule_changed = Signal()
    retained_runs_changed = Signal()
    mini_player_requested = Signal()

    _COL_NAME = 0
    _COL_FLOW = 1
    _COL_DEVICE = 2
    _COL_MODE = 3
    _COL_PIN = 4

    def __init__(
        self,
        db: DatabaseService,
        session_service: Any = None,
        device_selector: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._session_service = session_service
        self._device_selector = device_selector
        self._runner_service = RunnerService(max_concurrent=6)
        self._configs: list[SavedRunConfig] = []
        self._configs_by_id: dict[int, SavedRunConfig] = {}
        self._config_ids_by_name: dict[str, int] = {}
        self._run_panels: dict[str, RunPanel] = {}

        self._build_ui()
        self._refresh_table()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._workspace_tabs = QTabWidget()
        run_setups_page = QWidget()
        run_setups_layout = QVBoxLayout(run_setups_page)
        run_setups_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left panel: config table + buttons ──
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.setSpacing(6)

        title = QLabel("Run setups")
        title.setStyleSheet(
            f"font-weight: bold; font-size: 11.25pt; color: {COLORS['text_primary']}; padding: 4px 0;"
        )
        left_layout.addWidget(title)

        # Toolbar
        toolbar_widget = QWidget()
        toolbar = WrappingLayout(
            toolbar_widget,
            horizontal_spacing=4,
            vertical_spacing=4,
        )
        toolbar.setContentsMargins(0, 0, 0, 0)
        self._toolbar_widget = toolbar_widget

        self._new_btn = QPushButton("+ New")
        self._new_btn.clicked.connect(self._on_new)
        configure_button(self._new_btn, semantic="success", density="compact")
        toolbar.addWidget(self._new_btn)

        self._edit_btn = QPushButton("Edit")
        self._edit_btn.clicked.connect(self._on_edit)
        configure_button(self._edit_btn, semantic="primary", density="compact")
        toolbar.addWidget(self._edit_btn)

        self._run_btn = QPushButton("\u25b6 Run")
        self._run_btn.clicked.connect(self._on_run)
        configure_button(self._run_btn, semantic="success", density="compact")
        toolbar.addWidget(self._run_btn)

        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._on_delete)
        configure_button(self._delete_btn, semantic="danger", density="compact")
        toolbar.addWidget(self._delete_btn)

        refresh_btn = QPushButton("\u21bb Refresh")
        configure_button(refresh_btn, density="compact")
        refresh_btn.clicked.connect(self._refresh_table)
        toolbar.addWidget(refresh_btn)

        left_layout.addWidget(toolbar_widget)

        filter_toolbar = QHBoxLayout()
        filter_toolbar.setSpacing(4)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search setups, flows, targets, or folders")
        self._search.setClearButtonEnabled(True)
        self._search.setAccessibleName("Search Run Setups")
        self._search.textChanged.connect(self._populate_tree)
        filter_toolbar.addWidget(self._search, 1)
        self._quick_run_btn = QPushButton("▶ Quick Run")
        self._quick_run_btn.setToolTip("Run one flow once on the currently connected target")
        self._quick_run_btn.clicked.connect(self._on_quick_run)
        configure_button(self._quick_run_btn, semantic="success", density="compact")
        filter_toolbar.addWidget(self._quick_run_btn)
        left_layout.addLayout(filter_toolbar)

        # Run config tree
        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["Name", "Flow", "Device", "Mode", "Pin"])
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(18)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._tree.setAlternatingRowColors(True)
        self._tree.setStyleSheet(
            f"QTreeWidget {{ border: 1px solid {COLORS['border']}; "
            f"border-radius: 6px; background-color: {COLORS['bg_darkest']}; "
            f"alternate-background-color: {COLORS['bg_surface']}; }}"
            f"QTreeWidget::item {{ border-bottom: 1px solid {COLORS['border_light']}; }}"
            f"QTreeWidget::item:selected {{ background-color: {COLORS['bg_elevated']}; }}"
        )
        self._tree.header().setStretchLastSection(False)
        for col in range(5):
            self._tree.header().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemDoubleClicked.connect(self._on_tree_item_double_clicked)
        self._tree.itemClicked.connect(self._on_tree_item_clicked)
        self._pin_shortcut = QShortcut(QKeySequence("Alt+P"), self._tree)
        self._pin_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._pin_shortcut.activated.connect(self._toggle_selected_pin)
        self._tree.setToolTip("Select a Run Setup and press Alt+P to pin or unpin it.")
        left_layout.addWidget(self._tree)

        # ── Right panel: active runs ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 8, 8, 8)
        right_layout.setSpacing(6)

        right_header = QHBoxLayout()
        right_title = QLabel("Active Runs")
        right_title.setStyleSheet(
            f"font-weight: bold; font-size: 11.25pt; color: {COLORS['text_primary']}; padding: 4px 0;"
        )
        right_header.addWidget(right_title)
        right_header.addStretch()
        mini_player_btn = QPushButton("Mini player")
        configure_button(mini_player_btn, appearance="outline", density="compact")
        mini_player_btn.setToolTip("Open the compact run controller")
        mini_player_btn.clicked.connect(self.mini_player_requested.emit)
        self._mini_player_btn = mini_player_btn
        right_header.addWidget(mini_player_btn)
        right_layout.addLayout(right_header)

        self._runs_scroll = QScrollArea()
        self._runs_scroll.setWidgetResizable(True)
        self._runs_container = QWidget()
        self._runs_layout = QVBoxLayout(self._runs_container)
        self._runs_layout.setContentsMargins(0, 0, 0, 0)
        self._runs_layout.setSpacing(6)
        self._runs_layout.addStretch()
        self._runs_scroll.setWidget(self._runs_container)
        right_layout.addWidget(self._runs_scroll)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([350, 650])
        run_setups_layout.addWidget(splitter)

        self._schedule_view = ScheduleListView(self._db)
        self._history_view = ScheduleHistoryView(self._db)
        self._schedule_view.changed.connect(self._schedule_changed)
        self._schedule_view.run_now_requested.connect(self._run_config_by_id)
        self._workspace_tabs.addTab(run_setups_page, "Run setups")
        self._workspace_tabs.addTab(self._schedule_view, "Schedules")
        self._workspace_tabs.addTab(self._history_view, "History")
        self._workspace_tabs.currentChanged.connect(self._workspace_tab_changed)
        root.addWidget(self._workspace_tabs)

    def _workspace_tab_changed(self, _index: int) -> None:
        current = self._workspace_tabs.currentWidget()
        if current is self._schedule_view:
            self._schedule_view.refresh()
        elif current is self._history_view:
            self._history_view.refresh()

    # ── Tree management ──────────────────────────────────────────────

    def _refresh_table(self) -> None:
        names = self._db.list_run_configs()
        self._configs.clear()
        self._configs_by_id.clear()
        self._config_ids_by_name.clear()
        for name in names:
            try:
                cfg = self._db.load_run_config(name)
                if cfg is not None:
                    self._configs.append(cfg)
                    config_id = self._db.get_run_config_id(name)
                    if config_id is not None:
                        self._configs_by_id[config_id] = cfg
                        self._config_ids_by_name[cfg.name] = config_id
            except Exception:
                logger.warning("Skipping invalid run config: %s", name)
        self._populate_tree()

    def _populate_tree(self) -> None:
        self._tree.clear()
        group_items: dict[str, QTreeWidgetItem] = {}
        folder_index = self._db.workspace_folder_index()
        group_font = QFont()
        group_font.setBold(True)

        def ensure_group(parent: QTreeWidgetItem | None, path: str, label: str) -> QTreeWidgetItem:
            key = path.casefold()
            existing = group_items.get(key)
            if existing is not None:
                return existing
            item = QTreeWidgetItem([label, "", "", "", ""])
            item.setFont(self._COL_NAME, group_font)
            item.setForeground(self._COL_NAME, QBrush(QColor(COLORS["text_secondary"])))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item.setToolTip(self._COL_NAME, path)
            item.setExpanded(True)
            if parent is None:
                self._tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            group_items[key] = item
            return item

        configs = sorted(
            self._configs,
            key=lambda cfg: (
                folder_index.canonicalize_existing(cfg.group_path).casefold(),
                cfg.name.casefold(),
            ),
        )
        query = self._search.text().strip().casefold()
        if query:
            for cfg in configs:
                flow_display_path = folder_index.display_path(cfg.flow_name)
                target_display = _saved_device_display_name(self._db, cfg.device_id)
                group_path = folder_index.canonicalize_existing(cfg.group_path)
                searchable = "\n".join(
                    (cfg.name, flow_display_path, target_display, group_path)
                ).casefold()
                if query in searchable:
                    self._add_config_tree_item(
                        cfg,
                        flow_display_path=flow_display_path,
                        target_display=target_display,
                        group_path=group_path,
                    )
            self._resize_tree_columns()
            return

        pinned = [cfg for cfg in configs if cfg.is_pinned]
        if pinned:
            pinned_root = QTreeWidgetItem(["Pinned", "", "", "", ""])
            pinned_root.setFont(self._COL_NAME, group_font)
            pinned_root.setForeground(
                self._COL_NAME,
                QBrush(QColor(COLORS["text_secondary"])),
            )
            pinned_root.setFlags(pinned_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            pinned_root.setToolTip(
                self._COL_NAME,
                "Shortcuts to pinned Run Setups",
            )
            pinned_root.setExpanded(True)
            self._tree.addTopLevelItem(pinned_root)
            for cfg in sorted(pinned, key=lambda item: item.name.casefold()):
                self._add_config_tree_item(
                    cfg,
                    parent=pinned_root,
                    flow_display_path=folder_index.display_path(cfg.flow_name),
                    target_display=_saved_device_display_name(self._db, cfg.device_id),
                    group_path=folder_index.canonicalize_existing(cfg.group_path),
                )

        for cfg in configs:
            parent = None
            flow_display_path = folder_index.display_path(cfg.flow_name)
            path_parts = [
                part
                for part in folder_index.canonicalize_existing(cfg.group_path).split("/")
                if part
            ]
            path_so_far: list[str] = []
            for part in path_parts:
                path_so_far.append(part)
                path = "/".join(path_so_far)
                parent = ensure_group(parent, path, part)

            group_path = folder_index.canonicalize_existing(cfg.group_path)
            self._add_config_tree_item(
                cfg,
                parent=parent,
                flow_display_path=flow_display_path,
                target_display=_saved_device_display_name(self._db, cfg.device_id),
                group_path=group_path,
            )

        self._tree.expandAll()
        self._resize_tree_columns()

    def _add_config_tree_item(
        self,
        cfg: SavedRunConfig,
        *,
        flow_display_path: str,
        target_display: str,
        group_path: str,
        parent: QTreeWidgetItem | None = None,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [
                cfg.name,
                flow_display_path,
                target_display,
                cfg.config.mode.value.upper(),
                "★" if cfg.is_pinned else "☆",
            ]
        )
        config_id = self._config_ids_by_name.get(cfg.name)
        item.setData(self._COL_NAME, Qt.ItemDataRole.UserRole, config_id)
        item.setToolTip(self._COL_NAME, cfg.name)
        item.setToolTip(self._COL_FLOW, flow_display_path)
        item.setToolTip(self._COL_DEVICE, target_display)
        pin_action = "Unpin" if cfg.is_pinned else "Pin"
        item.setToolTip(self._COL_PIN, f"{pin_action} Run Setup '{cfg.name}'")
        item.setTextAlignment(self._COL_PIN, Qt.AlignmentFlag.AlignCenter)
        if group_path:
            item.setToolTip(self._COL_NAME, f"{cfg.name}\nFolder: {group_path}")
        if parent is None:
            self._tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _resize_tree_columns(self) -> None:
        self._tree.resizeColumnToContents(self._COL_NAME)
        self._tree.resizeColumnToContents(self._COL_FLOW)
        self._tree.resizeColumnToContents(self._COL_DEVICE)
        self._tree.resizeColumnToContents(self._COL_MODE)
        self._tree.resizeColumnToContents(self._COL_PIN)

    def _on_tree_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if item.data(self._COL_NAME, Qt.ItemDataRole.UserRole):
            self._on_edit()

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if column != self._COL_PIN:
            return
        config_id = item.data(self._COL_NAME, Qt.ItemDataRole.UserRole)
        cfg = self._configs_by_id.get(config_id) if isinstance(config_id, int) else None
        if cfg is not None:
            self._set_pin_state(config_id, not cfg.is_pinned)

    def _selected_config(self) -> SavedRunConfig | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        config_id = item.data(self._COL_NAME, Qt.ItemDataRole.UserRole)
        if not isinstance(config_id, int):
            return None
        return self._configs_by_id.get(config_id)

    def _toggle_selected_pin(self) -> None:
        cfg = self._selected_config()
        if cfg is None:
            QMessageBox.information(self, "No Selection", "Select a run setup first.")
            return
        config_id = self._config_ids_by_name.get(cfg.name)
        if config_id is not None:
            self._set_pin_state(config_id, not cfg.is_pinned)

    def _set_pin_state(self, config_id: int, pinned: bool) -> None:
        cfg = self._configs_by_id.get(config_id)
        if cfg is None or cfg.is_pinned == pinned:
            return
        previous = cfg.is_pinned
        cfg.is_pinned = pinned
        if not self._db.update_run_config(cfg.name, cfg):
            cfg.is_pinned = previous
            QMessageBox.warning(
                self,
                "Run Setup Missing",
                "The run setup no longer exists.",
            )
            return
        self._refresh_table()
        self.run_config_changed.emit()

    # ── CRUD actions ─────────────────────────────────────────────────

    def _on_quick_run(self) -> None:
        session = (
            self._device_selector.get_selected_session()
            if self._device_selector is not None
            else None
        )
        if session is None or not isinstance(getattr(session, "device", None), Device):
            QMessageBox.information(
                self,
                "No Connected Target",
                "Connect a target first, then choose Quick Run again.",
            )
            return
        dialog = QuickRunDialog(self._db, session.device.name, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._start_quick_run(
            session,
            dialog.flow_name(),
            dialog.input_overrides(),
        )

    def _start_quick_run(
        self,
        session: DeviceSession,
        flow_name: str,
        input_overrides: Mapping[str, object] | object | None = None,
    ) -> str | None:
        flow = self._db.load_flow(flow_name)
        if flow is None:
            QMessageBox.warning(self, "Flow Missing", "The selected flow no longer exists.")
            return None
        if not flow.commands:
            QMessageBox.warning(self, "Empty Flow", "The selected flow has no commands.")
            return None
        if flow.value_model_version != CURRENT_FLOW_VALUE_MODEL_VERSION:
            QMessageBox.warning(
                self,
                "Legacy Flow Needs Repair",
                "This flow uses the retired value model and cannot be run.",
            )
            return None
        try:
            input_values = resolve_flow_inputs(flow.inputs, {}, input_overrides)
        except FlowRuntimeValueError as exc:
            QMessageBox.warning(self, "Invalid Flow Inputs", str(exc))
            return None
        report = preflight_flow(
            flow,
            db=self._db,
            root_name=flow.name,
            target=target_kind_for_device(session.device),
            mode=PreflightMode.EXECUTION,
        )
        if not confirm_execution_preflight(self, report):
            return None

        flow_values = FlowInvocationValues(flow.flow_id, flow.value_catalog, input_values)
        context = _launcher_context(session, flow_values=flow_values)
        executor = build_executor(db=self._db)
        if isinstance(session.device, BrowserDevice):
            worker: FlowRunWorker = BrowserAwareRunWorker(
                executor,
                flow,
                context,
                RunConfig(),
                profile_use_kind="quick-run",
                profile_use_label=f"Quick Run: {flow.name}",
                unavailable_context="Quick Run",
            )
        else:
            worker = ConnectedTargetRunWorker(
                executor,
                flow,
                context,
                RunConfig(),
                session_service=self._session_service,
            )
        display_name = f"Quick Run · {self._db.workspace_folder_index().display_path(flow.name)}"
        worker.configure_run_presentation(
            display_name=display_name,
            target_display_name=session.device.name,
            source="manual",
        )
        run_id = uuid.uuid4().hex[:12]
        try:
            self._runner_service.register_run(run_id, worker)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Run Could Not Start", str(exc))
            return None

        panel = RunPanel(
            config_name=display_name,
            run_id=run_id,
            worker=worker,
            on_close=self._close_run_panel,
            parent=self._runs_container,
        )
        self._observe_run_panel(panel)
        self._run_panels[run_id] = panel
        self._runs_layout.insertWidget(max(0, self._runs_layout.count() - 1), panel)
        self.retained_runs_changed.emit()
        try:
            worker.reserve_device_operation()
            worker.start()
        except DeviceOperationConflictError as exc:
            self._detach_failed_start(run_id)
            QMessageBox.information(
                self,
                "Device Already in Use",
                f"This device is already being used by {exc.current.label}. "
                "Stop that operation and try again.",
            )
            return None
        except BaseException:
            if qthread_start_may_have_succeeded(worker):
                worker.ctx.stop()
                logger.exception("Quick Run reported a startup error after beginning")
                QMessageBox.critical(
                    self,
                    "Run Startup Problem",
                    "Quick Run reported a startup problem after beginning and is "
                    "being stopped safely.",
                )
                return run_id
            self._detach_failed_start(run_id)
            logger.exception("Could not start Quick Run worker")
            QMessageBox.critical(
                self,
                "Run Could Not Start",
                "Quick Run could not be started. Review Diagnostics and try again.",
            )
            return None
        logger.info("Started Quick Run '%s' (id: %s)", flow.name, run_id)
        return run_id

    def _on_new(self) -> None:
        device_id = ""
        if self._device_selector:
            session = self._device_selector.get_selected_session()
            if session and session.device:
                device_id = saved_device_id_for_device(session.device)
        dlg = RunConfigDialog(
            self._db,
            existing=None,
            current_device_id=device_id,
            session_service=self._session_service,
            parent=self,
        )
        if dlg.exec() == RunConfigDialog.DialogCode.Accepted:
            cfg = dlg.get_saved_run_config()
            self._db.save_run_config(cfg.name, cfg)
            self._refresh_table()
            self.run_config_changed.emit()

    def _on_edit(self) -> None:
        cfg = self._selected_config()
        if cfg is None:
            QMessageBox.information(self, "No Selection", "Select a run setup first.")
            return
        dlg = RunConfigDialog(
            self._db,
            existing=cfg,
            session_service=self._session_service,
            parent=self,
        )
        if dlg.exec() == RunConfigDialog.DialogCode.Accepted:
            updated = dlg.get_saved_run_config()
            if not self._db.update_run_config(cfg.name, updated):
                QMessageBox.warning(self, "Run Setup Missing", "The run setup no longer exists.")
                return
            self._refresh_table()
            self.run_config_changed.emit()

    def _on_delete(self) -> None:
        cfg = self._selected_config()
        if cfg is None:
            return
        reply = QMessageBox.question(
            self,
            "Delete",
            f"Delete run setup '{cfg.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self._db.delete_run_config(cfg.name):
                self._refresh_table()
                self.run_config_changed.emit()
            else:
                QMessageBox.information(
                    self,
                    "Run Setup In Use",
                    "This run setup is assigned to a schedule. Reassign or "
                    "delete that schedule first.",
                )

    # ── Run ──────────────────────────────────────────────────────────

    def _on_run(self) -> None:
        cfg = self._selected_config()
        if cfg is None:
            QMessageBox.information(self, "No Selection", "Select a run setup first.")
            return
        self._start_config(cfg)

    def _run_config_by_id(self, run_config_id: int) -> None:
        """Start a schedule-requested setup without interactive value prompts."""
        cfg = self._db.load_run_config_by_id(run_config_id)
        if cfg is None:
            QMessageBox.warning(
                self,
                "Run Setup Missing",
                "The saved run setup no longer exists.",
            )
            return
        self._start_config(cfg, prompt_for_parameters=False)

    def start_run_config_by_id(
        self,
        run_config_id: int,
        *,
        prompt_for_values: bool | None = None,
        reuse_panel: RunPanel | None = None,
        interaction_parent: QWidget | None = None,
    ) -> bool:
        """Start a saved setup by stable identity for tray and panel actions."""
        dialog_parent = interaction_parent or self
        cfg = self._db.load_run_config_by_id(run_config_id)
        if cfg is None:
            QMessageBox.warning(
                dialog_parent,
                "Run Setup Missing",
                "The saved run setup no longer exists.",
            )
            return False
        return self._start_config(
            cfg,
            prompt_for_parameters=prompt_for_values,
            reuse_panel=reuse_panel,
            interaction_parent=interaction_parent,
        )

    def _start_config(
        self,
        cfg: SavedRunConfig,
        *,
        prompt_for_parameters: bool | None = None,
        reuse_panel: RunPanel | None = None,
        interaction_parent: QWidget | None = None,
    ) -> bool:
        dialog_parent = interaction_parent or self
        should_prompt = (
            cfg.ask_for_values
            if prompt_for_parameters is None
            else prompt_for_parameters
        )
        coordinator = self._coordinator()
        missing_inputs_error: RunPreparationError | None = None
        try:
            prepared = coordinator.prepare(cfg)
        except RunPreparationError as exc:
            can_prompt_for_missing = (
                should_prompt
                and exc.title == "Invalid Flow Inputs"
                and exc.public_message.startswith("Required flow Inputs are missing:")
            )
            flow = self._db.load_flow(cfg.flow_name) if can_prompt_for_missing else None
            if (
                flow is None
                or flow.value_model_version != CURRENT_FLOW_VALUE_MODEL_VERSION
                or not flow.inputs
            ):
                message = QMessageBox.warning if exc.warning else QMessageBox.critical
                message(dialog_parent, exc.title, exc.public_message)
                return False
            prepared = None
            missing_inputs_error = exc

        flow_for_prompt = prepared.flow if prepared is not None else flow
        if (
            should_prompt
            and flow_for_prompt.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
            and flow_for_prompt.inputs
        ):
            input_dialog = ManualInputOverridesDialog(
                cfg.name,
                flow_for_prompt.inputs,
                cfg.input_overrides,
                parent=dialog_parent,
            )
            if input_dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            try:
                prepared = coordinator.prepare(
                    cfg,
                    transient_input_overrides=input_dialog.overrides(),
                )
            except RunPreparationError as exc:
                message = QMessageBox.warning if exc.warning else QMessageBox.critical
                message(dialog_parent, exc.title, exc.public_message)
                return False

        if prepared is None:
            assert missing_inputs_error is not None
            QMessageBox.critical(
                dialog_parent,
                missing_inputs_error.title,
                missing_inputs_error.public_message,
            )
            return False

        if should_prompt and prepared.flow.parameters:
            parameter_dialog = ManualParameterOverridesDialog(
                cfg.name,
                prepared.flow.parameters,
                prepared.parameter_values,
                parent=dialog_parent,
            )
            if parameter_dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            try:
                prepared = coordinator.prepare(
                    cfg,
                    transient_parameter_overrides=parameter_dialog.overrides(),
                )
            except RunPreparationError as exc:
                message = QMessageBox.warning if exc.warning else QMessageBox.critical
                message(dialog_parent, exc.title, exc.public_message)
                return False

        if not confirm_execution_preflight(dialog_parent, prepared.preflight):
            return False

        previous_run_id: str | None = None
        if reuse_panel is not None:
            previous_run_id = reuse_panel.run_id
            if not self._remove_launcher_run(previous_run_id):
                reuse_panel.show_cleanup_pending()
                QMessageBox.information(
                    dialog_parent,
                    "Run Cleanup Pending",
                    "The previous attempt is still releasing its target or "
                    "profile. Retry Run again after cleanup completes.",
                )
                return False
            self._run_panels.pop(previous_run_id, None)

        try:
            registered = coordinator.register(prepared)
        except RunCapacityError as exc:
            QMessageBox.critical(
                dialog_parent,
                "Error",
                str(exc),
            )
            if previous_run_id is not None and reuse_panel is not None:
                self._run_panels[previous_run_id] = reuse_panel
            return False
        run_id = registered.run_id
        worker = registered.worker

        # Use LauncherRunWorker which creates the session on the worker
        # thread — this is critical for Playwright (browser) devices whose
        # greenlet must live on the thread that later calls browser methods.
        logger.debug("Creating LauncherRunWorker for device: %s", prepared.device)
        panel = reuse_panel
        try:
            # Establish the panel as durable ownership before acquiring any
            # resource whose release can fail and need a user-visible retry.
            if panel is None:
                panel = RunPanel(
                    config_name=cfg.name,
                    run_id=run_id,
                    worker=worker,
                    on_close=self._close_run_panel,
                    parent=self._runs_container,
                    run_config_id=self._db.get_run_config_id(cfg.name),
                    on_restart=self._restart_run_panel,
                )
                self._observe_run_panel(panel)
            else:
                panel.rebind(run_id, worker)
            self._run_panels[run_id] = panel
            if reuse_panel is None:
                idx = max(0, self._runs_layout.count() - 1)
                self._runs_layout.insertWidget(idx, panel)
            self.retained_runs_changed.emit()

            coordinator.start(registered)
            panel.commit_rebind()
        except BrowserProfileInUseError as exc:
            if not self._recover_failed_start(run_id, panel, previous_run_id):
                if panel is not None:
                    panel.show_cleanup_pending()
                QMessageBox.critical(
                    dialog_parent,
                    "Run Cleanup Pending",
                    "The browser profile is busy and cleanup for this attempt "
                    "is still pending. The panel was kept open for a safe retry.",
                )
                return False
            QMessageBox.critical(
                dialog_parent,
                "Profile Already in Use",
                f"{exc}\n\nStop the active browser or run, then try again.",
            )
            return False
        except DeviceOperationConflictError as exc:
            if not self._recover_failed_start(run_id, panel, previous_run_id):
                if panel is not None:
                    panel.show_cleanup_pending()
                QMessageBox.critical(
                    dialog_parent,
                    "Run Cleanup Pending",
                    "The device is busy and the run's profile cleanup is still "
                    "pending. Automadin kept the run panel open for a safe retry.",
                )
                return False
            QMessageBox.information(
                dialog_parent,
                "Device Already in Use",
                f"This device is already being used by {exc.current.label}. "
                "Stop that operation and try again.",
            )
            return False
        except BaseException:
            if worker is not None and qthread_start_may_have_succeeded(worker):
                # Do not detach a QThread (or release its device/profile
                # ownership) after an ambiguous post-native-start exception.
                # The panel already owns the finished-signal lifecycle.
                if panel is not None:
                    panel._on_stop()
                else:
                    worker.ctx.stop()
                logger.exception("Launcher worker reported a startup error after beginning")
                QMessageBox.critical(
                    dialog_parent,
                    "Run Startup Problem",
                    "The run reported a startup problem after beginning. "
                    "Automadin is stopping it safely; review Diagnostics before "
                    "trying again.",
                )
                return False
            if not self._recover_failed_start(run_id, panel, previous_run_id):
                # Keep the panel and service registration as the durable cleanup
                # owner. Closing the panel retries remove_run() truthfully.
                if panel is not None:
                    panel.show_cleanup_pending()
                logger.exception("Could not start launcher worker")
                QMessageBox.critical(
                    dialog_parent,
                    "Run Cleanup Pending",
                    "The run did not start, but its device cleanup is still "
                    "pending. Automadin kept the run panel open so cleanup can "
                    "be retried safely.",
                )
                return False
            logger.exception("Could not start launcher worker")
            QMessageBox.critical(
                dialog_parent,
                "Run Could Not Start",
                "The run could not be started. Review Diagnostics for details, then try again.",
            )
            return False

        logger.info("Started run '%s' (id: %s) for config '%s'", cfg.flow_name, run_id, cfg.name)
        return True

    def _restart_run_panel(
        self,
        panel: RunPanel,
        *,
        interaction_parent: QWidget | None = None,
    ) -> bool:
        run_config_id = panel.run_config_id
        if run_config_id is None:
            return False
        return self.start_run_config_by_id(
            run_config_id,
            reuse_panel=panel,
            interaction_parent=interaction_parent,
        )

    def _recover_failed_start(
        self,
        run_id: str,
        panel: RunPanel | None,
        previous_run_id: str | None,
    ) -> bool:
        """Clean a failed replacement and restore its prior finished panel."""
        if not self._remove_launcher_run(run_id):
            return False
        self._run_panels.pop(run_id, None)
        if panel is None:
            self.retained_runs_changed.emit()
            return True
        if previous_run_id is None:
            panel.setParent(None)
            panel.deleteLater()
            self.retained_runs_changed.emit()
            return True
        restored_run_id = panel.rollback_rebind()
        if restored_run_id is not None:
            self._run_panels[restored_run_id] = panel
        self.retained_runs_changed.emit()
        return True

    def _detach_failed_start(self, run_id: str) -> bool:
        """Remove a failed-start panel only after service cleanup succeeds."""
        if not self._remove_launcher_run(run_id):
            return False
        panel = self._run_panels.pop(run_id, None)
        if panel is not None:
            panel.setParent(None)
            panel.deleteLater()
        self.retained_runs_changed.emit()
        return True

    def _remove_launcher_run(self, run_id: str) -> bool:
        """Release profile ownership before dropping the service's device owner."""
        return self._coordinator().remove(run_id)

    def _close_run_panel(self, run_id: str) -> bool:
        panel = self._run_panels.get(run_id)
        if panel is None:
            return True
        if not self._remove_launcher_run(run_id):
            QMessageBox.information(
                self,
                "Run Cleanup Pending",
                "This run is still stopping or releasing its reserved profile. "
                "Its panel was kept open so cleanup can be retried safely.",
            )
            return False
        self._run_panels.pop(run_id, None)
        panel.setParent(None)
        panel.deleteLater()
        self.retained_runs_changed.emit()
        return True

    def shutdown(self, *, wait_ms: int = 0) -> bool:
        """Stop every thread while retaining unresolved in-process profile locks."""
        result = self._coordinator().shutdown(wait_ms=wait_ms)
        if not result.stopped:
            return False
        for run_id in result.profile_cleanup_pending_ids:
            panel = self._run_panels.get(run_id)
            if panel is not None:
                panel.show_cleanup_pending()
        # A quarantined or transiently unreleased profile remains owned by the
        # retained worker until process exit.  It must block panel detachment,
        # but cannot block application exit once every QThread/device operation
        # has stopped: the process-local registry then ends safely.
        return True

    def _coordinator(self) -> RunCoordinator:
        return RunCoordinator(
            self._db,
            self._session_service,
            self._runner_service,
            worker_factory=_launcher_worker_for_saved_config,
        )

    def _observe_run_panel(self, panel: RunPanel) -> None:
        """Forward one retained panel's presentation changes to mirror UIs."""
        if getattr(panel, "_launcher_observed", False):
            return
        panel._launcher_observed = True  # type: ignore[attr-defined]
        panel.presentation_changed.connect(self.retained_runs_changed.emit)

    def scheduler_coordinator(self) -> RunCoordinator:
        """Return the shared run host used by scheduled, panel-free launches."""
        return self._coordinator()

    @property
    def runner_service(self) -> RunnerService:
        return self._runner_service

    def pinned_run_setups(self) -> tuple[tuple[int, str], ...]:
        """Return current pinned setup identities for transient shortcut menus."""
        pinned: list[tuple[int, str]] = []
        for name in self._db.list_run_configs():
            config_id = self._db.get_run_config_id(name)
            config = self._db.load_run_config(name)
            if config_id is not None and config is not None and config.is_pinned:
                pinned.append((config_id, config.name))
        return tuple(sorted(pinned, key=lambda item: item[1].casefold()))

    def run_setups(self) -> tuple[tuple[int, str, str], ...]:
        """Return all setup identities and canonical folders for compact launchers."""
        folder_index = self._db.workspace_folder_index()
        setups: list[tuple[int, str, str]] = []
        for config in self._configs:
            config_id = self._config_ids_by_name.get(config.name)
            if config_id is None:
                continue
            setups.append(
                (
                    config_id,
                    config.name,
                    folder_index.canonicalize_existing(config.group_path),
                )
            )
        return tuple(
            sorted(
                setups,
                key=lambda item: (item[2].casefold(), item[1].casefold()),
            )
        )

    def retained_run_snapshots(self) -> tuple[RunPanelSnapshot, ...]:
        """Return retained Active Runs cards in their visible order."""
        snapshots: list[RunPanelSnapshot] = []
        for index in range(max(0, self._runs_layout.count() - 1)):
            widget = self._runs_layout.itemAt(index).widget()
            if isinstance(widget, RunPanel):
                snapshots.append(widget.snapshot())
        return tuple(snapshots)

    def pause_or_resume_retained_run(self, run_id: str) -> None:
        panel = self._run_panels.get(run_id)
        if panel is not None:
            panel.request_pause_or_resume()

    def stop_retained_run(self, run_id: str) -> None:
        panel = self._run_panels.get(run_id)
        if panel is not None:
            panel.request_stop()

    def restart_retained_run(
        self,
        run_id: str,
        *,
        interaction_parent: QWidget | None = None,
    ) -> None:
        panel = self._run_panels.get(run_id)
        if panel is not None:
            panel.request_restart(interaction_parent=interaction_parent)

    def close_retained_run(self, run_id: str) -> None:
        panel = self._run_panels.get(run_id)
        if panel is not None:
            panel.request_close()

    def focus_run(self, run_id: str, source: str) -> None:
        """Open the best available detail surface for a run entry."""
        if source == "manual" and run_id in self._run_panels:
            self._workspace_tabs.setCurrentIndex(0)
            self._runs_scroll.ensureWidgetVisible(self._run_panels[run_id])
            return
        self._workspace_tabs.setCurrentWidget(self._history_view)
        self._history_view.refresh()

    def refresh_run_list(self) -> None:
        """Public slot: re-fetch run configs from DB and repopulate the table."""
        self._refresh_table()
        self._schedule_view.refresh()

    def refresh_scheduling_views(self) -> None:
        self._schedule_view.refresh()
        self._history_view.refresh()

    def _schedule_changed(self) -> None:
        self._history_view.refresh()
        self.schedule_changed.emit()
