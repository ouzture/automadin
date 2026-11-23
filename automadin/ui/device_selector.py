"""Device selector bar — choose device type, discover devices, connect/disconnect."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from automadin.engine.adb_engine import (
    AdbDiscoveryDiagnosticKind,
    AdbDiscoveryResult,
)
from automadin.engine.session import DeviceSessionService
from automadin.models.device import (
    AndroidDevice,
    Device,
    DeviceType,
    DeviceSession,
    WindowsDevice,
)
from automadin.services.database import DatabaseService
from automadin.services.browser_profile_metadata import profile_aware_device_name
from automadin.services.browser_profile_leases import BrowserProfileInUseError
from automadin.services.browser_health import BrowserHealthState
from automadin.services.browser_health_cache import (
    BrowserHealthCache,
    default_browser_health_cache,
)
from automadin.services.device_choices import (
    DeviceChoice,
    assess_device_choices,
    attach_browser_health,
    attach_browser_health_error,
    browser_health_statuses,
    browser_health_target_for_device,
)
from automadin.services.device_operation_leases import (
    DeviceOperationConflictError,
    DeviceOperationLease,
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    default_device_operation_registry,
    device_operation_key,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)

_CHOICE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_DISCOVERY_FAILURE_MESSAGE = (
    "Automadin could not discover devices. Check the selected runtime or "
    "connection, then try again."
)
_DISCOVERY_START_FAILURE_MESSAGE = (
    "Device discovery could not start. Review Diagnostics, then try again."
)
_BROWSER_HEALTH_FAILURE_MESSAGE = (
    "The browser launch check could not be completed. Selection remains "
    "available; the actual browser launch is the final check."
)
_CONNECTION_FAILURE_MESSAGE = (
    "Automadin could not connect to this device. Check the device and runtime, "
    "then try again."
)
_CONNECTION_START_FAILURE_MESSAGE = (
    "Device connection could not start. Review Diagnostics, then try again."
)
_DISCONNECT_FAILURE_MESSAGE = (
    "Automadin could not close this device session safely. It remains connected "
    "so you can try again."
)
_DISCONNECT_START_FAILURE_MESSAGE = (
    "Disconnect could not start. The device remains connected; review "
    "Diagnostics, then try again."
)
_DISCONNECT_CONFLICT_MESSAGE = (
    "This device is busy with another Automadin operation. Stop that operation, "
    "then disconnect again."
)

_TYPE_DISPLAY: dict[DeviceType, str] = {
    DeviceType.BROWSER: "Browser",
    DeviceType.ANDROID: "Android (ADB)",
    DeviceType.WINDOWS: "Windows App",
}


@dataclass(frozen=True)
class _DiscoveryOutcome:
    device_type: DeviceType
    devices: list[Device]
    adb_result: AdbDiscoveryResult | None = None
    choices: list[DeviceChoice] | None = None
    profile_labels: dict[str, str] | None = None


@dataclass(frozen=True)
class _ConnectionFailure:
    title: str
    message: str
    hint: str = ""


class _ConnectThread(QThread):
    """Connects to a device on a background thread."""

    succeeded = Signal(object)  # DeviceSession
    failed = Signal(object)  # sanitized _ConnectionFailure

    def __init__(
        self,
        session_service: DeviceSessionService,
        device: Device,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_service = session_service
        self._device = device

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        try:
            session = self._session_service.create_session(self._device)
            if self.isInterruptionRequested():
                try:
                    self._session_service.close_session(session.session_id)
                except Exception:
                    logger.exception(
                        "Could not close session created during selector shutdown"
                    )
                return
            self.succeeded.emit(session)
        except BrowserProfileInUseError as exc:
            if self.isInterruptionRequested():
                return
            logger.exception("Browser profile connection was refused")
            active = exc.current.borrowed_by or exc.current.use
            self.failed.emit(
                _ConnectionFailure(
                    "Browser Profile Already In Use",
                    "This browser profile is already active.",
                    f"Active operation: {active.label}\n"
                    "Stop that browser activity, then connect again.",
                )
            )
        except Exception:
            if self.isInterruptionRequested():
                return
            logger.exception("Device connection failed")
            self.failed.emit(
                _ConnectionFailure("Connection Failed", _CONNECTION_FAILURE_MESSAGE)
            )


class _DisconnectThread(QThread):
    """Closes one retained session without blocking the GUI thread."""

    succeeded = Signal(object)  # exact DeviceSession that was closed
    failed = Signal(str)  # fixed safe user-readable message

    def __init__(
        self,
        session_service: DeviceSessionService,
        session: DeviceSession,
        operation_lease: DeviceOperationLease | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_service = session_service
        self._session = session
        self._operation_lease = operation_lease

    def run(self) -> None:
        try:
            self._session_service.close_session(self._session.session_id)
        except Exception:
            logger.exception("Device session disconnect failed")
            self.failed.emit(_DISCONNECT_FAILURE_MESSAGE)
        else:
            self.succeeded.emit(self._session)
        finally:
            self.release_operation_lease()

    def release_operation_lease(self) -> bool:
        lease = self._operation_lease
        if lease is None:
            return True
        try:
            lease.release()
        except BaseException:
            logger.exception("Could not release the disconnect device-operation lease")
            return False
        if lease is self._operation_lease:
            self._operation_lease = None
        return True


class _DiscoverThread(QThread):
    """Discovers devices on a background thread."""

    succeeded = Signal(object)  # _DiscoveryOutcome
    failed = Signal(object, str)  # DeviceType, error message

    def __init__(
        self,
        session_service: DeviceSessionService,
        device_type: DeviceType,
        database: DatabaseService,
        browser_health_cache: BrowserHealthCache,
        data_dir: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_service = session_service
        self._device_type = device_type
        self._database = database
        self._browser_health_cache = browser_health_cache
        self._data_dir = data_dir

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        try:
            profiles = (
                self._database.profile_engine_specs()
                if self._device_type == DeviceType.BROWSER
                else None
            )
            profile_labels = (
                self._database.profile_display_labels()
                if self._device_type == DeviceType.BROWSER
                else None
            )
            if self.isInterruptionRequested():
                return
            devices = self._session_service.discovery.list_devices(
                self._device_type, profiles
            )
            if self.isInterruptionRequested():
                return
            device_list = list(devices)
            choices = (
                assess_device_choices(device_list, self._data_dir or Path.cwd())
                if self._device_type == DeviceType.BROWSER
                else [DeviceChoice(device=device, runtime_status=None) for device in device_list]
            )
            if self._device_type == DeviceType.BROWSER:
                statuses = browser_health_statuses(choices)
                try:
                    health = self._browser_health_cache.check_many(
                        statuses,
                        self._data_dir or Path.cwd(),
                        cancel=self.isInterruptionRequested,
                    )
                except Exception:
                    logger.exception(
                        "Browser launch-health assessment failed; preserving "
                        "detected choices"
                    )
                    choices = attach_browser_health_error(
                        choices,
                        RuntimeError(_BROWSER_HEALTH_FAILURE_MESSAGE),
                    )
                else:
                    choices = attach_browser_health(
                        choices,
                        {
                            target: result
                            for target, result in health.items()
                            if target in statuses
                        },
                    )
            if self.isInterruptionRequested():
                return
            self.succeeded.emit(
                _DiscoveryOutcome(
                    device_type=self._device_type,
                    devices=device_list,
                    adb_result=getattr(devices, "discovery_result", None),
                    choices=choices,
                    profile_labels=profile_labels,
                )
            )
        except Exception:
            if self.isInterruptionRequested():
                return
            logger.exception("Device discovery failed")
            self.failed.emit(self._device_type, _DISCOVERY_FAILURE_MESSAGE)
        finally:
            release = getattr(
                self._database,
                "release_current_thread_connection",
                None,
            )
            if callable(release):
                try:
                    release()
                except Exception:
                    logger.exception(
                        "Could not release the discovery worker database connection"
                    )


class DeviceSelectorWidget(QWidget):
    """Horizontal bar for selecting a device type, discovering devices,
    and connecting/disconnecting sessions.

    Flow: Choose Type → Refresh/Discover → Select Device → Connect
    """

    session_connected = Signal(str)   # session_id
    session_disconnected = Signal()

    def __init__(
        self,
        session_service: DeviceSessionService,
        db: DatabaseService,
        parent: QWidget | None = None,
        *,
        browser_health_cache: BrowserHealthCache | None = None,
        device_operation_registry: DeviceOperationLeaseRegistry | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_service = session_service
        self._db = db
        self._browser_health_cache = (
            browser_health_cache or default_browser_health_cache()
        )
        self._device_operation_registry = (
            device_operation_registry
            if device_operation_registry is not None
            else default_device_operation_registry()
        )
        self._current_session: DeviceSession | None = None
        self._discovered_devices: list[Device] = []
        self._discovered_choices: list[DeviceChoice] = []
        self._pending_discovery_type: DeviceType | None = None
        self._shutting_down = False
        self._shutdown_wait_pending = False
        self._disconnect_failed_during_shutdown = False
        self._disconnect_cleanup_pending = False

        # Thread refs (prevent GC)
        self._connect_thread: _ConnectThread | None = None
        self._disconnect_thread: _DisconnectThread | None = None
        self._pending_disconnect_lease: DeviceOperationLease | None = None
        self._discover_thread: _DiscoverThread | None = None

        self._build_ui()
        self._wire_signals()

    # ── UI ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setObjectName("deviceSelectorBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # 1. Automation capability
        layout.addWidget(QLabel("Controls:"))
        self._type_combo = QComboBox()
        for dt in DeviceType:
            self._type_combo.addItem(_TYPE_DISPLAY[dt], dt)
        self._type_combo.setMinimumWidth(130)
        self._type_combo.setAccessibleName("Automation capability")
        self._type_combo.setToolTip(
            "Choose whether this session controls a browser, Android target, "
            "or Windows application."
        )
        layout.addWidget(self._type_combo)

        # 2. Refresh / discover button
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setToolTip("Discover available devices")
        layout.addWidget(self._refresh_btn)

        # 3. Concrete target within the selected capability
        layout.addWidget(QLabel("Target:"))
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(440)
        self._device_combo.view().setMinimumWidth(760)
        self._device_combo.setPlaceholderText("Click Refresh to discover devices")
        self._device_combo.setAccessibleName("Automation target")
        layout.addWidget(self._device_combo)

        # 4. Connect button
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setProperty("cssClass", "success")
        self._connect_btn.setEnabled(False)
        layout.addWidget(self._connect_btn)

        # 5. Disconnect button (hidden by default)
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setProperty("cssClass", "danger")
        self._disconnect_btn.setVisible(False)
        layout.addWidget(self._disconnect_btn)

        # 6. Status label
        self._status_label = QLabel("")
        self._status_label.setVisible(False)
        layout.addWidget(self._status_label)

        layout.addStretch()

    def _wire_signals(self) -> None:
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        self._refresh_btn.clicked.connect(self._discover_devices)
        self._device_combo.currentIndexChanged.connect(
            self._on_device_choice_changed
        )
        self._connect_btn.clicked.connect(self._connect)
        self._disconnect_btn.clicked.connect(self._disconnect)

    # ── Device type changed ────────────────────────────────────────

    def _on_type_changed(self, _index: int) -> None:
        """Clear device list when type changes."""
        self._device_combo.clear()
        self._device_combo.setToolTip("")
        self._discovered_devices.clear()
        self._discovered_choices.clear()
        self._device_combo.setPlaceholderText("Click Refresh to discover devices")
        self._clear_status()

    # ── Discovery ──────────────────────────────────────────────────

    def _discover_devices(self) -> None:
        if self._shutting_down:
            return
        if self._discover_thread is not None and self._discover_thread.isRunning():
            return
        device_type: DeviceType = self._type_combo.currentData()

        self._discovered_devices.clear()
        self._discovered_choices.clear()
        self._device_combo.clear()
        self._device_combo.setToolTip("")
        self._device_combo.setPlaceholderText("Scanning for devices...")
        self._type_combo.setEnabled(False)
        self._device_combo.setEnabled(False)
        self._connect_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Scanning...")
        self._set_status("Discovering devices...", COLORS["warning"])

        worker: _DiscoverThread | None = None
        try:
            worker = _DiscoverThread(
                self._session_service,
                device_type,
                self._db,
                self._browser_health_cache,
                self._data_dir(),
                self,
            )
            worker.succeeded.connect(self._on_discovered)
            worker.failed.connect(self._on_discover_error)
            worker.finished.connect(self._on_discovery_thread_finished)
            self._discover_thread = worker
            worker.start()
        except Exception:
            logger.exception("Could not start the device discovery worker")
            startup_uncertain = qthread_start_may_have_succeeded(worker)
            if startup_uncertain:
                worker.requestInterruption()
                self._device_combo.setPlaceholderText(
                    "Stopping incomplete discovery startup..."
                )
                self._set_status(
                    "Discovery startup failed; finishing its background work.",
                    COLORS["error"],
                )
            else:
                if self._discover_thread is worker:
                    self._discover_thread = None
                if worker is not None:
                    worker.deleteLater()
                self._device_combo.setPlaceholderText(
                    "Discovery could not start — click Refresh to retry"
                )
                self._finish_discovery()
                self._set_status(_DISCOVERY_START_FAILURE_MESSAGE, COLORS["error"])
            QMessageBox.warning(
                self,
                (
                    "Discovery Startup Uncertain"
                    if startup_uncertain
                    else "Discovery Could Not Start"
                ),
                (
                    "Device discovery may have begun, but startup could not be "
                    "confirmed. Automadin is retaining it until background cleanup "
                    "finishes."
                    if startup_uncertain
                    else _DISCOVERY_START_FAILURE_MESSAGE
                ),
            )

    def _on_discovered(self, outcome: _DiscoveryOutcome) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._discover_thread:
            return
        if self._shutting_down:
            return
        if self._current_session is not None:
            logger.debug(
                "Ignoring discovery result because a session connected while scanning"
            )
            self._finish_discovery()
            return
        if outcome.device_type != self._type_combo.currentData():
            logger.debug(
                "Ignoring stale %s discovery result after target type changed",
                outcome.device_type.value,
            )
            self._finish_discovery()
            return

        devices = outcome.devices
        choices = outcome.choices or [
            DeviceChoice(device=device, runtime_status=None) for device in devices
        ]
        self._discovered_devices = devices
        self._discovered_choices = choices
        self._device_combo.clear()

        if not devices:
            if outcome.device_type == DeviceType.ANDROID and outcome.adb_result:
                self._show_adb_discovery_status(outcome.adb_result)
            else:
                self._device_combo.setPlaceholderText("No devices found")
                self._set_status("No devices found", COLORS["error"])
        else:
            profile_labels = outcome.profile_labels or {}
            first_selectable = -1
            blocked_count = 0
            for choice in choices:
                dev = choice.device
                display_name = profile_aware_device_name(dev, profile_labels)
                suffix = self._choice_status_label(choice)
                if suffix:
                    display_name = f"{display_name} — {suffix}"
                if not choice.selectable:
                    blocked_count += 1
                self._device_combo.addItem(display_name, dev)
                index = self._device_combo.count() - 1
                self._device_combo.setItemData(index, choice, _CHOICE_ROLE)
                tooltip = self._choice_tooltip(choice, display_name)
                self._device_combo.setItemData(
                    index,
                    tooltip,
                    Qt.ItemDataRole.ToolTipRole,
                )
                self._device_combo.setItemData(
                    index,
                    tooltip,
                    Qt.ItemDataRole.AccessibleDescriptionRole,
                )
                if choice.selectable and first_selectable < 0:
                    first_selectable = index
            self._device_combo.setCurrentIndex(first_selectable)
            if outcome.device_type == DeviceType.ANDROID and outcome.adb_result:
                self._show_adb_discovery_status(outcome.adb_result)
            elif blocked_count:
                available_count = len(choices) - blocked_count
                if available_count:
                    self._set_status(
                        f"{blocked_count} browser choice"
                        f"{'s need' if blocked_count != 1 else ' needs'} attention. "
                        "Select a row to read its reason; Connect stays unavailable "
                        "for blocked rows.",
                        COLORS["warning"],
                    )
                else:
                    self._device_combo.setPlaceholderText(
                        "No browser runtime is currently available"
                    )
                    self._set_status(
                        "No browser runtime is available. Open Tools > Runtime Components.",
                        COLORS["error"],
                    )
            else:
                self._clear_status()

        self._finish_discovery()

    def _on_discover_error(
        self, requested_type: DeviceType, _error_msg: str
    ) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._discover_thread:
            return
        if self._shutting_down:
            return
        if requested_type != self._type_combo.currentData():
            self._finish_discovery()
            return
        self._device_combo.clear()
        self._device_combo.setPlaceholderText("Discovery failed")
        self._set_status("Error", COLORS["error"])

        QMessageBox.warning(
            self,
            "Discovery Failed",
            _DISCOVERY_FAILURE_MESSAGE,
        )

        self._finish_discovery()

    def _show_adb_discovery_status(self, result: AdbDiscoveryResult) -> None:
        diagnostics = list(result.diagnostics)
        issues = [
            diagnostic
            for diagnostic in diagnostics
            if diagnostic.kind != AdbDiscoveryDiagnosticKind.SUCCESS
        ]
        success = next(
            (
                diagnostic
                for diagnostic in diagnostics
                if diagnostic.kind == AdbDiscoveryDiagnosticKind.SUCCESS
            ),
            None,
        )
        detail = "\n\n".join(
            "\n".join(
                part
                for part in (
                    diagnostic.message,
                    diagnostic.action,
                    diagnostic.detail,
                )
                if part
            )
            for diagnostic in diagnostics
        )

        if issues:
            primary = issues[0]
            fatal = primary.kind in {
                AdbDiscoveryDiagnosticKind.MISSING_EXECUTABLE,
                AdbDiscoveryDiagnosticKind.TIMEOUT,
                AdbDiscoveryDiagnosticKind.COMMAND_FAILED,
            }
            placeholder_by_kind = {
                AdbDiscoveryDiagnosticKind.MISSING_EXECUTABLE: (
                    "ADB missing — install or select adb.exe"
                ),
                AdbDiscoveryDiagnosticKind.TIMEOUT: "ADB timed out — retry discovery",
                AdbDiscoveryDiagnosticKind.COMMAND_FAILED: (
                    "ADB failed — test its configured path"
                ),
                AdbDiscoveryDiagnosticKind.UNAUTHORIZED: (
                    "Authorize USB debugging on the device"
                ),
                AdbDiscoveryDiagnosticKind.OFFLINE: (
                    "Device offline — reconnect and refresh"
                ),
            }
            if not result.devices:
                self._device_combo.setPlaceholderText(
                    placeholder_by_kind.get(primary.kind, "Android discovery needs attention")
                )
            self._set_status(
                primary.message,
                COLORS["error"] if fatal else COLORS["warning"],
            )
            self._status_label.setToolTip(detail)
            self._device_combo.setToolTip(detail)
            return

        message = success.message if success else "ADB discovery completed."
        if not result.devices:
            self._device_combo.setPlaceholderText(
                "ADB ready — connect a device and enable USB debugging"
            )
        self._set_status(message, COLORS["success"])
        self._status_label.setToolTip(detail)
        self._device_combo.setToolTip(detail)

    def _finish_discovery(self) -> None:
        self._refresh_btn.setText("Refresh")
        if self._current_session is None:
            self._type_combo.setEnabled(True)
            self._device_combo.setEnabled(True)
            self._refresh_btn.setEnabled(True)
            self._update_connect_availability()

    def _update_connect_availability(self, _index: int | None = None) -> None:
        """Enable Connect only for a concrete, selectable discovery result."""
        if self._current_session is not None or self._shutting_down:
            return
        choice = self._device_combo.currentData(_CHOICE_ROLE)
        device = self._device_combo.currentData()
        selectable = device is not None and (
            not isinstance(choice, DeviceChoice) or choice.selectable
        )
        self._connect_btn.setEnabled(selectable)

    def _on_device_choice_changed(self, _index: int | None = None) -> None:
        """Expose a focused choice's state without relying on a tooltip."""
        self._update_connect_availability()
        if self._shutting_down or self._current_session is not None:
            return
        choice = self._device_combo.currentData(_CHOICE_ROLE)
        if not isinstance(choice, DeviceChoice):
            return
        summary = self._choice_persistent_summary(choice)
        if not summary:
            self._clear_status()
            return
        color = COLORS["error"] if not choice.selectable else COLORS["warning"]
        self._set_status(summary, color)
        self._status_label.setToolTip(
            self._choice_tooltip(choice, self._device_combo.currentText())
        )

    @staticmethod
    def _choice_tooltip(choice: DeviceChoice, display_name: str) -> str:
        profile_id = (
            getattr(getattr(choice.device, "config", None), "profile_id", None)
            or "none"
        )
        lines = [display_name, f"Profile ID: {profile_id}"]
        if choice.assessment_error:
            lines.extend(
                (
                    "Could not check runtime availability.",
                    choice.assessment_error,
                    "You can still try to connect; launch remains the final check.",
                )
            )
        elif choice.launch_assessment_error:
            lines.extend(
                (
                    "Could not check isolated headless engine launch health.",
                    choice.launch_assessment_error,
                    "The engine remains selectable because this did not test the "
                    "real headed/profile launch.",
                )
            )
        elif choice.runtime_status is not None and not choice.runtime_status.usable:
            lines.extend(
                (
                    choice.runtime_status.error
                    or "The required browser runtime was not detected.",
                    "Open Tools > Runtime Components, repair or install it, then refresh.",
                )
            )
        elif choice.launch_health is not None:
            health = choice.launch_health
            checked = health.checked_at.astimezone().strftime("%H:%M:%S")
            if health.state is BrowserHealthState.HEALTHY:
                lines.append(
                    f"Launch tested at {checked}: an isolated temporary headless "
                    "browser opened and closed successfully. This does not verify "
                    "profile sign-in, headed mode, websites, or future launches."
                )
            elif not choice.selectable:
                lines.extend(
                    (
                        f"Engine launch check could not clean up safely ({health.stage}).",
                        health.message,
                        "Restart Automadin before retrying. If it repeats, repair the "
                        "browser from Tools > Runtime Components.",
                    )
                )
            else:
                lines.extend(
                    (
                        f"Isolated headless engine check needs attention ({health.stage}).",
                        health.message,
                        "Selection remains available because this did not test the "
                        "real headed/profile launch; the actual launch is final.",
                    )
                )
        elif choice.runtime_status is not None:
            lines.append(
                "Detected — not launch tested. This confirms required files were "
                "found, but does not verify profile sign-in or future launches."
            )
        return "\n".join(lines)

    @staticmethod
    def _choice_status_label(choice: DeviceChoice) -> str:
        if browser_health_target_for_device(choice.device) is None:
            return ""
        if choice.runtime_status is not None and not choice.runtime_status.usable:
            return "Unavailable"
        health = choice.launch_health
        if health is None:
            return (
                "Could not check"
                if choice.assessment_error or choice.launch_assessment_error
                else "Detected"
            )
        if health.state is BrowserHealthState.HEALTHY:
            profile_id = getattr(
                getattr(choice.device, "config", None),
                "profile_id",
                None,
            )
            return (
                "Engine tested · profile not checked"
                if profile_id
                else "Engine launch tested"
            )
        if health.state in {
            BrowserHealthState.FAILED,
            BrowserHealthState.UNAVAILABLE,
            BrowserHealthState.CLEANUP_FAILED,
        }:
            return "Engine check needs attention"
        return "Engine check incomplete"

    @staticmethod
    def _choice_persistent_summary(choice: DeviceChoice) -> str:
        if choice.assessment_error:
            return "Could not check browser runtime; selection remains available."
        if choice.launch_assessment_error:
            return "Could not check headless engine health; headed launch remains available."
        if choice.runtime_status is not None and not choice.runtime_status.usable:
            return "Unavailable — open Tools > Runtime Components, repair, then refresh."
        health = choice.launch_health
        if health is None or health.state is BrowserHealthState.HEALTHY:
            return ""
        if not choice.selectable:
            return "Cleanup was not confirmed — restart Automadin before retrying."
        return "Headless engine check needs attention; headed launch remains available."

    def _on_discovery_thread_finished(self) -> None:
        """Release the retained worker reference only after QThread has stopped."""
        sender = self.sender()
        if sender is not None and sender is not self._discover_thread:
            return
        if sender is self._discover_thread:
            self._discover_thread = None
        pending = self._pending_discovery_type
        self._pending_discovery_type = None
        if (
            pending is not None
            and not self._shutting_down
            and self._current_session is None
        ):
            index = self._type_combo.findData(pending)
            if index >= 0:
                self._type_combo.setCurrentIndex(index)
                self._discover_devices()
                return
        if not self._shutting_down:
            self._finish_discovery()
        self._resume_after_shutdown_wait()

    # ── Connect ────────────────────────────────────────────────────

    def _connect(self) -> None:
        if self._shutting_down:
            return
        if self._connect_thread is not None and self._connect_thread.isRunning():
            return
        device: Device | None = self._device_combo.currentData()
        choice = self._device_combo.currentData(_CHOICE_ROLE)
        if isinstance(choice, DeviceChoice) and not choice.selectable:
            QMessageBox.warning(
                self,
                "Browser Runtime Unavailable",
                self._choice_tooltip(choice, self._device_combo.currentText()),
            )
            self._update_connect_availability()
            return
        if device is None:
            QMessageBox.information(
                self, "No Device", "Select a device first (click Refresh to discover)."
            )
            return

        # Check if already connected
        existing = self._session_service.get_session_by_device(device.id)
        if existing is not None:
            self._current_session = existing
            self._show_connected()
            self.session_connected.emit(existing.session_id)
            return

        # Disable UI during connect
        self._connect_btn.setEnabled(False)
        self._connect_btn.setText("Connecting...")
        self._set_inputs_enabled(False)
        self._set_status("Connecting...", COLORS["warning"])

        worker: _ConnectThread | None = None
        try:
            worker = _ConnectThread(self._session_service, device, self)
            worker.succeeded.connect(self._on_connected)
            worker.failed.connect(self._on_connect_error)
            worker.finished.connect(self._on_connect_thread_finished)
            self._connect_thread = worker
            worker.start()
        except Exception:
            logger.exception("Could not start the device connection worker")
            startup_uncertain = qthread_start_may_have_succeeded(worker)
            if startup_uncertain:
                worker.requestInterruption()
                self._set_status(
                    "Connection startup failed; finishing its background work.",
                    COLORS["error"],
                )
            else:
                if self._connect_thread is worker:
                    self._connect_thread = None
                if worker is not None:
                    worker.deleteLater()
                self._restore_connect_controls()
                self._set_status(_CONNECTION_START_FAILURE_MESSAGE, COLORS["error"])
            QMessageBox.critical(
                self,
                (
                    "Connection Startup Uncertain"
                    if startup_uncertain
                    else "Connection Could Not Start"
                ),
                (
                    "The connection may have begun, but startup could not be "
                    "confirmed. Automadin is retaining it until background cleanup "
                    "finishes."
                    if startup_uncertain
                    else _CONNECTION_START_FAILURE_MESSAGE
                ),
            )

    def _on_connected(self, session: DeviceSession) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._connect_thread:
            return
        if self._shutting_down:
            return
        self._current_session = session
        self._show_connected()

        device_name = session.device.name if session.device else "unknown"
        self.session_connected.emit(session.session_id)
        logger.info("Connected session %s for %s", session.session_id, device_name)

    def _on_connect_error(self, error: object) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._connect_thread:
            return
        if self._shutting_down:
            return
        self._restore_connect_controls()
        self._set_status("Failed", COLORS["error"])

        if isinstance(error, _ConnectionFailure):
            failure = error
        elif isinstance(error, BrowserProfileInUseError):
            active = error.current.borrowed_by or error.current.use
            failure = _ConnectionFailure(
                "Browser Profile Already In Use",
                "This browser profile is already active.",
                f"Active operation: {active.label}\n"
                "Stop that browser activity, then connect again.",
            )
        else:
            failure = _ConnectionFailure(
                "Connection Failed",
                _CONNECTION_FAILURE_MESSAGE,
            )
        device_type: DeviceType = self._type_combo.currentData()
        hint = failure.hint
        if not hint and device_type == DeviceType.BROWSER:
            hint = (
                "Install or repair browsers from Tools > Runtime Components, "
                "then restart Automadin."
            )
        elif not hint and device_type == DeviceType.ANDROID:
            hint = (
                "Install or repair ADB from Tools > Runtime Components, "
                "then restart Automadin and connect the device."
            )

        detail = failure.message
        if hint:
            detail = f"{detail}\n\n{hint}"
        QMessageBox.critical(
            self,
            failure.title,
            detail,
        )

    def _on_connect_thread_finished(self) -> None:
        """Release the retained worker reference only after QThread has stopped."""
        sender = self.sender()
        if sender is not None and sender is not self._connect_thread:
            return
        if sender is self._connect_thread:
            self._connect_thread = None
        if (
            not self._shutting_down
            and self._current_session is None
            and self._connect_thread is None
        ):
            self._restore_connect_controls()
        self._resume_after_shutdown_wait()

    # ── Disconnect ─────────────────────────────────────────────────

    def _retry_disconnect_cleanup(self) -> bool:
        worker = self._disconnect_thread
        if (
            self._disconnect_cleanup_pending
            and worker is not None
            and not worker.isRunning()
        ):
            if not worker.release_operation_lease():
                return False
            self._disconnect_cleanup_pending = False
            if self._disconnect_thread is worker:
                self._disconnect_thread = None
            try:
                worker.deleteLater()
            except BaseException:
                logger.exception("Could not dispose disconnect cleanup worker")

        lease = self._pending_disconnect_lease
        if lease is not None:
            try:
                lease.release()
            except BaseException:
                logger.exception("Could not retry unstarted disconnect lease cleanup")
                return False
            if lease is self._pending_disconnect_lease:
                self._pending_disconnect_lease = None
        return not self._disconnect_cleanup_pending

    def _disconnect(self) -> None:
        if self._shutting_down:
            return
        if not self._retry_disconnect_cleanup():
            self._set_status(
                "Previous disconnect cleanup is still pending. Review Diagnostics "
                "and try again.",
                COLORS["error"],
            )
            return
        if self._disconnect_thread is not None:
            return
        session = self._current_session
        if session is None:
            return

        operation_lease: DeviceOperationLease | None = None
        device = session.device
        if isinstance(device, (AndroidDevice, WindowsDevice)):
            try:
                operation_lease = self._device_operation_registry.acquire(
                    device_operation_key(device),
                    DeviceOperationUse(
                        kind="device-disconnect",
                        label="Device disconnect",
                        operation_id=f"disconnect:{session.session_id}",
                    ),
                )
            except DeviceOperationConflictError as exc:
                logger.info(
                    "Device disconnect blocked by active operation: current_kind=%s",
                    exc.current.kind,
                )
                QMessageBox.warning(
                    self,
                    "Device Is Busy",
                    _DISCONNECT_CONFLICT_MESSAGE,
                )
                return
            except Exception:
                logger.exception("Could not reserve the device for disconnect")
                QMessageBox.critical(
                    self,
                    "Disconnect Could Not Start",
                    _DISCONNECT_START_FAILURE_MESSAGE,
                )
                return

        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.setText("Disconnecting...")
        self._set_status("Disconnecting device...", COLORS["warning"])
        self._disconnect_failed_during_shutdown = False

        worker: _DisconnectThread | None = None
        try:
            worker = _DisconnectThread(
                self._session_service,
                session,
                operation_lease,
                self,
            )
            worker.succeeded.connect(self._on_disconnected)
            worker.failed.connect(self._on_disconnect_error)
            worker.finished.connect(self._on_disconnect_thread_finished)
            self._disconnect_thread = worker
            worker.start()
        except Exception:
            logger.exception("Could not start the device disconnect worker")
            startup_uncertain = qthread_start_may_have_succeeded(worker)
            if startup_uncertain:
                worker.requestInterruption()
                self._set_status(
                    "Disconnect startup failed; finishing its background work.",
                    COLORS["error"],
                )
            else:
                if self._disconnect_thread is worker:
                    self._disconnect_thread = None
                if worker is not None:
                    if worker.release_operation_lease():
                        worker.deleteLater()
                    else:
                        self._disconnect_thread = worker
                        self._disconnect_cleanup_pending = True
                elif operation_lease is not None:
                    try:
                        operation_lease.release()
                    except BaseException:
                        logger.exception(
                            "Could not release the unstarted disconnect lease"
                        )
                        self._pending_disconnect_lease = operation_lease
                self._restore_connected_after_disconnect()
                self._set_status(_DISCONNECT_START_FAILURE_MESSAGE, COLORS["error"])
            QMessageBox.critical(
                self,
                (
                    "Disconnect Startup Uncertain"
                    if startup_uncertain
                    else "Disconnect Could Not Start"
                ),
                (
                    "Disconnect may have begun, but startup could not be confirmed. "
                    "Automadin is retaining it until background cleanup finishes."
                    if startup_uncertain
                    else _DISCONNECT_START_FAILURE_MESSAGE
                ),
            )

    def _on_disconnected(self, session: DeviceSession) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._disconnect_thread:
            return
        if session is not self._current_session:
            return
        self._disconnect_failed_during_shutdown = False
        self._current_session = None
        self.session_disconnected.emit()
        logger.info("Disconnected session")
        if not self._shutting_down:
            self._show_disconnected()

    def _on_disconnect_error(self, _message: str) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._disconnect_thread:
            return
        self._disconnect_failed_during_shutdown = self._shutting_down
        self._restore_connected_after_disconnect()
        self._set_status(_DISCONNECT_FAILURE_MESSAGE, COLORS["error"])
        QMessageBox.critical(
            self,
            "Disconnect Failed",
            _DISCONNECT_FAILURE_MESSAGE,
        )

    def _on_disconnect_thread_finished(self) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._disconnect_thread:
            return
        if sender is self._disconnect_thread:
            if sender.release_operation_lease():
                self._disconnect_thread = None
                self._disconnect_cleanup_pending = False
                try:
                    sender.deleteLater()
                except BaseException:
                    logger.exception("Could not dispose finished disconnect worker")
            else:
                self._disconnect_cleanup_pending = True
                self._set_status(
                    "Disconnect finished, but device cleanup is still pending. "
                    "Review Diagnostics before trying again.",
                    COLORS["error"],
                )
        if not self._shutting_down and self._current_session is not None:
            self._restore_connected_after_disconnect()
        self._resume_after_shutdown_wait()

    # ── UI helpers ─────────────────────────────────────────────────

    def _show_connected(self) -> None:
        """Switch UI to connected state."""
        if self._current_session is not None and self._current_session.device is not None:
            connected_device = self._current_session.device
            connected_id = connected_device.id
            matching_index = next(
                (
                    index
                    for index in range(self._device_combo.count())
                    if getattr(self._device_combo.itemData(index), "id", None)
                    == connected_id
                ),
                -1,
            )
            if matching_index < 0:
                self._device_combo.insertItem(
                    0,
                    connected_device.name,
                    connected_device,
                )
                matching_index = 0
            self._device_combo.setItemText(
                matching_index,
                f"CONNECTED · {connected_device.name}",
            )
            self._device_combo.setCurrentIndex(matching_index)
        self._connect_btn.setVisible(False)
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("Connect")
        self._disconnect_btn.setVisible(True)
        self._set_inputs_enabled(False)

        device_name = ""
        if self._current_session and self._current_session.device:
            device_name = self._current_session.device.name
        self._set_status(
            f"Connected: {device_name} \u2713",
            None,
            f"background-color: {COLORS['success']}; "
            f"color: {COLORS['text_on_accent']}; padding: 4px 12px; "
            "border-radius: 10px; font-weight: bold;",
        )

    def _show_disconnected(self) -> None:
        self._disconnect_btn.setVisible(False)
        self._disconnect_btn.setEnabled(True)
        self._disconnect_btn.setText("Disconnect")
        self._connect_btn.setVisible(True)
        self._connect_btn.setText("Connect")
        self._set_inputs_enabled(True)
        self._update_connect_availability()
        self._clear_status()

    def _restore_connected_after_disconnect(self) -> None:
        if self._current_session is None:
            self._show_disconnected()
            return
        self._disconnect_btn.setVisible(True)
        self._disconnect_btn.setEnabled(True)
        self._disconnect_btn.setText("Disconnect")
        self._connect_btn.setVisible(False)
        self._set_inputs_enabled(False)

    def _set_inputs_enabled(self, enabled: bool) -> None:
        self._type_combo.setEnabled(enabled)
        self._device_combo.setEnabled(enabled)
        self._refresh_btn.setEnabled(enabled)

    def _restore_connect_controls(self) -> None:
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("Connect")
        self._set_inputs_enabled(True)
        self._update_connect_availability()

    def _set_status(self, text: str, color: str | None = None, style: str | None = None) -> None:
        self._status_label.setText(text)
        if style:
            self._status_label.setStyleSheet(style)
        elif color:
            self._status_label.setStyleSheet(
                f"color: {color}; font-weight: bold; padding: 4px 8px;"
            )
        self._status_label.setVisible(True)

    def _clear_status(self) -> None:
        self._status_label.setVisible(False)
        self._status_label.setText("")
        self._status_label.setToolTip("")

    def _data_dir(self) -> Path:
        """Return the active data directory without requiring test doubles to persist."""
        db_path = getattr(self._db, "db_path", None)
        if db_path:
            return Path(db_path).resolve().parent
        return Path.cwd()

    # ── Public accessors ───────────────────────────────────────────

    def get_selected_session(self) -> DeviceSession | None:
        """Return the currently connected session, or ``None``."""
        return self._current_session

    def get_selected_device_type(self) -> DeviceType:
        return self._type_combo.currentData()

    def discover_device_type(self, device_type: DeviceType) -> None:
        """Select and discover one target type after an explicit user action."""
        if not isinstance(device_type, DeviceType):
            raise ValueError(f"Unsupported device type: {device_type!r}")
        if self._shutting_down:
            return
        if self._current_session is not None:
            self._set_status(
                "Disconnect the current target before discovering another.",
                COLORS["warning"],
            )
            return
        if self._discover_thread is not None and self._discover_thread.isRunning():
            self._pending_discovery_type = device_type
            self._set_status(
                f"{_TYPE_DISPLAY[device_type]} discovery queued after this scan.",
                COLORS["warning"],
            )
            return
        index = self._type_combo.findData(device_type)
        if index < 0:
            raise ValueError(f"Device type is not available: {device_type.value}")
        self._type_combo.setCurrentIndex(index)
        self._discover_devices()

    def refresh_profiles(self) -> None:
        """Trigger a re-discovery if browser type is selected."""
        if self._shutting_down:
            return
        if self._type_combo.currentData() == DeviceType.BROWSER:
            self.discover_device_type(DeviceType.BROWSER)

    def refresh_android_devices(self) -> None:
        """Switch to Android device discovery and refresh the device list."""
        self.discover_device_type(DeviceType.ANDROID)

    def shutdown(self, wait_ms: int = 0) -> bool:
        """Cooperatively stop selector work before its widget is destroyed.

        Device discovery and connection APIs are not generally cancellable while
        inside an operating-system or third-party call. Interruption suppresses
        late results, and this method never waits on the GUI thread. A ``False``
        result means the owning window must stay alive and retry closing after the
        operation finishes. ``wait_ms`` remains accepted for API compatibility.
        """
        del wait_ms
        self._shutting_down = True
        self._pending_discovery_type = None
        self.setEnabled(False)

        threads = [
            thread
            for thread in (
                self._discover_thread,
                self._connect_thread,
                self._disconnect_thread,
            )
            if thread is not None and thread.isRunning()
        ]
        for thread in threads:
            thread.requestInterruption()

        still_running = [thread for thread in threads if thread.isRunning()]
        cleanup_ok = self._retry_disconnect_cleanup()
        if still_running or not cleanup_ok:
            self._shutdown_wait_pending = True
            message = (
                "Finishing device discovery or connection before closing. "
                "Please wait a moment, then close Automadin again."
            )
            self._set_status(message, COLORS["warning"])
            self._status_label.setToolTip(
                "Automadin is keeping this window open so a background device "
                "operation is not destroyed while it is still running."
            )
            return False

        self._shutdown_wait_pending = False
        return True

    def abort_shutdown(self) -> bool:
        """Restore this selector when a later shutdown stage keeps the app open."""
        self._shutdown_wait_pending = True
        self._resume_after_shutdown_wait()
        return not self._shutting_down

    def _resume_after_shutdown_wait(self) -> None:
        """Restore the selector if a refused window close was not retried."""
        if not self._shutdown_wait_pending:
            return
        if any(
            thread is not None and thread.isRunning()
            for thread in (
                self._discover_thread,
                self._connect_thread,
                self._disconnect_thread,
            )
        ):
            return

        self._shutdown_wait_pending = False
        self._shutting_down = False
        self.setEnabled(True)
        self._refresh_btn.setText("Refresh")
        if self._current_session is None:
            self._show_disconnected()
        else:
            self._restore_connected_after_disconnect()
        if self._device_combo.placeholderText() == "Scanning for devices...":
            self._device_combo.setPlaceholderText(
                "Discovery stopped — click Refresh to try again"
            )
        if self._disconnect_failed_during_shutdown:
            self._disconnect_failed_during_shutdown = False
            self._set_status(_DISCONNECT_FAILURE_MESSAGE, COLORS["error"])
        else:
            self._set_status(
                "Device operation stopped. You can continue or close safely.",
                COLORS["success"],
            )
