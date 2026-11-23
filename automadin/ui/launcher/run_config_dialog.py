"""Dialog for creating / editing a saved run configuration."""

from __future__ import annotations

from PySide6.QtCore import Qt

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    DeviceType,
    WindowsDevice,
    device_from_saved_id,
    display_name_for_saved_device_id,
    saved_device_id_for_device,
)
from automadin.models.resource_path import (
    find_resource_collision,
    join_resource_path,
    resource_display_label,
    resource_sort_key,
)
from automadin.models.run_config import (
    BrowserRunMode,
    RunConfig,
    RunMode,
    SavedRunConfig,
)
from automadin.models.resolution_scaling import ResolutionScalingOverride
from automadin.models.managed_emulator import EmulatorCleanupOverride
from automadin.models.flow_parameter import FlowParameterError
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION
from automadin.execution.flow_values import FlowRuntimeValueError
from automadin.services.database import DatabaseService
from automadin.ui.flow_editor.param_editor import NoScrollSpinBox
from automadin.ui.launcher.parameter_overrides import ParameterOverridesWidget
from automadin.ui.launcher.input_overrides import InputOverridesWidget
from automadin.ui.theme import COLORS
from automadin.ui.editable_combo import set_editable_combo_text


class RunConfigDialog(QDialog):
    """Modal dialog to create or edit a SavedRunConfig."""

    def __init__(
        self,
        db: DatabaseService,
        existing: SavedRunConfig | None = None,
        current_device_id: str = "",
        session_service=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._existing = existing
        self._session_service = session_service
        self._selected_device_id = ""
        self.setWindowTitle("Edit Run Setup" if existing else "New Run Setup")
        self.setMinimumWidth(420)
        self.setStyleSheet(
            f"background-color: {COLORS['bg_surface']}; color: {COLORS['text_primary']};"
        )

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)
        form.setContentsMargins(12, 12, 12, 12)

        # Name
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. rewards-desktop")
        form.addRow("Name:", self._name_edit)

        # Virtual folder
        self._group_combo = QComboBox()
        self._group_combo.setEditable(True)
        self._group_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._group_combo.addItem("")
        self._group_combo.addItems(self._load_group_paths())
        group_edit = self._group_combo.lineEdit()
        if group_edit:
            group_edit.setPlaceholderText("e.g. Mobile/Daily tasks")
        form.addRow("Folder:", self._group_combo)
        folder_hint = QLabel(
            "Choose an existing folder or type a new one; it is created when "
            "this setup is saved. Use / for subfolders; capitalization does "
            "not create a separate folder."
        )
        folder_hint.setWordWrap(True)
        folder_hint.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10pt;")
        form.addRow("", folder_hint)

        # Flow
        self._flow_combo = QComboBox()
        flow_names = sorted(db.list_flows(), key=resource_sort_key)
        folder_index = db.workspace_folder_index()
        if not flow_names:
            self._flow_combo.addItem("No flows available", "")
        for flow_name in flow_names:
            display_path = folder_index.display_path(flow_name)
            self._flow_combo.addItem(
                resource_display_label(display_path),
                flow_name,
            )
            self._flow_combo.setItemData(
                self._flow_combo.count() - 1,
                display_path,
                Qt.ItemDataRole.ToolTipRole,
            )
        form.addRow("Flow:", self._flow_combo)

        # Capability comes first; type-specific identity follows below.
        self._capability_combo = QComboBox()
        self._capability_combo.addItem("Browser", DeviceType.BROWSER)
        self._capability_combo.addItem("Android", DeviceType.ANDROID)
        self._capability_combo.addItem("Windows", DeviceType.WINDOWS)
        self._capability_combo.currentIndexChanged.connect(self._on_capability_changed)
        form.addRow("Controls:", self._capability_combo)

        self._target_type_combo = QComboBox()
        self._target_type_combo.addItem("Connected device or emulator", "device")
        self._target_type_combo.addItem("Managed emulator profile", "managed")
        self._target_type_combo.currentIndexChanged.connect(self._refresh_target_sections)
        self._android_source_label = QLabel("Android source:")
        form.addRow(self._android_source_label, self._target_type_combo)

        device_row = QHBoxLayout()
        self._device_edit = QLineEdit()
        self._device_edit.setReadOnly(True)
        self._device_edit.setPlaceholderText("Click 'Choose…' to select a device")
        self._device_edit.setStyleSheet(f"color: {COLORS['accent']};")
        device_row.addWidget(self._device_edit)
        self._choose_device_btn = QPushButton("Choose…")
        self._choose_device_btn.setFixedWidth(80)
        self._choose_device_btn.clicked.connect(self._on_choose_device)
        device_row.addWidget(self._choose_device_btn)
        device_widget = QWidget()
        device_widget.setLayout(device_row)
        self._device_label = QLabel("Device:")
        form.addRow(self._device_label, device_widget)
        self._device_widget = device_widget

        self._managed_emulator_combo = QComboBox()
        profiles = self._db.list_managed_emulator_profiles()
        if not profiles:
            self._managed_emulator_combo.addItem(
                "No managed emulator profiles — use Tools > Managed Emulators",
                "",
            )
        else:
            for profile in profiles:
                self._managed_emulator_combo.addItem(profile.name, profile.profile_id)
        self._managed_emulator_label = QLabel("Emulator profile:")
        form.addRow(self._managed_emulator_label, self._managed_emulator_combo)

        self._cleanup_override_combo = QComboBox()
        self._cleanup_override_combo.addItem("Use profile default", EmulatorCleanupOverride.INHERIT)
        self._cleanup_override_combo.addItem(
            "Close if Automadin started it", EmulatorCleanupOverride.CLOSE_IF_STARTED
        )
        self._cleanup_override_combo.addItem("Leave running", EmulatorCleanupOverride.LEAVE_RUNNING)
        self._cleanup_override_combo.addItem("Always close", EmulatorCleanupOverride.ALWAYS_CLOSE)
        self._cleanup_override_label = QLabel("After run:")
        form.addRow(self._cleanup_override_label, self._cleanup_override_combo)

        # Browser session details (shown only for browser devices)
        self._browser_section = QGroupBox("Browser Session")
        self._browser_section.setStyleSheet(
            f"QGroupBox {{ border: 1px solid {COLORS['border']}; border-radius: 6px; "
            f"margin-top: 8px; padding-top: 10px; color: {COLORS['text_secondary']}; }}"
        )
        browser_form = QFormLayout(self._browser_section)
        browser_form.setSpacing(6)
        browser_form.setContentsMargins(10, 10, 10, 8)

        self._engine_field_label = QLabel("Engine:")
        self._engine_label = QLabel("—")
        browser_form.addRow(self._engine_field_label, self._engine_label)

        self._screen_field_label = QLabel("Screen Mode:")
        self._screen_label = QLabel("—")
        browser_form.addRow(self._screen_field_label, self._screen_label)

        self._profile_field_label = QLabel("Profile:")
        self._profile_label = QLabel("—")
        browser_form.addRow(self._profile_field_label, self._profile_label)

        self._browser_run_mode_combo = QComboBox()
        self._browser_run_mode_combo.addItem(
            "Launch browser for this run",
            BrowserRunMode.LAUNCH,
        )
        self._browser_run_mode_combo.addItem(
            "Use currently connected browser",
            BrowserRunMode.CONNECTED,
        )
        self._browser_run_mode_combo.setToolTip(
            "Connected mode uses the browser currently connected in Automadin "
            "when the run starts. It does not save a transient browser session."
        )
        self._browser_run_mode_combo.currentIndexChanged.connect(self._refresh_browser_run_mode)
        browser_form.addRow("Browser lifecycle:", self._browser_run_mode_combo)

        self._connected_browser_hint = QLabel()
        self._connected_browser_hint.setWordWrap(True)
        self._connected_browser_hint.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt;"
        )
        browser_form.addRow("", self._connected_browser_hint)

        self._headless_combo = QComboBox()
        self._headless_combo.addItem("Headed")
        self._headless_combo.addItem("Headless")
        self._headless_combo.currentIndexChanged.connect(self._refresh_headless_identity_state)
        browser_form.addRow("Mode:", self._headless_combo)

        self._headless_identity_cb = QCheckBox("Use headed-compatible browser identity")
        self._headless_identity_cb.setChecked(True)
        self._headless_identity_cb.setToolTip(
            "In headless desktop mode, use the regular browser user-agent "
            "instead of identifying as HeadlessChrome. Other automation "
            "signals are not hidden."
        )
        browser_form.addRow("", self._headless_identity_cb)

        self._browser_section.setVisible(False)
        form.addRow(self._browser_section)

        self._windows_section = QGroupBox("Windows Application")
        windows_layout = QVBoxLayout(self._windows_section)
        windows_layout.setContentsMargins(10, 10, 10, 8)
        self._windows_auto_start = QCheckBox("Start the application if it is closed")
        self._windows_auto_start.setChecked(False)
        self._windows_auto_start.setToolTip(
            "When disabled, the run stops if the saved Windows application is not already open."
        )
        windows_layout.addWidget(self._windows_auto_start)
        self._windows_section.setVisible(False)
        form.addRow(self._windows_section)

        # Mode
        self._mode_combo = QComboBox()
        for mode in RunMode:
            self._mode_combo.addItem(mode.value.upper(), mode)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Mode:", self._mode_combo)

        # Count (for COUNT mode)
        self._count_spin = NoScrollSpinBox()
        self._count_spin.setMinimum(1)
        self._count_spin.setMaximum(99999)
        self._count_spin.setValue(1)
        self._count_label = QLabel("Count:")
        form.addRow(self._count_label, self._count_spin)

        # Duration (for DURATION mode)
        self._duration_spin = NoScrollSpinBox()
        self._duration_spin.setMinimum(1)
        self._duration_spin.setMaximum(86400)
        self._duration_spin.setValue(60)
        self._duration_spin.setSuffix(" s")
        self._duration_label = QLabel("Duration:")
        form.addRow(self._duration_label, self._duration_spin)

        # Delay
        self._delay_spin = NoScrollSpinBox()
        self._delay_spin.setMinimum(0)
        self._delay_spin.setMaximum(60000)
        self._delay_spin.setValue(0)
        self._delay_spin.setSingleStep(100)
        self._delay_spin.setSuffix(" ms")
        form.addRow("Delay:", self._delay_spin)

        # Stop on error
        self._stop_on_error_cb = QCheckBox("Stop execution on first error")
        self._stop_on_error_cb.setChecked(True)
        form.addRow("", self._stop_on_error_cb)

        self._ask_for_values_cb = QCheckBox("Ask for values before starting")
        self._ask_for_values_cb.setChecked(True)
        self._ask_for_values_cb.setToolTip(
            "When disabled, manual and tray launches use saved overrides and "
            "flow defaults without opening input or parameter dialogs."
        )
        form.addRow("", self._ask_for_values_cb)

        self._resolution_override_combo = QComboBox()
        self._resolution_override_combo.setObjectName("resolutionScalingOverride")
        self._resolution_override_combo.addItem(
            "Use flow setting", ResolutionScalingOverride.INHERIT
        )
        self._resolution_override_combo.addItem("Force off", ResolutionScalingOverride.OFF)
        self._resolution_override_combo.addItem(
            "Force adaptive", ResolutionScalingOverride.ADAPTIVE
        )
        self._resolution_override_combo.addItem("Custom scale", ResolutionScalingOverride.CUSTOM)
        self._resolution_override_combo.setToolTip(
            "Override resolution-aware execution for this saved run only."
        )
        self._resolution_override_combo.currentIndexChanged.connect(
            self._refresh_resolution_override
        )
        form.addRow("Resolution scaling:", self._resolution_override_combo)

        self._custom_scale_spin = NoScrollSpinBox()
        self._custom_scale_spin.setObjectName("customResolutionScale")
        self._custom_scale_spin.setRange(25, 400)
        self._custom_scale_spin.setValue(100)
        self._custom_scale_spin.setSuffix(" %")
        self._custom_scale_label = QLabel("Custom scale:")
        form.addRow(self._custom_scale_label, self._custom_scale_spin)

        layout.addLayout(form)

        self._parameter_overrides = ParameterOverridesWidget("Saved Flow Parameter Overrides")
        layout.addWidget(self._parameter_overrides)

        self._input_overrides_label = QLabel("Saved Flow Input Overrides")
        self._input_overrides_label.setStyleSheet("font-weight: 650;")
        layout.addWidget(self._input_overrides_label)
        self._input_overrides = InputOverridesWidget(mode="saved")
        layout.addWidget(self._input_overrides)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Pre-fill from existing or defaults
        if existing:
            self._name_edit.setText(existing.name)
            set_editable_combo_text(
                self._group_combo,
                self._db.workspace_folder_index().canonicalize_existing(existing.group_path)
            )
            idx = self._flow_combo.findData(existing.flow_name)
            if idx >= 0:
                self._flow_combo.setCurrentIndex(idx)
            self._set_selected_device_id(existing.device_id)
            self._select_capability_for_saved_target(existing.device_id)
            self._refresh_browser_section(existing.device_id)
            self._refresh_windows_section(existing.device_id)
            if existing.managed_emulator_profile_id:
                self._capability_combo.setCurrentIndex(
                    self._capability_combo.findData(DeviceType.ANDROID)
                )
                self._target_type_combo.setCurrentIndex(self._target_type_combo.findData("managed"))
                profile_index = self._managed_emulator_combo.findData(
                    existing.managed_emulator_profile_id
                )
                if profile_index >= 0:
                    self._managed_emulator_combo.setCurrentIndex(profile_index)
                cleanup_index = self._cleanup_override_combo.findData(
                    existing.emulator_cleanup_override
                )
                if cleanup_index >= 0:
                    self._cleanup_override_combo.setCurrentIndex(cleanup_index)
            headless = existing.browser_options.get("headless", False)
            self._headless_combo.setCurrentIndex(1 if headless else 0)
            browser_mode_index = self._browser_run_mode_combo.findData(existing.browser_run_mode)
            if browser_mode_index >= 0:
                self._browser_run_mode_combo.setCurrentIndex(browser_mode_index)
            self._headless_identity_cb.setChecked(
                bool(
                    existing.browser_options.get(
                        "headless_uses_headed_user_agent",
                        True,
                    )
                )
            )
            mode_idx = self._mode_combo.findData(existing.config.mode)
            if mode_idx >= 0:
                self._mode_combo.setCurrentIndex(mode_idx)
            self._count_spin.setValue(existing.config.count)
            self._duration_spin.setValue(existing.config.duration_seconds)
            self._delay_spin.setValue(existing.config.delay_between_runs_ms)
            self._stop_on_error_cb.setChecked(existing.config.stop_on_error)
            self._ask_for_values_cb.setChecked(existing.ask_for_values)
            scaling_index = self._resolution_override_combo.findData(
                existing.config.resolution_scaling_override
            )
            if scaling_index >= 0:
                self._resolution_override_combo.setCurrentIndex(scaling_index)
            self._custom_scale_spin.setValue(existing.config.custom_scale_percent)
        else:
            self._set_selected_device_id(current_device_id)
            if current_device_id:
                self._select_capability_for_saved_target(current_device_id)
                self._refresh_browser_section(current_device_id)
                self._refresh_windows_section(current_device_id)

        self._flow_combo.currentIndexChanged.connect(self._refresh_parameter_overrides)
        self._refresh_parameter_overrides()
        self._on_mode_changed()
        self._refresh_target_sections()
        self._refresh_browser_run_mode()
        self._refresh_resolution_override()

    def _load_group_paths(self) -> list[str]:
        return self._db.list_resource_folders()

    def _resolved_group_path(self) -> str:
        return self._db.resolve_resource_folder(
            self._group_combo.currentText(),
            ignore_run_config=self._existing.name if self._existing is not None else None,
            candidate_is_run_group=True,
        )

    def _refresh_resolution_override(self, _index: int = -1) -> None:
        override = ResolutionScalingOverride(self._resolution_override_combo.currentData())
        custom = override is ResolutionScalingOverride.CUSTOM
        self._custom_scale_label.setVisible(custom)
        self._custom_scale_spin.setVisible(custom)

    # ── Browser section ───────────────────────────────────────────────

    def _refresh_browser_section(self, device_id: object) -> None:
        """Refresh launch-target details without owning section visibility."""
        try:
            device = device_from_saved_id(device_id)
            if not isinstance(device, BrowserDevice):
                raise ValueError("Not a browser device")
            cfg = device.config
            self._engine_label.setText(cfg.engine.name.title())
            self._screen_label.setText(cfg.screen_mode.name.replace("_", " ").title())
            if cfg.cdp_endpoint:
                profile_label = (
                    f"Automadin-managed persistent profile at {cfg.cdp_endpoint}"
                    if cfg.cdp_auto_launch
                    else f"External browser at {cfg.cdp_endpoint}"
                )
            elif cfg.profile_id:
                profile_label = self._db.get_profile_display_label(cfg.profile_id)
            elif cfg.profile_name:
                profile_label = f"{cfg.profile_name} (needs setup)"
            else:
                profile_label = "Temporary — data discarded"
            self._profile_label.setText(profile_label)
        except (TypeError, ValueError, IndexError, AttributeError):
            self._engine_label.setText("—")
            self._screen_label.setText("—")
            self._profile_label.setText("—")

    def _refresh_windows_section(self, device_id: object) -> None:
        """Show the explicit, default-off Windows startup policy."""
        try:
            device = device_from_saved_id(device_id)
        except (TypeError, ValueError, IndexError, AttributeError):
            device = None
        is_windows = isinstance(device, WindowsDevice)
        self._windows_section.setVisible(is_windows)
        self._windows_auto_start.setChecked(bool(device.auto_start) if is_windows else False)

    def _on_choose_device(self) -> None:
        """Open the device picker and write the selection into the device field."""
        if self._session_service is None:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "No Discovery", "Session service not available.")
            return
        from automadin.ui.launcher.device_picker_dialog import DevicePickerDialog

        capability = self._capability_combo.currentData()
        if not isinstance(capability, DeviceType):
            capability = DeviceType.BROWSER
        dlg = DevicePickerDialog(
            self._session_service.discovery,
            self._db,
            parent=self,
            initial_type=capability,
            allowed_types=(capability,),
        )
        if dlg.exec():
            dev_id = dlg.selected_device_id()
            if dev_id:
                self._set_selected_device_id(dev_id)
                self._refresh_browser_section(dev_id)
                self._refresh_windows_section(dev_id)

    def _refresh_target_sections(self) -> None:
        capability = self._capability_combo.currentData()
        is_android = capability is DeviceType.ANDROID
        managed = is_android and self._target_type_combo.currentData() == "managed"
        connected_browser = (
            capability is DeviceType.BROWSER
            and self._browser_run_mode_combo.currentData() is BrowserRunMode.CONNECTED
        )
        self._android_source_label.setVisible(is_android)
        self._target_type_combo.setVisible(is_android)
        self._device_label.setVisible(not managed and not connected_browser)
        self._device_widget.setVisible(not managed and not connected_browser)
        self._managed_emulator_label.setVisible(managed)
        self._managed_emulator_combo.setVisible(managed)
        self._cleanup_override_label.setVisible(managed)
        self._cleanup_override_combo.setVisible(managed)
        self._device_label.setText(
            {
                DeviceType.BROWSER: "Browser target:",
                DeviceType.ANDROID: "Android target:",
                DeviceType.WINDOWS: "Windows target:",
            }.get(capability, "Target:")
        )
        self._browser_section.setVisible(capability is DeviceType.BROWSER)
        if managed:
            self._windows_section.setVisible(False)
        else:
            self._refresh_browser_section(
                self._selected_device_id if capability is DeviceType.BROWSER else ""
            )
            self._refresh_windows_section(
                self._selected_device_id if capability is DeviceType.WINDOWS else ""
            )

    def _on_capability_changed(self) -> None:
        selected = self._capability_for_saved_target(self._selected_device_id)
        current = self._capability_combo.currentData()
        if selected is not None and selected is not current:
            self._set_selected_device_id("")
        self._refresh_target_sections()

    @staticmethod
    def _capability_for_saved_target(device_id: object) -> DeviceType | None:
        try:
            device = device_from_saved_id(device_id)
        except (TypeError, ValueError, IndexError, AttributeError):
            return None
        if isinstance(device, BrowserDevice):
            return DeviceType.BROWSER
        if isinstance(device, AndroidDevice):
            return DeviceType.ANDROID
        if isinstance(device, WindowsDevice):
            return DeviceType.WINDOWS
        return None

    def _select_capability_for_saved_target(self, device_id: object) -> None:
        capability = self._capability_for_saved_target(device_id)
        if capability is None:
            return
        index = self._capability_combo.findData(capability)
        if index >= 0:
            self._capability_combo.setCurrentIndex(index)

    def _set_selected_device_id(self, device_id: object) -> None:
        self._selected_device_id = device_id.strip() if isinstance(device_id, str) else ""
        display_name = display_name_for_saved_device_id(device_id)
        try:
            device = device_from_saved_id(self._selected_device_id)
            if isinstance(device, BrowserDevice):
                cfg = device.config
                if cfg.cdp_endpoint:
                    self._device_edit.setText(cfg.to_display_name())
                    return
                base = BrowserDeviceConfig(
                    engine=cfg.engine,
                    headless=cfg.headless,
                    screen_mode=cfg.screen_mode,
                ).to_display_name()
                if cfg.profile_id:
                    profile_label = self._db.get_profile_display_label(cfg.profile_id)
                    display_name = f"{base} · Profile: {profile_label}"
                elif cfg.profile_name:
                    display_name = f"{base} · Profile: {cfg.profile_name} (needs setup)"
                else:
                    display_name = f"{base} · Temporary profile"
        except (TypeError, ValueError, IndexError):
            pass
        self._device_edit.setText(display_name)

    # ── Mode visibility ───────────────────────────────────────────────

    def _on_mode_changed(self) -> None:
        mode = self._mode_combo.currentData()
        self._count_label.setVisible(mode == RunMode.COUNT)
        self._count_spin.setVisible(mode == RunMode.COUNT)
        self._duration_label.setVisible(mode == RunMode.DURATION)
        self._duration_spin.setVisible(mode == RunMode.DURATION)

    def _refresh_headless_identity_state(self) -> None:
        self._headless_identity_cb.setEnabled(
            self._headless_combo.isEnabled() and self._headless_combo.currentIndex() == 1
        )

    def _refresh_browser_run_mode(self, _index: int = -1) -> None:
        launches_browser = self._browser_run_mode_combo.currentData() is BrowserRunMode.LAUNCH
        self._headless_combo.setEnabled(launches_browser)
        for widget in (
            self._engine_field_label,
            self._engine_label,
            self._screen_field_label,
            self._screen_label,
            self._profile_field_label,
            self._profile_label,
        ):
            widget.setVisible(launches_browser)
        self._connected_browser_hint.setVisible(not launches_browser)
        if not launches_browser:
            connected_name = self._connected_browser_name()
            self._connected_browser_hint.setText(
                f"Will use the live connected browser: {connected_name}"
                if connected_name
                else "Will use whichever browser is connected in the top target bar "
                "when this run starts. No browser target or session ID is saved."
            )
        self._refresh_headless_identity_state()
        self._refresh_target_sections()

    def _connected_browser_name(self) -> str:
        list_sessions = getattr(self._session_service, "list_sessions", None)
        if not callable(list_sessions):
            return ""
        sessions = [
            session
            for session in list_sessions()
            if isinstance(getattr(session, "device", None), BrowserDevice)
        ]
        if len(sessions) != 1:
            return ""
        return sessions[0].device.name

    def _refresh_parameter_overrides(self) -> None:
        flow_name = str(self._flow_combo.currentData() or "")
        flow = self._db.load_flow(flow_name) if flow_name else None
        is_v1 = bool(
            flow is not None and flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
        )
        parameter_overrides = (
            self._existing.parameter_overrides
            if not is_v1 and self._existing is not None and flow_name == self._existing.flow_name
            else {}
        )
        self._parameter_overrides.set_parameters(
            flow.parameters if flow is not None and not is_v1 else [],
            parameter_overrides,
        )
        input_overrides = (
            self._existing.input_overrides
            if is_v1 and self._existing is not None and flow_name == self._existing.flow_name
            else {}
        )
        self._input_overrides.set_inputs(
            flow.inputs if flow is not None and is_v1 else [],
            input_overrides,
        )
        self._input_overrides_label.setVisible(self._input_overrides.isVisible())

    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._name_edit.setFocus()
            return
        try:
            folder = self._resolved_group_path()
            join_resource_path(folder, name)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Run Setup", str(exc))
            self._name_edit.setFocus()
            return
        collision = find_resource_collision(
            name,
            self._db.list_run_configs(),
            ignore_path=self._existing.name if self._existing is not None else "",
        )
        if collision is not None:
            QMessageBox.warning(
                self,
                "Name Taken",
                f"A run setup named '{collision}' already exists.",
            )
            self._name_edit.setFocus()
            return
        if not self._flow_combo.currentData():
            QMessageBox.warning(
                self,
                "Flow Required",
                "Choose a saved flow before creating this run setup.",
            )
            self._flow_combo.setFocus()
            return
        scaling_override = ResolutionScalingOverride(self._resolution_override_combo.currentData())
        if scaling_override is ResolutionScalingOverride.ADAPTIVE:
            flow = self._db.load_flow(str(self._flow_combo.currentData()))
            if flow is None or flow.resolution_scaling.reference_size is None:
                QMessageBox.warning(
                    self,
                    "Reference Surface Required",
                    "Force adaptive needs a reference width and height on the "
                    "selected flow. Open the flow's Scaling settings and record "
                    "its authored browser viewport, Android display, or Windows "
                    "app-window size first.",
                )
                self._resolution_override_combo.setFocus()
                return
        if (
            self._capability_combo.currentData() is DeviceType.ANDROID
            and self._target_type_combo.currentData() == "managed"
            and not self._managed_emulator_combo.currentData()
        ):
            QMessageBox.warning(
                self,
                "Managed Emulator Required",
                "Create or choose a managed emulator profile first.",
            )
            self._managed_emulator_combo.setFocus()
            return
        managed = (
            self._capability_combo.currentData() is DeviceType.ANDROID
            and self._target_type_combo.currentData() == "managed"
        )
        connected_browser = (
            self._capability_combo.currentData() is DeviceType.BROWSER
            and self._browser_run_mode_combo.currentData() is BrowserRunMode.CONNECTED
        )
        if not managed and not connected_browser:
            selected_capability = self._capability_for_saved_target(self._selected_device_id)
            if selected_capability is not self._capability_combo.currentData():
                QMessageBox.warning(
                    self,
                    "Target Required",
                    "Choose a target that matches the selected capability.",
                )
                self._choose_device_btn.setFocus()
                return
        try:
            self._parameter_overrides.overrides()
        except FlowParameterError as exc:
            QMessageBox.warning(self, "Invalid Flow Parameters", str(exc))
            return
        try:
            self._input_overrides.overrides()
        except FlowRuntimeValueError as exc:
            QMessageBox.warning(self, "Invalid Flow Inputs", str(exc))
            return
        set_editable_combo_text(self._group_combo, folder)
        self.accept()

    def get_saved_run_config(self) -> SavedRunConfig:
        """Build a SavedRunConfig from the dialog fields."""
        mode = self._mode_combo.currentData()
        config = RunConfig(
            mode=mode,
            count=self._count_spin.value(),
            duration_seconds=self._duration_spin.value(),
            stop_on_error=self._stop_on_error_cb.isChecked(),
            delay_between_runs_ms=self._delay_spin.value(),
            resolution_scaling_override=ResolutionScalingOverride(
                self._resolution_override_combo.currentData()
            ),
            custom_scale_percent=self._custom_scale_spin.value(),
        )
        browser_options: dict = {}
        browser_run_mode = BrowserRunMode.LAUNCH
        if self._capability_combo.currentData() is DeviceType.BROWSER:
            browser_run_mode = self._browser_run_mode_combo.currentData()
            browser_options["headless"] = self._headless_combo.currentIndex() == 1
            browser_options["headless_uses_headed_user_agent"] = (
                self._headless_identity_cb.isChecked()
            )
        device_id = self._selected_device_id
        if browser_run_mode is BrowserRunMode.CONNECTED:
            device_id = ""
        managed_profile_id = ""
        cleanup_override = EmulatorCleanupOverride.INHERIT
        if (
            self._capability_combo.currentData() is DeviceType.ANDROID
            and self._target_type_combo.currentData() == "managed"
        ):
            managed_profile_id = str(self._managed_emulator_combo.currentData() or "")
            cleanup_override = self._cleanup_override_combo.currentData()
        try:
            device = device_from_saved_id(device_id)
        except (TypeError, ValueError, IndexError):
            device = None
        if isinstance(device, WindowsDevice):
            device.auto_start = self._windows_auto_start.isChecked()
            device_id = saved_device_id_for_device(device)
        return SavedRunConfig(
            name=self._name_edit.text().strip(),
            flow_name=str(self._flow_combo.currentData() or ""),
            device_id=device_id,
            group_path=self._resolved_group_path(),
            config=config,
            variables=dict(self._existing.variables) if self._existing else {},
            browser_options=browser_options,
            browser_run_mode=browser_run_mode,
            parameter_overrides=self._parameter_overrides.overrides(),
            input_overrides=self._input_overrides.overrides(),
            managed_emulator_profile_id=managed_profile_id,
            emulator_cleanup_override=cleanup_override,
            is_pinned=self._existing.is_pinned if self._existing is not None else False,
            ask_for_values=self._ask_for_values_cb.isChecked(),
        )
