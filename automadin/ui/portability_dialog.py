"""Thin, accessible UI for exporting a portable Automadin backup."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from automadin.build_info import packaged_app_version
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseRegistry,
    MaintenanceProfileScope,
    browser_profile_leases,
)
from automadin.services.browser_profile_storage import (
    ProfileStorageStatus,
    inspect_profile_storage,
)
from automadin.services.browser_profile_paths import managed_profiles_root
from automadin.services.database import DatabaseService
from automadin.services.portability_bundle import (
    BundleProgress,
    LocalProfileCollision,
    OmittedProfileCollision,
    PortabilityBundleCancelledError,
    PortabilityBundleExportResult,
    PortabilityBundleInspection,
    export_portability_bundle,
    inspect_portability_bundle,
)
from automadin.services.portability_staging import (
    PortabilityStagingResult,
    ProfileRestoreDecision,
    stage_portability_bundle_import,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)

PORTABLE_BACKUP_EXTENSION = ".automadin-bundle"

_STATUS_LABELS = {
    ProfileStorageStatus.READY: "Ready",
    ProfileStorageStatus.NEEDS_SETUP: "Needs setup",
    ProfileStorageStatus.MISSING: "Profile data missing",
    ProfileStorageStatus.CORRUPT: "Storage problem",
    ProfileStorageStatus.MISMATCH: "Identity mismatch",
    ProfileStorageStatus.INCOMPATIBLE: "Incompatible data",
}

_PHASE_LABELS = {
    "validating_bundle": "Validating portable backup",
    "validating_payloads": "Verifying every backup file",
    "classifying_local_profiles": "Comparing profiles on this PC",
    "exporting_database": "Copying database",
    "exporting_profiles": "Copying browser profiles",
    "staging_database": "Staging replacement database",
    "staging_profiles": "Staging browser profiles",
    "verifying_kept_profiles": "Verifying profiles kept on this PC",
    "publishing_activation_plan": "Preparing restart activation",
    "writing_manifest": "Writing backup manifest",
    "verifying": "Verifying portable backup",
    "complete": "Portable backup complete",
}


def _portable_destination(path: str) -> Path:
    """Return a destination with the canonical portable-backup extension."""
    value = Path(path.strip()).expanduser()
    if not str(value).casefold().endswith(PORTABLE_BACKUP_EXTENSION):
        value = Path(f"{value}{PORTABLE_BACKUP_EXTENSION}")
    return value


def _installed_app_version() -> str:
    """Return packaged project metadata without breaking source checkouts."""
    return packaged_app_version("development")


def _maintenance_cleanup_scope_from_error(
    error: BaseException,
) -> MaintenanceProfileScope | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        scope = getattr(current, "scope", None)
        if isinstance(scope, MaintenanceProfileScope):
            return scope
        current = current.__cause__ or current.__context__
    return None


def _retry_maintenance_cleanup(
    scope: MaintenanceProfileScope | None,
) -> tuple[bool, str]:
    if scope is None:
        return False, ""
    try:
        released = bool(scope.release() or getattr(scope, "released", False))  # type: ignore[attr-defined]
    except Exception as exc:
        return False, str(exc)
    return released, ""


def _retain_maintenance_cleanup_scope(scope: MaintenanceProfileScope | None) -> None:
    """Keep an uncertain live scope reachable until process restart."""
    if scope is None:
        return
    application = QApplication.instance()
    if application is None:
        return
    retained = getattr(application, "_automadin_maintenance_cleanup_scopes", None)
    if retained is None:
        retained = []
        setattr(application, "_automadin_maintenance_cleanup_scopes", retained)
    if scope not in retained:
        retained.append(scope)


class _ExportPortableBackupThread(QThread):
    """Run the service export without blocking Qt's UI thread."""

    progress_changed = Signal(str, int, int)
    succeeded = Signal(object)
    failed = Signal(str, str, object)
    cancelled = Signal(str, object)

    def __init__(
        self,
        database: DatabaseService,
        destination: Path,
        profile_ids: tuple[str, ...],
        lease_registry: BrowserProfileLeaseRegistry,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._destination = destination
        self._profile_ids = profile_ids
        self._lease_registry = lease_registry

    def _export_bundle(self) -> PortabilityBundleExportResult:
        """Keep the evolving core API behind one worker-owned call boundary."""
        return export_portability_bundle(
            self._database,
            self._destination,
            self._profile_ids,
            lease_registry=self._lease_registry,
            cancel=self.isInterruptionRequested,
            progress=self._on_progress,
            source_app_version=_installed_app_version(),
        )

    def _on_progress(self, update: BundleProgress) -> None:
        self.progress_changed.emit(
            update.phase,
            update.completed_bytes,
            update.total_bytes,
        )

    def run(self) -> None:
        try:
            result = self._export_bundle()
        except PortabilityBundleCancelledError as exc:
            cleanup_scope = _maintenance_cleanup_scope_from_error(exc)
            cleanup_released, cleanup_error = _retry_maintenance_cleanup(cleanup_scope)
            message = str(exc)
            if cleanup_scope is not None:
                message += (
                    " Browser-profile maintenance cleanup completed on retry."
                    if cleanup_released
                    else " Browser-profile maintenance cleanup remains uncertain; "
                    f"restart Automadin before continuing. {cleanup_error}"
                )
            self.cancelled.emit(message, None if cleanup_released else cleanup_scope)
        except BrowserProfileInUseError as exc:
            active = exc.current.borrowed_by or exc.current.use
            self.failed.emit(
                "Browser Profile Is In Use",
                "The portable backup could not start because a selected profile "
                f"is being used by '{active.label}'.\n\n"
                "Stop that browser activity and try again.",
                None,
            )
        except Exception as exc:
            logger.exception("Portable backup export failed")
            cleanup_scope = _maintenance_cleanup_scope_from_error(exc)
            cleanup_released, cleanup_error = _retry_maintenance_cleanup(cleanup_scope)
            cleanup_note = ""
            if cleanup_scope is not None:
                cleanup_note = (
                    " Browser-profile maintenance cleanup completed on retry."
                    if cleanup_released
                    else " Browser-profile maintenance cleanup remains uncertain; "
                    f"restart Automadin before retrying. {cleanup_error}"
                )
            self.failed.emit(
                "Portable Backup Failed",
                "The portable backup was not created. Review Diagnostics for "
                f"details, then try again.{cleanup_note}",
                None if cleanup_released else cleanup_scope,
            )
        else:
            self.succeeded.emit(result)
        finally:
            # DatabaseService connections are thread-local.  An export worker is
            # short-lived, so release its connection instead of retaining one
            # SQLite handle per export until the entire application exits.
            try:
                self._database.release_current_thread_connection()
            except Exception:
                logger.exception(
                    "Could not release portable export database connection"
                )


class ExportPortableBackupDialog(QDialog):
    """Select ready browser profiles and export a portable workspace bundle."""

    _COL_INCLUDE = 0
    _COL_PROFILE = 1
    _COL_BROWSER_PROFILE = 2
    _COL_USER = 3
    _COL_ENGINE = 4
    _COL_SIZE = 5
    _COL_STATUS = 6

    def __init__(
        self,
        db: DatabaseService,
        parent=None,
        *,
        lease_registry: BrowserProfileLeaseRegistry = browser_profile_leases,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._lease_registry = lease_registry
        self._thread: _ExportPortableBackupThread | None = None
        self._pending_outcome: tuple[str, object] | None = None
        self._export_result: PortabilityBundleExportResult | None = None
        self._maintenance_cleanup_scope: MaintenanceProfileScope | None = None

        self.setWindowTitle("Export Portable Backup")
        self.setMinimumSize(980, 650)
        self.resize(1100, 720)
        self.setAccessibleName("Export Portable Backup")
        self.setAccessibleDescription(
            "Export the Automadin database and selected ready browser profiles."
        )
        self._build_ui()
        self._populate_profiles()

    @property
    def export_result(self) -> PortabilityBundleExportResult | None:
        return self._export_result

    @property
    def export_active(self) -> bool:
        return self._thread is not None

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QLabel("Export a portable Automadin backup")
        heading.setStyleSheet("font-size: 15pt; font-weight: 700;")
        layout.addWidget(heading)

        included = QLabel(
            "The Automadin database is always included. Select any ready browser "
            "profiles whose local browser data should travel with it."
        )
        included.setWordWrap(True)
        included.setAccessibleName("Backup contents")
        layout.addWidget(included)

        warning = QLabel(
            "Sensitive browser data: selected profiles may contain cookies, tokens, "
            "history, and signed-in sessions. This backup is not encrypted or "
            "authenticated. Store it like a password. Browser protection may require "
            "you to sign in again on another Windows account or computer."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            f"color: {COLORS['warning']}; border: 1px solid {COLORS['warning']}; "
            "border-radius: 5px; padding: 9px;"
        )
        warning.setAccessibleName("Sensitive data warning")
        warning.setAccessibleDescription(warning.text())
        layout.addWidget(warning)
        self._warning_label = warning

        self._profiles_table = QTableWidget(0, 7)
        self._profiles_table.setHorizontalHeaderLabels(
            [
                "Include",
                "Profile",
                "Browser profile",
                "Signed-in user",
                "Engine",
                "Estimated size",
                "Status",
            ]
        )
        self._profiles_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._profiles_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._profiles_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._profiles_table.verticalHeader().setVisible(False)
        self._profiles_table.setAccessibleName("Browser profiles to include")
        self._profiles_table.setAccessibleDescription(
            "Ready profiles are selected by default. Profiles that are not ready "
            "cannot be included."
        )
        header = self._profiles_table.horizontalHeader()
        header.setSectionResizeMode(self._COL_INCLUDE, QHeaderView.ResizeMode.ResizeToContents)
        for column in (
            self._COL_PROFILE,
            self._COL_BROWSER_PROFILE,
            self._COL_USER,
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column in (self._COL_ENGINE, self._COL_SIZE, self._COL_STATUS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self._profiles_table.itemChanged.connect(self._update_selection_summary)
        layout.addWidget(self._profiles_table, 1)

        self._selection_summary = QLabel("")
        self._selection_summary.setAccessibleName("Selected profile count")
        layout.addWidget(self._selection_summary)

        destination_row = QHBoxLayout()
        destination_label = QLabel("Destination")
        destination_row.addWidget(destination_label)
        self._destination_edit = QLineEdit(self._default_destination())
        self._destination_edit.setAccessibleName("Portable backup destination")
        self._destination_edit.setAccessibleDescription(
            f"Destination file ending in {PORTABLE_BACKUP_EXTENSION}"
        )
        self._destination_edit.textChanged.connect(self._update_export_enabled)
        destination_label.setBuddy(self._destination_edit)
        destination_row.addWidget(self._destination_edit, 1)
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.setAccessibleName("Browse for portable backup destination")
        self._browse_btn.clicked.connect(self._browse_destination)
        destination_row.addWidget(self._browse_btn)
        layout.addLayout(destination_row)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setAccessibleName("Portable backup progress")
        layout.addWidget(self._progress)
        self._status_label = QLabel("Ready to export.")
        self._status_label.setWordWrap(True)
        self._status_label.setAccessibleName("Portable backup status")
        layout.addWidget(self._status_label)

        actions = QHBoxLayout()
        actions.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._cancel_or_close)
        actions.addWidget(self._cancel_btn)
        self._export_btn = QPushButton("Export Backup")
        self._export_btn.setProperty("cssClass", "primary")
        self._export_btn.setDefault(True)
        self._export_btn.clicked.connect(self._start_export)
        actions.addWidget(self._export_btn)
        layout.addLayout(actions)

    def _default_destination(self) -> str:
        data_dir = Path(self._db.db_path).resolve().parent
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return str(data_dir / f"automadin-portable-{stamp}{PORTABLE_BACKUP_EXTENSION}")

    def _populate_profiles(self) -> None:
        self._profiles_table.blockSignals(True)
        self._profiles_table.setRowCount(0)
        for row, record in enumerate(self._db.list_profile_records()):
            try:
                inspection = inspect_profile_storage(
                    record.profile_id,
                    record.engine or None,
                    legacy_storage_name=record.legacy_storage_name,
                )
                ready = inspection.status is ProfileStorageStatus.READY
                status = _STATUS_LABELS.get(inspection.status, inspection.status.value)
                detail = inspection.detail
            except Exception:
                logger.exception(
                    "Could not inspect browser profile %s for portable export",
                    record.profile_id,
                )
                ready = False
                status = "Inspection failed"
                detail = "Review Diagnostics for details."

            self._profiles_table.insertRow(row)
            include_item = QTableWidgetItem("Include" if ready else "Unavailable")
            include_item.setData(Qt.ItemDataRole.UserRole, record.profile_id)
            include_item.setToolTip(
                "Selected for export."
                if ready
                else f"This profile cannot be exported. {detail or status}"
            )
            if ready:
                include_item.setFlags(
                    include_item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                )
                include_item.setCheckState(Qt.CheckState.Checked)
            else:
                include_item.setFlags(
                    include_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable
                )
                include_item.setCheckState(Qt.CheckState.Unchecked)
            self._profiles_table.setItem(row, self._COL_INCLUDE, include_item)

            identity = record.identity
            user = (
                f"{identity.account_name} ({identity.account_email})"
                if identity.account_name and identity.account_email
                else identity.account_name or identity.account_email or "Not detected"
            )
            values = (
                record.name,
                identity.source_profile or "Not detected",
                user,
                record.engine or identity.browser or "Not detected",
                "Calculated during export" if ready else "Unavailable",
                status,
            )
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setToolTip(detail if column == self._COL_STATUS and detail else value)
                self._profiles_table.setItem(row, column, item)
        self._profiles_table.blockSignals(False)
        self._update_selection_summary()

    def _selected_profile_ids(self) -> tuple[str, ...]:
        selected: list[str] = []
        for row in range(self._profiles_table.rowCount()):
            item = self._profiles_table.item(row, self._COL_INCLUDE)
            if (
                item is not None
                and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                and item.checkState() == Qt.CheckState.Checked
            ):
                selected.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return tuple(selected)

    def _update_selection_summary(self, _item: QTableWidgetItem | None = None) -> None:
        selected = len(self._selected_profile_ids())
        self._selection_summary.setText(
            f"Database included • {selected} browser "
            f"{'profile' if selected == 1 else 'profiles'} selected"
        )

    def _browse_destination(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Portable Backup",
            self._destination_edit.text(),
            f"Automadin Portable Backup (*{PORTABLE_BACKUP_EXTENSION});;All Files (*)",
        )
        if path:
            self._destination_edit.setText(str(_portable_destination(path)))

    def _update_export_enabled(self) -> None:
        self._export_btn.setEnabled(
            self._thread is None and bool(self._destination_edit.text().strip())
        )

    def _set_exporting(self, exporting: bool) -> None:
        self._profiles_table.setEnabled(not exporting)
        self._destination_edit.setEnabled(not exporting)
        self._browse_btn.setEnabled(not exporting)
        self._export_btn.setEnabled(not exporting)
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setText("Cancel Export" if exporting else "Close")
        self._progress.setVisible(exporting)
        if exporting:
            self._progress.setRange(0, 0)
        else:
            self._update_export_enabled()

    def _start_export(self) -> None:
        if self._thread is not None:
            return
        raw_destination = self._destination_edit.text().strip()
        if not raw_destination:
            QMessageBox.warning(
                self,
                "Destination Required",
                "Choose where to save the portable backup.",
            )
            return
        destination = _portable_destination(raw_destination)
        self._destination_edit.setText(str(destination))
        if os.path.lexists(destination):
            QMessageBox.warning(
                self,
                "Destination Already Exists",
                "Choose a new file name. Automadin will not overwrite an existing "
                f"portable backup.\n\n{destination}",
            )
            return

        thread = _ExportPortableBackupThread(
            self._db,
            destination,
            self._selected_profile_ids(),
            self._lease_registry,
            self,
        )
        self._thread = thread
        self._pending_outcome = None
        thread.progress_changed.connect(self._on_progress)
        thread.succeeded.connect(self._on_export_succeeded)
        thread.failed.connect(self._on_export_failed)
        thread.cancelled.connect(self._on_export_cancelled)
        thread.finished.connect(lambda: self._on_thread_finished(thread))
        self._set_exporting(True)
        self._status_label.setText("Preparing portable backup…")
        try:
            thread.start()
        except BaseException:
            logger.exception("Could not start portable backup export worker")
            startup_uncertain = qthread_start_may_have_succeeded(thread)
            if startup_uncertain:
                self._status_label.setText(
                    "The export started but its startup could not be confirmed. "
                    "Wait for it to finish before closing this window."
                )
            else:
                self._thread = None
                self._pending_outcome = None
                self._set_exporting(False)
                try:
                    thread.deleteLater()
                except Exception:
                    logger.exception("Could not release failed export worker")
            QMessageBox.critical(
                self,
                (
                    "Portable Backup Startup Uncertain"
                    if startup_uncertain
                    else "Portable Backup Could Not Start"
                ),
                (
                    "The export may have begun, but startup could not be confirmed. "
                    "Wait for its final result before trying again."
                    if startup_uncertain
                    else "The portable backup worker could not be started. Review "
                    "Diagnostics for details, then try again."
                ),
            )

    def _on_progress(self, phase: str, completed: int, total: int) -> None:
        label = _PHASE_LABELS.get(phase, phase.replace("_", " ").capitalize())
        if total > 0:
            self._progress.setRange(0, 1000)
            self._progress.setValue(min(1000, int(completed * 1000 / total)))
            self._progress.setFormat(f"{label} — %p%")
        else:
            self._progress.setRange(0, 0)
            self._progress.setFormat(label)
        self._status_label.setText(label)

    def _on_export_succeeded(self, result: PortabilityBundleExportResult) -> None:
        self._pending_outcome = ("success", result)
        self._status_label.setText("Finalizing portable backup…")

    def _show_export_succeeded(self, result: PortabilityBundleExportResult) -> None:
        self._export_result = result
        self._progress.setVisible(True)
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._progress.setFormat("Complete")
        self._status_label.setText(f"Portable backup created at {result.path}")
        cleanup_scope = getattr(result, "maintenance_cleanup_scope", None)
        cleanup_error = getattr(result, "cleanup_error", None)
        if cleanup_error or cleanup_scope is not None:
            released, retry_error = _retry_maintenance_cleanup(cleanup_scope)
            self._maintenance_cleanup_scope = None if released else cleanup_scope
            if not released:
                _retain_maintenance_cleanup_scope(cleanup_scope)
            title = (
                "Portable Backup Created; Cleanup Completed"
                if released
                else "Portable Backup Created; Restart Required"
            )
            cleanup_note = (
                "Delayed browser-profile maintenance cleanup completed on retry."
                if released
                else "Restart Automadin before using the exported browser profiles."
            )
            details = retry_error or cleanup_error or ""
            QMessageBox.warning(
                self,
                title,
                f"Portable backup created successfully:\n{result.path}\n\n"
                f"{cleanup_note}\n\n{details}\n\nKeep this unencrypted file secure.",
            )
            return
        QMessageBox.information(
            self,
            "Portable Backup Created",
            f"Portable backup created successfully:\n{result.path}\n\n"
            "Keep this unencrypted file secure.",
        )

    def _on_export_failed(
        self,
        title: str,
        message: str,
        cleanup_scope: MaintenanceProfileScope | None,
    ) -> None:
        self._pending_outcome = ("failure", (title, message, cleanup_scope))
        self._status_label.setText("Cleaning up the unsuccessful export…")

    def _show_export_failed(
        self,
        title: str,
        message: str,
        cleanup_scope: MaintenanceProfileScope | None = None,
    ) -> None:
        self._maintenance_cleanup_scope = cleanup_scope
        _retain_maintenance_cleanup_scope(cleanup_scope)
        self._progress.setVisible(False)
        self._status_label.setText(message)
        QMessageBox.critical(self, title, message)

    def _on_export_cancelled(
        self,
        message: str,
        cleanup_scope: MaintenanceProfileScope | None = None,
    ) -> None:
        self._pending_outcome = (
            "cancelled",
            (message or "Portable backup export was cancelled.", cleanup_scope),
        )
        self._status_label.setText("Cleaning up the cancelled export…")

    def _show_export_cancelled(
        self,
        message: str,
        cleanup_scope: MaintenanceProfileScope | None = None,
    ) -> None:
        self._maintenance_cleanup_scope = cleanup_scope
        _retain_maintenance_cleanup_scope(cleanup_scope)
        self._progress.setVisible(False)
        self._status_label.setText(message)

    def _on_thread_finished(self, thread: _ExportPortableBackupThread) -> None:
        if self._thread is not thread:
            return
        # Never delete or drop the only reference to a worker Qt still reports
        # as live, even if a future implementation emits finished unusually.
        if thread.isRunning():
            return

        outcome = self._pending_outcome
        self._pending_outcome = None
        self._thread = None
        try:
            thread.deleteLater()
        except Exception:
            logger.exception("Could not release completed export worker")
        self._set_exporting(False)

        if outcome is None:
            self._show_export_failed(
                "Portable Backup Failed",
                "The export worker stopped without reporting a result. No completed "
                "portable backup was confirmed.",
            )
            return
        kind, payload = outcome
        if kind == "success":
            self._show_export_succeeded(payload)  # type: ignore[arg-type]
        elif kind == "failure":
            title, message, cleanup_scope = payload  # type: ignore[misc]
            self._show_export_failed(str(title), str(message), cleanup_scope)
        else:
            message, cleanup_scope = payload  # type: ignore[misc]
            self._show_export_cancelled(str(message), cleanup_scope)

    def _cancel_or_close(self) -> None:
        thread = self._thread
        if thread is None:
            self.reject()
            return
        thread.requestInterruption()
        self._cancel_btn.setEnabled(False)
        self._status_label.setText(
            "Cancellation requested. Finishing the current safe copy step…"
        )

    def reject(self) -> None:
        if self._thread is not None:
            self._cancel_or_close()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            self._thread.requestInterruption()
            self._cancel_btn.setEnabled(False)
            self._status_label.setText(
                "Export is still active. Cancellation was requested; this window "
                "will remain open until cleanup finishes."
            )
            event.ignore()
            return
        super().closeEvent(event)


# Concise alias for callers that group portability dialogs together.
PortabilityExportDialog = ExportPortableBackupDialog


class _InspectPortableBackupThread(QThread):
    """Fully verify an untrusted bundle away from the GUI thread."""

    progress_changed = Signal(str, int, int)
    succeeded = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal(str)

    def __init__(self, source: Path, parent: Any = None) -> None:
        super().__init__(parent)
        self._source = source

    def _inspect_bundle(self) -> PortabilityBundleInspection:
        return inspect_portability_bundle(
            self._source,
            profiles_root=managed_profiles_root(),
            progress=self._on_progress,
            cancel=self.isInterruptionRequested,
            classify_local_profiles=True,
        )

    def _on_progress(self, update: BundleProgress) -> None:
        self.progress_changed.emit(
            update.phase,
            update.completed_bytes,
            update.total_bytes,
        )

    def run(self) -> None:
        try:
            inspection = self._inspect_bundle()
        except PortabilityBundleCancelledError as exc:
            self.cancelled.emit(str(exc))
        except Exception:
            logger.exception("Portable backup inspection failed")
            self.failed.emit(
                "Portable Backup Is Not Valid",
                "Automadin could not safely inspect this portable backup. Nothing "
                "was staged or changed. Review Diagnostics for details.",
            )
        else:
            self.succeeded.emit(inspection)


class _StagePortableBackupThread(QThread):
    """Stage one fully inspected bundle for restart activation."""

    progress_changed = Signal(str, int, int)
    succeeded = Signal(object)
    failed = Signal(str, str, object)
    cancelled = Signal(str, object)

    def __init__(
        self,
        database: DatabaseService,
        source: Path,
        inspection: PortabilityBundleInspection,
        decisions: dict[str, ProfileRestoreDecision],
        lease_registry: BrowserProfileLeaseRegistry,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._source = source
        self._inspection = inspection
        self._decisions = decisions
        self._lease_registry = lease_registry

    def _stage_bundle(self) -> PortabilityStagingResult:
        return stage_portability_bundle_import(
            self._source,
            self._inspection,
            self._database.db_path,
            self._decisions,
            profiles_root=managed_profiles_root(),
            lease_registry=self._lease_registry,
            cancel=self.isInterruptionRequested,
            progress=self._on_progress,
        )

    def _on_progress(self, update: BundleProgress) -> None:
        self.progress_changed.emit(
            update.phase,
            update.completed_bytes,
            update.total_bytes,
        )

    def run(self) -> None:
        try:
            result = self._stage_bundle()
        except PortabilityBundleCancelledError as exc:
            cleanup_scope = _maintenance_cleanup_scope_from_error(exc)
            cleanup_released, cleanup_error = _retry_maintenance_cleanup(cleanup_scope)
            message = str(exc)
            if cleanup_scope is not None:
                message += (
                    " Browser-profile maintenance cleanup completed on retry."
                    if cleanup_released
                    else " Browser-profile maintenance cleanup remains uncertain; "
                    f"restart Automadin before continuing. {cleanup_error}"
                )
            self.cancelled.emit(message, None if cleanup_released else cleanup_scope)
        except BrowserProfileInUseError as exc:
            active = exc.current.borrowed_by or exc.current.use
            self.failed.emit(
                "Browser Profile Is In Use",
                "The portable backup cannot be staged because a profile is being "
                f"used by '{active.label}'.\n\nStop that browser activity and try again.",
                None,
            )
        except Exception as exc:
            logger.exception("Portable backup staging failed")
            cleanup_scope = _maintenance_cleanup_scope_from_error(exc)
            cleanup_released, cleanup_error = _retry_maintenance_cleanup(cleanup_scope)
            cleanup_note = ""
            if cleanup_scope is not None:
                cleanup_note = (
                    " Browser-profile maintenance cleanup completed on retry."
                    if cleanup_released
                    else " Browser-profile maintenance cleanup remains uncertain; "
                    f"restart Automadin before retrying. {cleanup_error}"
                )
            self.failed.emit(
                "Portable Backup Staging Failed",
                "Nothing was activated. Review Diagnostics for details before "
                f"trying again.{cleanup_note}",
                None if cleanup_released else cleanup_scope,
            )
        else:
            self.succeeded.emit(result)


class ImportPortableBackupDialog(QDialog):
    """Inspect a portable backup and stage an explicit safe restore plan."""

    _COL_PROFILE = 0
    _COL_ENGINE = 1
    _COL_BUNDLE = 2
    _COL_THIS_PC = 3
    _COL_ACTION = 4

    def __init__(
        self,
        db: DatabaseService,
        parent=None,
        *,
        lease_registry: BrowserProfileLeaseRegistry = browser_profile_leases,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._lease_registry = lease_registry
        self._thread: QThread | None = None
        self._pending_outcome: tuple[str, object] | None = None
        self._inspection: PortabilityBundleInspection | None = None
        self._inspected_source: Path | None = None
        self._staged_result: PortabilityStagingResult | None = None
        self._maintenance_cleanup_scope: MaintenanceProfileScope | None = None
        self._different_acknowledgements: dict[str, QCheckBox] = {}
        self._omitted_acknowledgements: dict[str, QCheckBox] = {}
        self._preview_blocked = True

        self.setWindowTitle("Import Portable Backup")
        self.setMinimumSize(1000, 680)
        self.resize(1120, 760)
        self.setAccessibleName("Import Portable Backup")
        self.setAccessibleDescription(
            "Fully verify and stage a portable Automadin backup for restart."
        )
        self._build_ui()

    @property
    def staged_result(self) -> PortabilityStagingResult | None:
        return self._staged_result

    @property
    def source(self) -> Path | None:
        if self._staged_result is not None:
            return self._staged_result.source
        return self._inspected_source

    @property
    def source_path(self) -> Path | None:
        return self.source

    @property
    def import_active(self) -> bool:
        return self._thread is not None

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QLabel("Import a portable Automadin backup")
        heading.setStyleSheet("font-size: 15pt; font-weight: 700;")
        layout.addWidget(heading)

        consequence = QLabel(
            "This is a full restore. The entire Automadin database—including flows, "
            "images, run setups, settings, and profile records—will replace the "
            "current database the next time Automadin starts. A safety backup of "
            "the current database is created automatically."
        )
        consequence.setWordWrap(True)
        consequence.setAccessibleName("Database replacement warning")
        consequence.setAccessibleDescription(consequence.text())
        layout.addWidget(consequence)
        self._consequence_label = consequence

        warning = QLabel(
            "Sensitive browser data: portable profiles may contain cookies, tokens, "
            "history, and signed-in sessions. The backup is not encrypted or "
            "authenticated; checksums detect damage, not who created it. Browser "
            "protection may require you to sign in again on this Windows account or "
            "computer."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            f"color: {COLORS['warning']}; border: 1px solid {COLORS['warning']}; "
            "border-radius: 5px; padding: 9px;"
        )
        warning.setAccessibleName("Sensitive data warning")
        warning.setAccessibleDescription(warning.text())
        layout.addWidget(warning)
        self._warning_label = warning

        source_row = QHBoxLayout()
        source_label = QLabel("Backup file")
        source_row.addWidget(source_label)
        self._source_edit = QLineEdit()
        self._source_edit.setPlaceholderText(
            f"Choose a *{PORTABLE_BACKUP_EXTENSION} file"
        )
        self._source_edit.setAccessibleName("Portable backup file")
        self._source_edit.setAccessibleDescription(
            "The portable backup that will be fully verified before staging."
        )
        self._source_edit.textChanged.connect(self._on_source_changed)
        source_label.setBuddy(self._source_edit)
        source_row.addWidget(self._source_edit, 1)
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.setAccessibleName("Browse for portable backup")
        self._browse_btn.clicked.connect(self._browse_source)
        source_row.addWidget(self._browse_btn)
        self._inspect_btn = QPushButton("Review Backup")
        self._inspect_btn.setProperty("cssClass", "primary")
        self._inspect_btn.clicked.connect(self._start_inspection)
        self._inspect_btn.setEnabled(False)
        source_row.addWidget(self._inspect_btn)
        layout.addLayout(source_row)

        self._database_summary = QLabel(
            "Choose a backup and select Review Backup. No files will be changed "
            "during review."
        )
        self._database_summary.setWordWrap(True)
        self._database_summary.setAccessibleName("Portable backup database summary")
        layout.addWidget(self._database_summary)

        self._profiles_table = QTableWidget(0, 5)
        self._profiles_table.setHorizontalHeaderLabels(
            ["Profile", "Engine", "In backup", "On this PC", "Restore action"]
        )
        self._profiles_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._profiles_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._profiles_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._profiles_table.verticalHeader().setVisible(False)
        self._profiles_table.setAccessibleName("Portable backup profile preview")
        self._profiles_table.setAccessibleDescription(
            "Shows included and omitted profiles, local collision status, and the "
            "action that will occur at restart."
        )
        header = self._profiles_table.horizontalHeader()
        header.setSectionResizeMode(self._COL_PROFILE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_ENGINE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_BUNDLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_THIS_PC, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_ACTION, QHeaderView.ResizeMode.Stretch)
        self._profiles_table.setVisible(False)
        layout.addWidget(self._profiles_table, 1)

        self._decision_note = QLabel("")
        self._decision_note.setWordWrap(True)
        self._decision_note.setAccessibleName("Profile restore decision status")
        layout.addWidget(self._decision_note)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setAccessibleName("Portable backup import progress")
        layout.addWidget(self._progress)
        self._status_label = QLabel("Ready to review a portable backup.")
        self._status_label.setWordWrap(True)
        self._status_label.setAccessibleName("Portable backup import status")
        layout.addWidget(self._status_label)

        actions = QHBoxLayout()
        actions.addStretch()
        self._cancel_btn = QPushButton("Close")
        self._cancel_btn.clicked.connect(self._cancel_or_close)
        actions.addWidget(self._cancel_btn)
        self._stage_btn = QPushButton("Stage Restore for Restart")
        self._stage_btn.setProperty("cssClass", "primary")
        self._stage_btn.clicked.connect(self._start_staging)
        self._stage_btn.setEnabled(False)
        actions.addWidget(self._stage_btn)
        layout.addLayout(actions)

    def _browse_source(self) -> None:
        start = self._source_edit.text().strip() or str(Path(self._db.db_path).parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Portable Backup",
            start,
            f"Automadin Portable Backup (*{PORTABLE_BACKUP_EXTENSION});;All Files (*)",
        )
        if path:
            self._source_edit.setText(path)

    def _on_source_changed(self) -> None:
        if self._thread is not None:
            return
        self._inspection = None
        self._inspected_source = None
        self._staged_result = None
        self._profiles_table.clearContents()
        self._profiles_table.setRowCount(0)
        self._profiles_table.setVisible(False)
        self._different_acknowledgements.clear()
        self._omitted_acknowledgements.clear()
        self._preview_blocked = True
        self._stage_btn.setEnabled(False)
        self._database_summary.setText(
            "Select Review Backup to verify every file and preview the restore."
        )
        self._decision_note.clear()
        self._inspect_btn.setEnabled(bool(self._source_edit.text().strip()))

    def _set_busy(self, busy: bool) -> None:
        self._source_edit.setEnabled(not busy)
        self._browse_btn.setEnabled(not busy)
        self._inspect_btn.setEnabled(
            not busy and bool(self._source_edit.text().strip())
        )
        self._profiles_table.setEnabled(not busy)
        self._stage_btn.setEnabled(not busy and self._decisions_ready())
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setText("Cancel" if busy else "Close")
        self._progress.setVisible(busy)
        if busy:
            self._progress.setRange(0, 0)

    def _start_inspection(self) -> None:
        if self._thread is not None:
            return
        raw = self._source_edit.text().strip()
        if not raw:
            QMessageBox.warning(
                self,
                "Backup Required",
                "Choose an Automadin portable backup to review.",
            )
            return
        source = Path(raw).expanduser()
        # A new integrity pass invalidates the prior preview immediately. Do
        # not allow an older inspection to become stageable again if review is
        # cancelled or the archive changed in place.
        self._inspection = None
        self._inspected_source = None
        self._profiles_table.setVisible(False)
        self._different_acknowledgements.clear()
        self._omitted_acknowledgements.clear()
        self._preview_blocked = True
        self._stage_btn.setEnabled(False)
        self._decision_note.clear()
        thread = _InspectPortableBackupThread(source, self)
        self._start_worker(thread)
        self._status_label.setText(
            "Fully verifying the portable backup. Nothing is being changed…"
        )

    def _start_worker(self, thread: QThread) -> None:
        self._thread = thread
        self._pending_outcome = None
        thread.progress_changed.connect(self._on_progress)  # type: ignore[attr-defined]
        thread.succeeded.connect(self._on_worker_succeeded)  # type: ignore[attr-defined]
        thread.failed.connect(self._on_worker_failed)  # type: ignore[attr-defined]
        thread.cancelled.connect(self._on_worker_cancelled)  # type: ignore[attr-defined]
        thread.finished.connect(lambda: self._on_thread_finished(thread))
        self._set_busy(True)
        try:
            thread.start()
        except BaseException:
            logger.exception("Could not start portable backup import worker")
            startup_uncertain = qthread_start_may_have_succeeded(thread)
            if startup_uncertain:
                self._status_label.setText(
                    "The operation started but its startup could not be confirmed. "
                    "Wait for it to finish before closing this window."
                )
            else:
                self._thread = None
                self._pending_outcome = None
                self._set_busy(False)
                try:
                    thread.deleteLater()
                except Exception:
                    logger.exception("Could not release failed import worker")
            QMessageBox.critical(
                self,
                (
                    "Portable Backup Startup Uncertain"
                    if startup_uncertain
                    else "Portable Backup Operation Could Not Start"
                ),
                (
                    "The operation may have begun, but startup could not be "
                    "confirmed. Wait for its final result before trying again."
                    if startup_uncertain
                    else "The portable backup worker could not be started. Review "
                    "Diagnostics for details, then try again."
                ),
            )

    def _on_progress(self, phase: str, completed: int, total: int) -> None:
        label = _PHASE_LABELS.get(phase, phase.replace("_", " ").capitalize())
        if total > 0:
            self._progress.setRange(0, 1000)
            self._progress.setValue(min(1000, int(completed * 1000 / total)))
            self._progress.setFormat(f"{label} — %p%")
        else:
            self._progress.setRange(0, 0)
            self._progress.setFormat(label)
        self._status_label.setText(label)

    def _on_worker_succeeded(self, result: object) -> None:
        kind = (
            "inspect_success"
            if isinstance(result, PortabilityBundleInspection)
            else "stage_success"
        )
        self._pending_outcome = (kind, result)
        self._status_label.setText("Finalizing the completed operation…")

    def _on_worker_failed(
        self,
        title: str,
        message: str,
        cleanup_scope: MaintenanceProfileScope | None = None,
    ) -> None:
        self._pending_outcome = ("failure", (title, message, cleanup_scope))
        self._status_label.setText("Cleaning up the unsuccessful operation…")

    def _on_worker_cancelled(
        self,
        message: str,
        cleanup_scope: MaintenanceProfileScope | None = None,
    ) -> None:
        self._pending_outcome = (
            "cancelled",
            (message or "Portable backup operation was cancelled.", cleanup_scope),
        )
        self._status_label.setText("Cleaning up the cancelled operation…")

    def _on_thread_finished(self, thread: QThread) -> None:
        if self._thread is not thread:
            return
        if thread.isRunning():
            return
        outcome = self._pending_outcome
        self._pending_outcome = None
        self._thread = None
        try:
            thread.deleteLater()
        except Exception:
            logger.exception("Could not release completed import worker")
        self._set_busy(False)

        if outcome is None:
            self._show_failure(
                "Portable Backup Operation Failed",
                "The worker stopped without reporting a result. No staged restore "
                "was confirmed.",
            )
            return
        kind, payload = outcome
        if kind == "inspect_success":
            self._show_inspection(payload)  # type: ignore[arg-type]
        elif kind == "stage_success":
            self._show_staged(payload)  # type: ignore[arg-type]
        elif kind == "failure":
            title, message, cleanup_scope = payload  # type: ignore[misc]
            self._show_failure(str(title), str(message), cleanup_scope)
        else:
            message, cleanup_scope = payload  # type: ignore[misc]
            self._maintenance_cleanup_scope = cleanup_scope
            _retain_maintenance_cleanup_scope(cleanup_scope)
            self._progress.setVisible(False)
            self._status_label.setText(str(message))

    def _show_failure(
        self,
        title: str,
        message: str,
        cleanup_scope: MaintenanceProfileScope | None = None,
    ) -> None:
        self._maintenance_cleanup_scope = cleanup_scope
        _retain_maintenance_cleanup_scope(cleanup_scope)
        self._progress.setVisible(False)
        self._status_label.setText(message)
        QMessageBox.critical(self, title, message)

    def _show_inspection(self, inspection: PortabilityBundleInspection) -> None:
        self._inspection = inspection
        self._inspected_source = inspection.path
        database = inspection.database
        included_count = len(inspection.manifest.profiles)
        omitted_count = len(inspection.manifest.omitted_profiles)
        self._database_summary.setText(
            f"Backup created: {inspection.manifest.created_at} • "
            f"Automadin {inspection.manifest.source_app_version}\n"
            f"Verified database: {database.flow_count} flows • "
            f"{database.image_count} images • {database.profile_count} profile "
            f"records\nPortable profile data: {included_count} included • "
            f"{omitted_count} omitted"
        )
        self._populate_import_preview(inspection)
        self._profiles_table.setVisible(True)
        self._progress.setVisible(False)
        self._status_label.setText(
            "Backup verified. Review every profile action before staging."
        )
        self._update_decision_state()

    def _populate_import_preview(
        self,
        inspection: PortabilityBundleInspection,
    ) -> None:
        self._profiles_table.clearContents()
        self._profiles_table.setRowCount(0)
        self._different_acknowledgements.clear()
        self._omitted_acknowledgements.clear()
        collisions = {item.profile_id: item for item in inspection.collisions}
        omitted_collisions = {
            item.profile_id: item for item in inspection.omitted_collisions
        }
        omitted = {
            item.profile_id: item.reason for item in inspection.manifest.omitted_profiles
        }
        self._preview_blocked = False

        for row, profile in enumerate(inspection.database_profiles):
            self._profiles_table.insertRow(row)
            included = profile.included
            bundle_text = "Included" if included else "Omitted"
            local_text = "Not evaluated"
            action_text = "Database record only"
            detail = omitted.get(profile.profile_id, "")
            collision = collisions.get(profile.profile_id) if included else None
            omitted_collision = (
                omitted_collisions.get(profile.profile_id) if not included else None
            )

            if included and collision is None:
                local_text = "Collision result missing"
                action_text = "Blocked"
                self._preview_blocked = True
            elif collision is not None:
                detail = collision.detail
                if collision.outcome is LocalProfileCollision.ABSENT:
                    local_text = "Not on this PC"
                    action_text = "Install from backup"
                elif collision.outcome is LocalProfileCollision.IDENTICAL:
                    local_text = "Same profile data"
                    action_text = "Keep this PC's profile"
                elif collision.outcome is LocalProfileCollision.DIFFERENT:
                    local_text = "Different local browser data"
                    action_text = ""
                elif collision.outcome is LocalProfileCollision.ENGINE_MISMATCH:
                    local_text = "Engine mismatch"
                    action_text = "Blocked"
                    self._preview_blocked = True
                else:
                    local_text = "Unsafe or unreadable local profile"
                    action_text = "Blocked"
                    self._preview_blocked = True
            elif not included and omitted_collision is None:
                local_text = "Omitted profile status missing"
                action_text = "Blocked"
                self._preview_blocked = True
            elif omitted_collision is not None:
                detail = omitted_collision.detail or detail
                if omitted_collision.outcome is OmittedProfileCollision.ABSENT:
                    local_text = "Not on this PC"
                    action_text = "No browser data to restore"
                elif omitted_collision.outcome is OmittedProfileCollision.READY:
                    local_text = "Ready profile data exists"
                    action_text = ""
                elif (
                    omitted_collision.outcome
                    is OmittedProfileCollision.ENGINE_MISMATCH
                ):
                    local_text = "Engine mismatch"
                    action_text = "Blocked"
                    self._preview_blocked = True
                else:
                    local_text = "Unsafe or unreadable local profile"
                    action_text = "Blocked"
                    self._preview_blocked = True

            values = (
                profile.display_name,
                profile.engine or "Not recorded",
                bundle_text,
                local_text,
                action_text,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(detail or value)
                self._profiles_table.setItem(row, column, item)

            if (
                collision is not None
                and collision.outcome is LocalProfileCollision.DIFFERENT
            ):
                acknowledgement = QCheckBox("Keep this PC's existing data")
                acknowledgement.setAccessibleName(
                    f"Keep existing local data for {profile.display_name}"
                )
                acknowledgement.setAccessibleDescription(
                    "Required acknowledgement: bundled browser bytes will not replace "
                    "the different profile data already on this PC."
                )
                acknowledgement.stateChanged.connect(self._update_decision_state)
                self._profiles_table.setCellWidget(
                    row,
                    self._COL_ACTION,
                    acknowledgement,
                )
                self._different_acknowledgements[profile.profile_id] = acknowledgement
            elif (
                omitted_collision is not None
                and omitted_collision.outcome is OmittedProfileCollision.READY
            ):
                acknowledgement = QCheckBox("Keep this PC's existing profile")
                acknowledgement.setAccessibleName(
                    f"Keep omitted local profile {profile.display_name}"
                )
                acknowledgement.setAccessibleDescription(
                    "Required acknowledgement: this profile was omitted from the "
                    "backup, so its ready browser data on this PC will be kept."
                )
                acknowledgement.stateChanged.connect(self._update_decision_state)
                self._profiles_table.setCellWidget(
                    row,
                    self._COL_ACTION,
                    acknowledgement,
                )
                self._omitted_acknowledgements[profile.profile_id] = acknowledgement

    def _restore_decisions(self) -> dict[str, ProfileRestoreDecision] | None:
        inspection = self._inspection
        if inspection is None or self._preview_blocked:
            return None
        decisions: dict[str, ProfileRestoreDecision] = {}
        for collision in inspection.collisions:
            if collision.outcome is LocalProfileCollision.ABSENT:
                decisions[collision.profile_id] = ProfileRestoreDecision.INSTALL
            elif collision.outcome is LocalProfileCollision.IDENTICAL:
                decisions[collision.profile_id] = ProfileRestoreDecision.KEEP_LOCAL
            elif collision.outcome is LocalProfileCollision.DIFFERENT:
                acknowledgement = self._different_acknowledgements.get(
                    collision.profile_id
                )
                if acknowledgement is None or not acknowledgement.isChecked():
                    return None
                decisions[collision.profile_id] = ProfileRestoreDecision.KEEP_LOCAL
            else:
                return None
        if len(decisions) < len(inspection.manifest.profiles):
            return None
        for collision in inspection.omitted_collisions:
            if collision.outcome is OmittedProfileCollision.ABSENT:
                continue
            if collision.outcome is OmittedProfileCollision.READY:
                acknowledgement = self._omitted_acknowledgements.get(
                    collision.profile_id
                )
                if acknowledgement is None or not acknowledgement.isChecked():
                    return None
                decisions[collision.profile_id] = ProfileRestoreDecision.KEEP_LOCAL
                continue
            return None
        return decisions

    def _decisions_ready(self) -> bool:
        return self._restore_decisions() is not None

    def _update_decision_state(self, _state: int = 0) -> None:
        ready = self._decisions_ready()
        self._stage_btn.setEnabled(self._thread is None and ready)
        if self._preview_blocked:
            self._decision_note.setText(
                "Restore is blocked: at least one local profile has an engine "
                "mismatch or unsafe/unreadable storage. Resolve it in Browser "
                "Profiles, then review the backup again."
            )
        elif not ready:
            self._decision_note.setText(
                "Different or omitted local browser data was found. Explicitly "
                "confirm every profile that this PC should keep before staging."
            )
        else:
            self._decision_note.setText(
                "All profile actions are resolved. Staging will not activate "
                "anything until Automadin restarts."
            )

    def _start_staging(self) -> None:
        if self._thread is not None:
            return
        inspection = self._inspection
        source = self._inspected_source
        decisions = self._restore_decisions()
        if inspection is None or source is None or decisions is None:
            QMessageBox.warning(
                self,
                "Restore Decisions Required",
                "Review the backup and resolve every profile action before staging.",
            )
            return
        if not self._confirm_staging(inspection, decisions):
            self._status_label.setText(
                "Restore staging was not started. The current workspace is unchanged."
            )
            return
        thread = _StagePortableBackupThread(
            self._db,
            source,
            inspection,
            decisions,
            self._lease_registry,
            self,
        )
        self._start_worker(thread)
        self._status_label.setText(
            "Staging the verified restore. The current workspace remains active "
            "until restart…"
        )

    @staticmethod
    def _staging_confirmation_details(
        inspection: PortabilityBundleInspection,
        decisions: dict[str, ProfileRestoreDecision],
    ) -> str:
        labels = {
            profile.profile_id: profile.display_name
            for profile in inspection.database_profiles
        }
        omitted = [
            labels[profile.profile_id]
            for profile in inspection.database_profiles
            if not profile.included
        ]
        kept = [
            labels.get(profile_id, profile_id)
            for profile_id, decision in decisions.items()
            if decision is ProfileRestoreDecision.KEEP_LOCAL
        ]

        def render(title: str, values: list[str]) -> str:
            entries = "\n".join(f"• {value}" for value in values) or "• None"
            return f"{title}\n{entries}"

        return (
            render("Profiles omitted from portable browser data:", omitted)
            + "\n\n"
            + render("Existing browser profiles kept on this PC:", kept)
        )

    def _confirm_staging(
        self,
        inspection: PortabilityBundleInspection,
        decisions: dict[str, ProfileRestoreDecision],
    ) -> bool:
        omitted_count = sum(
            not profile.included for profile in inspection.database_profiles
        )
        kept_count = sum(
            decision is ProfileRestoreDecision.KEEP_LOCAL
            for decision in decisions.values()
        )
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Confirm Full Portable Restore")
        message.setText("Stage this full Automadin restore for restart?")
        message.setInformativeText(
            "The entire Automadin database will be replaced at the next start. "
            "A safety backup of the current database will be created automatically.\n\n"
            f"Profiles omitted from portable browser data: {omitted_count}\n"
            f"Existing local browser profiles kept: {kept_count}"
        )
        message.setDetailedText(
            self._staging_confirmation_details(inspection, decisions)
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        message.setDefaultButton(QMessageBox.StandardButton.No)
        no_button = message.button(QMessageBox.StandardButton.No)
        if no_button is not None:
            message.setEscapeButton(no_button)
        return message.exec() == QMessageBox.StandardButton.Yes

    def _show_staged(self, result: PortabilityStagingResult) -> None:
        self._staged_result = result
        self._progress.setVisible(True)
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._progress.setFormat("Ready for restart")
        self._status_label.setText(
            "Portable restore staged safely. Restart Automadin to activate it."
        )
        cleanup_note = ""
        if result.cleanup_paths:
            cleanup_note = (
                "\n\nThe restore is staged, but temporary cleanup data remains at:\n"
                + "\n".join(str(path) for path in result.cleanup_paths)
            )
        cleanup_scope = getattr(result, "maintenance_cleanup_scope", None)
        cleanup_error = getattr(result, "cleanup_error", None)
        if cleanup_error or cleanup_scope is not None:
            released, retry_error = _retry_maintenance_cleanup(cleanup_scope)
            self._maintenance_cleanup_scope = None if released else cleanup_scope
            if not released:
                _retain_maintenance_cleanup_scope(cleanup_scope)
            title = (
                "Portable Restore Ready; Cleanup Completed"
                if released
                else "Portable Restore Ready; Restart Required"
            )
            scope_note = (
                "Delayed browser-profile maintenance cleanup completed on retry."
                if released
                else "Maintenance cleanup remains uncertain; restart Automadin now."
            )
            details = retry_error or cleanup_error or ""
            QMessageBox.warning(
                self,
                title,
                "The portable backup is staged and will replace the Automadin database "
                "the next time the app starts.\n\n"
                f"{scope_note}\n\n{details}{cleanup_note}",
            )
        else:
            QMessageBox.information(
                self,
                "Portable Restore Ready",
                "The portable backup is staged and will replace the Automadin database "
                "the next time the app starts. A safety backup of the current database "
                "will be created during activation.\n\n"
                f"Close and restart Automadin when ready.{cleanup_note}",
            )
        self.accept()

    def _cancel_or_close(self) -> None:
        thread = self._thread
        if thread is None:
            self.reject()
            return
        thread.requestInterruption()
        self._cancel_btn.setEnabled(False)
        self._status_label.setText(
            "Cancellation requested. Finishing the current safe verification or "
            "copy step…"
        )

    def reject(self) -> None:
        if self._thread is not None:
            self._cancel_or_close()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            self._thread.requestInterruption()
            self._cancel_btn.setEnabled(False)
            self._status_label.setText(
                "A portability operation is still active. Cancellation was "
                "requested; this window will remain open until cleanup finishes."
            )
            event.ignore()
            return
        super().closeEvent(event)


PortabilityImportDialog = ImportPortableBackupDialog
