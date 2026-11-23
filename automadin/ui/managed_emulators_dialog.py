"""Create and maintain reusable managed Android emulator profiles."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from automadin.models.managed_emulator import (
    EmulatorCleanupPolicy,
    EmulatorProcessCommand,
    ManagedEmulatorProfile,
    ManagedEmulatorTargetMode,
)
from automadin.services.database import DatabaseService


def _arguments_from_text(value: str) -> tuple[str, ...]:
    return tuple(line for line in (part.strip() for part in value.splitlines()) if line)


class ManagedEmulatorsDialog(QDialog):
    """Profile list and editor without starting or stopping an emulator."""

    def __init__(self, db: DatabaseService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._profile_id = ""
        self.profiles_changed = False
        self.setWindowTitle("Managed Emulators")
        self.resize(820, 620)

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        self._profiles = QListWidget()
        self._profiles.currentItemChanged.connect(self._load_selected)
        left.addWidget(QLabel("Profiles"))
        left.addWidget(self._profiles, 1)
        profile_actions = QHBoxLayout()
        new_button = QPushButton("New")
        new_button.clicked.connect(self._new_profile)
        delete_button = QPushButton("Delete")
        delete_button.clicked.connect(self._delete_profile)
        profile_actions.addWidget(new_button)
        profile_actions.addWidget(delete_button)
        left.addLayout(profile_actions)
        root.addLayout(left, 1)

        editor = QVBoxLayout()
        note = QLabel(
            "Commands are stored as an executable plus one argument per line. "
            "Automadin matches only the exact provider instance or ADB endpoint."
        )
        note.setWordWrap(True)
        editor.addWidget(note)
        form = QFormLayout()
        self._name = QLineEdit()
        form.addRow("Name:", self._name)
        self._launch_executable = QLineEdit()
        form.addRow("Launch executable:", self._launch_executable)
        self._launch_arguments = QPlainTextEdit()
        self._launch_arguments.setMaximumHeight(72)
        self._launch_arguments.setPlaceholderText("Example: start 0 (one argument per line)")
        form.addRow("Launch arguments:", self._launch_arguments)
        self._launch_working_directory = QLineEdit()
        form.addRow("Launch working directory:", self._launch_working_directory)

        self._target_mode = QComboBox()
        self._target_mode.addItem("Provider instance", ManagedEmulatorTargetMode.PROVIDER_INSTANCE)
        self._target_mode.addItem("Exact ADB endpoint", ManagedEmulatorTargetMode.EXACT_ADB_ENDPOINT)
        self._target_mode.currentIndexChanged.connect(self._refresh_target_fields)
        form.addRow("Target match:", self._target_mode)
        self._provider_id = QLineEdit()
        self._provider_id.setPlaceholderText("mumu")
        self._provider_id_label = QLabel("Provider ID:")
        form.addRow(self._provider_id_label, self._provider_id)
        self._instance_id = QLineEdit()
        self._instance_id.setPlaceholderText("0")
        self._instance_id_label = QLabel("Instance ID:")
        form.addRow(self._instance_id_label, self._instance_id)
        self._adb_endpoint = QLineEdit()
        self._adb_endpoint.setPlaceholderText("127.0.0.1:16384")
        self._adb_endpoint_label = QLabel("ADB endpoint:")
        form.addRow(self._adb_endpoint_label, self._adb_endpoint)

        self._startup_timeout = QSpinBox()
        self._startup_timeout.setRange(5, 600)
        self._startup_timeout.setValue(120)
        self._startup_timeout.setSuffix(" s")
        form.addRow("Startup timeout:", self._startup_timeout)
        self._cleanup = QComboBox()
        self._cleanup.addItem("Close if Automadin started it", EmulatorCleanupPolicy.CLOSE_IF_STARTED)
        self._cleanup.addItem("Leave running", EmulatorCleanupPolicy.LEAVE_RUNNING)
        self._cleanup.addItem("Always close", EmulatorCleanupPolicy.ALWAYS_CLOSE)
        form.addRow("Default after run:", self._cleanup)
        self._shutdown_executable = QLineEdit()
        form.addRow("Shutdown executable:", self._shutdown_executable)
        self._shutdown_arguments = QPlainTextEdit()
        self._shutdown_arguments.setMaximumHeight(72)
        self._shutdown_arguments.setPlaceholderText("One argument per line")
        form.addRow("Shutdown arguments:", self._shutdown_arguments)
        self._shutdown_working_directory = QLineEdit()
        form.addRow("Shutdown working directory:", self._shutdown_working_directory)
        self._shutdown_timeout = QSpinBox()
        self._shutdown_timeout.setRange(1, 120)
        self._shutdown_timeout.setValue(30)
        self._shutdown_timeout.setSuffix(" s")
        form.addRow("Shutdown timeout:", self._shutdown_timeout)
        self._force_owned = QCheckBox(
            "If graceful shutdown fails, terminate only the process Automadin started"
        )
        form.addRow("", self._force_owned)
        editor.addLayout(form)

        save_button = QPushButton("Save Profile")
        save_button.clicked.connect(self._save_profile)
        editor.addWidget(save_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        editor.addWidget(buttons)
        root.addLayout(editor, 2)

        self._reload_profiles()
        self._refresh_target_fields()

    def _reload_profiles(self, select_id: str = "") -> None:
        self._profiles.clear()
        selected: QListWidgetItem | None = None
        for profile in self._db.list_managed_emulator_profiles():
            item = QListWidgetItem(profile.name)
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_id)
            self._profiles.addItem(item)
            if profile.profile_id == select_id:
                selected = item
        if selected is not None:
            self._profiles.setCurrentItem(selected)

    def _new_profile(self) -> None:
        self._profiles.clearSelection()
        self._profile_id = ""
        for edit in (
            self._name,
            self._launch_executable,
            self._launch_working_directory,
            self._provider_id,
            self._instance_id,
            self._adb_endpoint,
            self._shutdown_executable,
            self._shutdown_working_directory,
        ):
            edit.clear()
        self._launch_arguments.clear()
        self._shutdown_arguments.clear()
        self._startup_timeout.setValue(120)
        self._shutdown_timeout.setValue(30)
        self._cleanup.setCurrentIndex(0)
        self._force_owned.setChecked(False)
        self._name.setFocus()

    def _load_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        profile = self._db.get_managed_emulator_profile(
            str(current.data(Qt.ItemDataRole.UserRole))
        )
        if profile is None:
            return
        self._profile_id = profile.profile_id
        self._name.setText(profile.name)
        self._launch_executable.setText(profile.launch_command.executable_path)
        self._launch_arguments.setPlainText("\n".join(profile.launch_command.arguments))
        self._launch_working_directory.setText(profile.launch_command.working_directory)
        self._target_mode.setCurrentIndex(self._target_mode.findData(profile.target_mode))
        self._provider_id.setText(profile.provider_id)
        self._instance_id.setText(profile.provider_instance_id)
        self._adb_endpoint.setText(profile.adb_endpoint)
        self._startup_timeout.setValue(profile.startup_timeout_seconds)
        self._cleanup.setCurrentIndex(self._cleanup.findData(profile.default_cleanup_policy))
        shutdown = profile.shutdown_command
        self._shutdown_executable.setText(shutdown.executable_path if shutdown else "")
        self._shutdown_arguments.setPlainText(
            "\n".join(shutdown.arguments) if shutdown else ""
        )
        self._shutdown_working_directory.setText(shutdown.working_directory if shutdown else "")
        self._shutdown_timeout.setValue(profile.shutdown_timeout_seconds)
        self._force_owned.setChecked(profile.force_terminate_owned_process)

    def _refresh_target_fields(self) -> None:
        provider = self._target_mode.currentData() is ManagedEmulatorTargetMode.PROVIDER_INSTANCE
        self._provider_id_label.setVisible(provider)
        self._provider_id.setVisible(provider)
        self._instance_id_label.setVisible(provider)
        self._instance_id.setVisible(provider)
        self._adb_endpoint_label.setVisible(not provider)
        self._adb_endpoint.setVisible(not provider)

    def _save_profile(self) -> None:
        shutdown = None
        if self._shutdown_executable.text().strip():
            shutdown = EmulatorProcessCommand(
                self._shutdown_executable.text(),
                _arguments_from_text(self._shutdown_arguments.toPlainText()),
                self._shutdown_working_directory.text(),
            )
        try:
            values = dict(
                name=self._name.text(),
                launch_command=EmulatorProcessCommand(
                    self._launch_executable.text(),
                    _arguments_from_text(self._launch_arguments.toPlainText()),
                    self._launch_working_directory.text(),
                ),
                target_mode=self._target_mode.currentData(),
                provider_id=self._provider_id.text(),
                provider_instance_id=self._instance_id.text(),
                adb_endpoint=self._adb_endpoint.text(),
                startup_timeout_seconds=self._startup_timeout.value(),
                default_cleanup_policy=self._cleanup.currentData(),
                shutdown_command=shutdown,
                shutdown_timeout_seconds=self._shutdown_timeout.value(),
                force_terminate_owned_process=self._force_owned.isChecked(),
            )
            if self._profile_id:
                values["profile_id"] = self._profile_id
            profile = ManagedEmulatorProfile(**values)
            self._db.save_managed_emulator_profile(profile)
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid Managed Emulator", str(exc))
            return
        self._profile_id = profile.profile_id
        self.profiles_changed = True
        self._reload_profiles(profile.profile_id)

    def _delete_profile(self) -> None:
        if not self._profile_id:
            return
        references = self._db.list_run_configs_referencing_managed_emulator(
            self._profile_id
        )
        if references:
            names = ", ".join(config.name for config in references)
            QMessageBox.warning(
                self,
                "Profile In Use",
                f"This profile is used by these run setups: {names}",
            )
            return
        if QMessageBox.question(
            self,
            "Delete Managed Emulator",
            "Delete this managed emulator profile? The emulator itself is not changed.",
        ) != QMessageBox.StandardButton.Yes:
            return
        if self._db.delete_managed_emulator_profile(self._profile_id):
            self.profiles_changed = True
            self._new_profile()
            self._reload_profiles()


__all__ = ["ManagedEmulatorsDialog"]
