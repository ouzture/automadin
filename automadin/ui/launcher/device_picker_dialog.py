"""Device picker dialog — searchable list of all discoverable devices."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.session import DeviceDiscoveryService
from automadin.models.device import DeviceType, saved_device_id_for_device
from automadin.services.browser_profile_metadata import profile_aware_device_name
from automadin.services.browser_health import BrowserHealthState
from automadin.services.browser_health_cache import (
    BrowserHealthCache,
    default_browser_health_cache,
)
from automadin.services.database import DatabaseService
from automadin.services.device_choices import (
    DeviceChoice,
    assess_device_choices,
    attach_browser_health,
    attach_browser_health_error,
    browser_health_statuses,
    browser_health_target_for_device,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)

_SAFE_DISCOVERY_FAILURE = (
    "Discovery is unavailable. Check this device type's runtime or connection, "
    "then reopen the dialog."
)
_SAFE_HEALTH_FAILURE = (
    "The browser launch check could not be completed. Selection remains "
    "available; the actual launch is the final check."
)
_SAFE_START_FAILURE = (
    "Device discovery could not start. Close this dialog, reopen it, and try again."
)
_SAFE_START_UNCERTAIN = (
    "Device discovery reported a startup problem. Automadin is waiting for the "
    "background scan to finish safely; review Diagnostics before trying again."
)


@dataclass(frozen=True, slots=True)
class _PickerItem:
    group: str
    display_name: str
    device_id: str
    choice: DeviceChoice

    @property
    def selectable(self) -> bool:
        return self.choice.selectable


class _DiscoverThread(QThread):
    """Discovers devices on a background thread to avoid blocking the UI."""

    discovery_ready = Signal(list, list)

    def __init__(
        self,
        discovery: DeviceDiscoveryService,
        database: DatabaseService,
        browser_health_cache: BrowserHealthCache,
        data_dir: Path,
        device_types: tuple[DeviceType, ...],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._discovery = discovery
        self._database = database
        self._browser_health_cache = browser_health_cache
        self._data_dir = data_dir
        self._device_types = device_types

    def run(self) -> None:
        results: list[_PickerItem] = []
        failures: list[tuple[str, str]] = []
        try:
            for device_type in self._device_types:
                if self.isInterruptionRequested():
                    return
                try:
                    profiles = (
                        self._database.profile_engine_specs()
                        if device_type is DeviceType.BROWSER
                        else None
                    )
                    labels = (
                        self._database.profile_display_labels()
                        if device_type is DeviceType.BROWSER
                        else {}
                    )
                    if self.isInterruptionRequested():
                        return
                    devices = self._discovery.list_devices(device_type, profiles)
                    if self.isInterruptionRequested():
                        return
                    group = device_type.name
                    device_list = list(devices)
                    choices = (
                        assess_device_choices(device_list, self._data_dir)
                        if device_type is DeviceType.BROWSER
                        else [
                            DeviceChoice(device=device, runtime_status=None)
                            for device in device_list
                        ]
                    )
                    if device_type is DeviceType.BROWSER:
                        statuses = browser_health_statuses(choices)
                        try:
                            health = self._browser_health_cache.check_many(
                                statuses,
                                self._data_dir,
                                cancel=self.isInterruptionRequested,
                            )
                        except BaseException:
                            logger.exception(
                                "Browser health checks failed during device discovery"
                            )
                            choices = attach_browser_health_error(
                                choices,
                                RuntimeError(_SAFE_HEALTH_FAILURE),
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
                    for choice in choices:
                        dev = choice.device
                        display = profile_aware_device_name(dev, labels)
                        results.append(
                            _PickerItem(
                                group,
                                display,
                                saved_device_id_for_device(dev),
                                choice,
                            )
                        )
                except BaseException:
                    logger.exception(
                        "Device discovery failed for %s",
                        device_type.value,
                    )
                    failures.append(
                        (device_type.value.title(), _SAFE_DISCOVERY_FAILURE)
                    )
            if not self.isInterruptionRequested():
                self.discovery_ready.emit(results, failures)
        except BaseException:
            logger.exception("Device discovery worker failed unexpectedly")
            if not self.isInterruptionRequested():
                self.discovery_ready.emit(
                    [],
                    [("Device discovery", _SAFE_DISCOVERY_FAILURE)],
                )
        finally:
            release = getattr(
                self._database,
                "release_current_thread_connection",
                None,
            )
            if callable(release):
                try:
                    release()
                except BaseException:
                    logger.exception(
                        "Could not release the device discovery database connection"
                    )


class DevicePickerDialog(QDialog):
    """Searchable popup for selecting a device from all discoverable options."""

    def __init__(
        self,
        discovery_service: DeviceDiscoveryService,
        db: DatabaseService,
        parent: QWidget | None = None,
        *,
        browser_health_cache: BrowserHealthCache | None = None,
        initial_type: DeviceType = DeviceType.BROWSER,
        allowed_types: tuple[DeviceType, ...] | None = None,
    ) -> None:
        super().__init__(parent)
        self._discovery = discovery_service
        self._db = db
        self._browser_health_cache = (
            browser_health_cache or default_browser_health_cache()
        )
        self._selected_id: str | None = None
        self._all_items: list[_PickerItem] = []
        self._thread: _DiscoverThread | None = None
        self._discovery_started = False
        self._startup_uncertain = False
        self._closing_requested = False
        self._allowed_types = allowed_types or (
            DeviceType.BROWSER,
            DeviceType.ANDROID,
            DeviceType.WINDOWS,
        )
        if not self._allowed_types:
            raise ValueError("At least one device capability must be selectable")
        if initial_type not in self._allowed_types:
            initial_type = self._allowed_types[0]
        self._active_type = initial_type

        self.setWindowTitle("Choose Automation Target")
        self.setMinimumSize(760, 560)
        self.setStyleSheet(
            f"background-color: {COLORS['bg_surface']}; color: {COLORS['text_primary']};"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        heading = QLabel("Choose what this run controls")
        heading.setStyleSheet("font-size: 14pt; font-weight: 650;")
        layout.addWidget(heading)

        self._type_tabs = QTabBar()
        self._type_tabs.setExpanding(True)
        self._type_tabs.setDrawBase(False)
        type_labels = {
            DeviceType.BROWSER: "Browser",
            DeviceType.ANDROID: "Android",
            DeviceType.WINDOWS: "Windows",
        }
        for device_type in self._allowed_types:
            index = self._type_tabs.addTab(type_labels[device_type])
            self._type_tabs.setTabData(index, device_type)
        self._type_tabs.setCurrentIndex(
            next(
                index
                for index in range(self._type_tabs.count())
                if self._type_tabs.tabData(index) is self._active_type
            )
        )
        self._type_tabs.currentChanged.connect(self._on_type_changed)
        layout.addWidget(self._type_tabs)

        self._scope_hint = QLabel()
        self._scope_hint.setWordWrap(True)
        self._scope_hint.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(self._scope_hint)

        self._search = QLineEdit()
        self._search.textChanged.connect(self._filter)
        layout.addWidget(self._search)
        self._refresh_scope_copy()

        self._stack = QStackedWidget()

        # Page 0: loading indicator
        loading = QLabel("Discovering devices…")
        loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10.5pt;")
        self._stack.addWidget(loading)

        # Page 1: device list
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget {{ background-color: {COLORS['bg_darkest']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; }}"
            f"QListWidget::item {{ padding: 6px 10px; }}"
            f"QListWidget::item:selected {{ background-color: {COLORS['bg_elevated']}; }}"
        )
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._stack.addWidget(self._list)

        layout.addWidget(self._stack, 1)

        self._details = QLabel()
        self._details.setWordWrap(True)
        self._details.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 10pt; "
            f"background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; "
            "padding: 6px 8px;"
        )
        self._details.hide()
        layout.addWidget(self._details)

        self._warning = QLabel()
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet(
            f"color: {COLORS['warning']}; font-size: 10pt; padding: 4px 2px;"
        )
        self._warning.hide()
        layout.addWidget(self._warning)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Select")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._discovery_started:
            return
        self._discovery_started = True
        self._startup_uncertain = False
        thread: _DiscoverThread | None = None
        try:
            thread = _DiscoverThread(
                self._discovery,
                self._db,
                self._browser_health_cache,
                self._data_dir(),
                self._allowed_types,
                self,
            )
            thread.discovery_ready.connect(self._populate)
            thread.finished.connect(self._on_discovery_finished)
            self._thread = thread
            thread.start()
        except BaseException:
            logger.exception("Device discovery worker could not be started")
            if qthread_start_may_have_succeeded(thread):
                # Keep a worker that may already be executing.  Its finished
                # signal is the only safe point at which to drop the Qt object.
                self._startup_uncertain = True
                thread.requestInterruption()
                self._warning.setText(_SAFE_START_UNCERTAIN)
                self._warning.show()
                return
            if self._thread is thread:
                self._thread = None
            self._discovery_started = False
            if thread is not None:
                try:
                    thread.deleteLater()
                except BaseException:
                    logger.exception("Could not dispose failed discovery worker")
            self._show_start_failure()

    def reject(self) -> None:
        if self._defer_rejection():
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._defer_rejection():
            event.ignore()
            return
        super().closeEvent(event)

    def _defer_rejection(self) -> bool:
        thread = self._thread
        if thread is None or not thread.isRunning():
            return False
        self._closing_requested = True
        thread.requestInterruption()
        self._search.setEnabled(False)
        self._list.setEnabled(False)
        self._buttons.setEnabled(False)
        self._warning.setText("Finishing device discovery before closing…")
        self._warning.show()
        return True

    def _on_discovery_finished(self) -> None:
        thread = self._thread
        if thread is None:
            return
        sender = self.sender()
        if isinstance(sender, _DiscoverThread) and sender is not thread:
            return
        self._thread = None
        self._startup_uncertain = False
        try:
            thread.deleteLater()
        except BaseException:
            logger.exception("Could not dispose finished discovery worker")
        if self._closing_requested:
            QDialog.reject(self)

    def _show_start_failure(self) -> None:
        self._all_items = []
        self._list.clear()
        self._stack.setCurrentIndex(1)
        self._warning.setText(_SAFE_START_FAILURE)
        self._warning.show()

    def _populate(
        self,
        items: list[_PickerItem],
        failures: list[tuple[str, str]],
    ) -> None:
        if self._closing_requested or self._startup_uncertain:
            return
        self._all_items = items
        self._filter(self._search.text())
        self._stack.setCurrentIndex(1)
        blocked_count = sum(not item.selectable for item in items)
        notices: list[str] = []
        if failures:
            details = "\n".join(f"• {group}: {message}" for group, message in failures)
            notices.append(
                "Some device types could not be discovered:\n"
                f"{details}\n"
                "Check the affected runtime or connection, then reopen this dialog."
            )
        if blocked_count:
            notices.append(
                f"{blocked_count} browser choice"
                f"{'s need' if blocked_count != 1 else ' needs'} attention. "
                "Select a row to read its reason; the Select action stays "
                "unavailable for blocked rows."
            )
        if notices:
            self._warning.setText("\n\n".join(notices))
            self._warning.show()
        else:
            self._warning.clear()
            self._warning.hide()

    def _rebuild_list(self, items: list[_PickerItem]) -> None:
        self._list.clear()
        current_group = None
        for picker_item in items:
            if picker_item.group != current_group:
                sep = QListWidgetItem(picker_item.group)
                sep.setFlags(Qt.ItemFlag.NoItemFlags)
                sep.setForeground(Qt.GlobalColor.gray)
                font = sep.font()
                font.setBold(True)
                point_size = font.pointSize()
                if point_size <= 0:
                    point_size = 10
                # Group headings are secondary through color and weight; they
                # remain at the same readable 10pt floor as other launcher text.
                font.setPointSize(max(10, point_size))
                sep.setFont(font)
                self._list.addItem(sep)
                current_group = picker_item.group
            display_name = picker_item.display_name
            suffix = self._choice_status_label(picker_item.choice)
            if suffix:
                display_name += f" — {suffix}"
            item = QListWidgetItem(display_name)
            item.setData(Qt.ItemDataRole.UserRole, picker_item.device_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, picker_item)
            tooltip = self._choice_tooltip(picker_item)
            item.setToolTip(tooltip)
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, tooltip)
            if not picker_item.selectable:
                # Keep the row focusable and readable so keyboard/screen-reader
                # users can inspect its persistent explanation.  Acceptance is
                # gated below and again by launcher preflight.
                item.setForeground(Qt.GlobalColor.white)
            self._list.addItem(item)

    def _filter(self, text: str) -> None:
        q = text.lower()
        filtered = [
            item
            for item in self._all_items
            if item.group == self._active_type.name
            and (not q or q in item.display_name.lower())
        ]
        self._rebuild_list(filtered)

    def _on_type_changed(self, index: int) -> None:
        device_type = self._type_tabs.tabData(index)
        if not isinstance(device_type, DeviceType):
            return
        self._active_type = device_type
        self._selected_id = None
        self._details.clear()
        self._details.hide()
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._refresh_scope_copy()
        self._filter(self._search.text())

    def _refresh_scope_copy(self) -> None:
        copy = {
            DeviceType.BROWSER: (
                "Choose a browser engine and viewport. A managed profile is a "
                "separate identity and sign-in choice shown in each row."
            ),
            DeviceType.ANDROID: (
                "Choose a currently discoverable ADB device or emulator. Managed "
                "emulator lifecycle profiles are selected directly in Run Setup."
            ),
            DeviceType.WINDOWS: (
                "Choose the application window Automadin should reacquire and control."
            ),
        }
        search_names = {
            DeviceType.BROWSER: "Search browser engines or profiles…",
            DeviceType.ANDROID: "Search Android devices or emulators…",
            DeviceType.WINDOWS: "Search Windows applications…",
        }
        self._scope_hint.setText(copy[self._active_type])
        self._search.setPlaceholderText(search_names[self._active_type])

    def _on_selection_changed(self) -> None:
        items = self._list.selectedItems()
        picker_item = (
            items[0].data(Qt.ItemDataRole.UserRole + 1)
            if items
            else None
        )
        if not isinstance(picker_item, _PickerItem):
            self._selected_id = None
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            self._details.clear()
            self._details.hide()
            return

        self._details.setText(self._choice_tooltip(picker_item))
        self._details.show()
        if picker_item.selectable:
            self._selected_id = picker_item.device_id
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        else:
            self._selected_id = None
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def selected_device_id(self) -> str | None:
        return self._selected_id

    @staticmethod
    def _choice_tooltip(item: _PickerItem) -> str:
        choice = item.choice
        lines = [item.display_name]
        if choice.assessment_error:
            lines.extend(
                (
                    "Could not check runtime availability.",
                    choice.assessment_error,
                    "Selection remains available; launch is the final check.",
                )
            )
        elif choice.launch_assessment_error:
            lines.extend(
                (
                    "Could not check isolated headless engine launch health.",
                    choice.launch_assessment_error,
                    "Selection remains available because this did not test the "
                    "real headed/profile launch.",
                )
            )
        elif choice.runtime_status is not None and not choice.runtime_status.usable:
            lines.extend(
                (
                    choice.runtime_status.error
                    or "The required browser runtime was not detected.",
                    "Open Tools > Runtime Components, repair or install it, then try again.",
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
                "Detected — not launch tested. Presence does not verify profile "
                "sign-in or a future launch."
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

    def _data_dir(self) -> Path:
        db_path = getattr(self._db, "db_path", None)
        if db_path:
            return Path(db_path).resolve().parent
        return Path.cwd()
