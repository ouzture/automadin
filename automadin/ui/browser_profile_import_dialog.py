"""Create, import, reconcile, and remove Automadin browser profiles."""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from automadin.models.run_config import normalize_group_path
from automadin.services.browser_profile_metadata import (
    BrowserProfileIdentity,
    ProfileIdentityField,
    StoredBrowserProfile,
    compatible_engine_for_browser,
    detect_browser_profile,
)
from automadin.services.browser_profile_readiness import (
    BrowserProfileReadiness,
    BrowserProfileReadinessState,
    assess_browser_profile_readiness_many,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseRegistry,
    MaintenanceProfileLease,
    ProfileUse,
    browser_profile_leases,
)
from automadin.services.browser_profile_identity_refresh import (
    IdentityRefreshStatus,
    ProfileIdentityRefreshOutcome,
    refresh_browser_profile_identity,
)
from automadin.services.browser_profile_paths import (
    managed_profiles_root,
    new_profile_id,
    resolve_managed_profile_path,
    resolve_profile_storage_path,
)
from automadin.services.browser_profile_storage import (
    ManagedProfileStorage,
    PendingProfileDelete,
    ProfileStagingRemnant,
    ProfileStorageChange,
    ProfileStorageInspection,
    ProfileStorageStatus,
    adopt_legacy_profile,
    begin_profile_delete,
    create_clean_profile,
    discard_profile_staging_remnant,
    import_browser_profile,
    inspect_pending_profile_deletes,
    inspect_profile_storage,
    list_managed_profile_storage,
    list_profile_staging_remnants,
    recover_pending_profile_delete,
    _stage_browser_profile as _stage_browser_profile_storage,
)
from automadin.services.database import DatabaseService
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)


_CREATABLE_PROFILE_ENGINES = {
    "chromium": "Chromium",
    "firefox": "Firefox",
}
_ENGINE_BROWSER_LABELS = {
    "chromium": "Chromium",
    "chrome": "Chrome",
    "msedge": "Edge",
    "firefox": "Firefox",
}
_LEGACY_ENGINE_CHOICES = {
    "Chrome": "chrome",
    "Microsoft Edge": "msedge",
    "Chromium": "chromium",
    "Firefox": "firefox",
}
_STATUS_LABELS = {
    ProfileStorageStatus.READY: "Ready",
    ProfileStorageStatus.NEEDS_SETUP: "Needs setup",
    ProfileStorageStatus.MISSING: "Profile data missing",
    ProfileStorageStatus.CORRUPT: "Storage problem",
    ProfileStorageStatus.MISMATCH: "Identity mismatch",
    ProfileStorageStatus.INCOMPATIBLE: "Incompatible data",
}
_READINESS_ENGINES = ("chromium", "firefox", "chrome", "msedge")
_MANAGED_RUNTIME_ENGINES = frozenset({"chromium", "firefox"})
_PROFILE_ENGINE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_READINESS_FAILURE_MESSAGE = (
    "Runtime availability could not be checked. Profile management remains "
    "available; the actual browser launch is the final check."
)


def _profiles_dir() -> str:
    return str(managed_profiles_root())


def _safe_profile_path(name: str, profiles_root: str | Path | None = None) -> Path:
    """Resolve a legacy display-name folder for explicit compatibility work."""
    return resolve_managed_profile_path(name, profiles_root)


def _sanitize_profile_name(name: str) -> str:
    """Return a clean display label without applying filesystem-name rules."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", name).strip()


def _stage_browser_profile(source: Path, staging: Path, browser: str) -> None:
    """Compatibility wrapper around the canonical staged-copy implementation."""
    engine = compatible_engine_for_browser(browser)
    if not engine:
        raise RuntimeError(
            f"{browser or 'This browser'} profiles are not safely supported. "
            "Use Chrome, Edge, Chromium, or Firefox."
        )
    _stage_browser_profile_storage(source, staging, engine)


class _ManagedProfileCreationError(RuntimeError):
    """Preserve a live maintenance capability when profile creation fails."""

    def __init__(self, cause: Exception, lease: MaintenanceProfileLease) -> None:
        super().__init__(str(cause))
        self.maintenance_lease = lease


def _create_managed_profile(
    db: DatabaseService,
    name: str,
    engine: str,
    profiles_root: str | Path | None = None,
    *,
    lease_registry: BrowserProfileLeaseRegistry = browser_profile_leases,
) -> tuple[Path, MaintenanceProfileLease | None]:
    """Create UUID storage first and compensate it if database creation fails."""
    browser = _CREATABLE_PROFILE_ENGINES.get(engine)
    if browser is None:
        raise ValueError(f"Unsupported managed profile engine: {engine}")
    if db.get_profile_by_name(name) is not None:
        raise ValueError(f"A browser profile named '{name}' already exists.")

    profile_id = new_profile_id()
    lease = lease_registry.acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", f"creating profile '{name}'", str(uuid.uuid4())),
    )
    operation_error: Exception | None = None
    try:
        change = create_clean_profile(
            profile_id,
            engine,
            maintenance_lease=lease,
            lease_registry=lease_registry,
            profiles_root=profiles_root,
        )
        try:
            db.create_profile(
                name,
                profile_id=profile_id,
                browser=browser,
                engine=engine,
            )
        except Exception:
            change.rollback()
            raise
        change.commit()
        destination = change.path
    except Exception as exc:
        operation_error = exc
    finally:
        try:
            released = lease.release()
        except BaseException:
            logger.exception("Could not release created profile maintenance lease")
            released = False
    retained = lease if not released and not lease.released else None
    if operation_error is not None:
        if retained is not None:
            raise _ManagedProfileCreationError(operation_error, retained) from operation_error
        raise operation_error
    return destination, retained


def _common_profile_dirs() -> list[tuple[str, str]]:
    local_app_data = os.environ.get("LOCALAPPDATA")
    app_data = os.environ.get("APPDATA")
    roots: list[tuple[str, Path]] = []
    if local_app_data:
        local = Path(local_app_data)
        roots.extend(
            [
                ("Chrome", local / "Google" / "Chrome" / "User Data"),
                ("Edge", local / "Microsoft" / "Edge" / "User Data"),
            ]
        )
    if app_data:
        roots.append(("Firefox", Path(app_data) / "Mozilla" / "Firefox" / "Profiles"))

    found: list[tuple[str, str]] = []
    for browser_name, root in roots:
        if not root.exists():
            continue
        if browser_name == "Firefox":
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    found.append((f"{browser_name}: {child.name}", str(child)))
            continue
        children = [
            child
            for child in root.iterdir()
            if child.is_dir()
            and (child.name == "Default" or re.fullmatch(r"Profile \d+", child.name))
        ]
        for child in sorted(children, key=lambda item: item.name.casefold()):
            found.append((f"{browser_name}: {child.name}", str(child)))
    return found


class _CopyProfileThread(QThread):
    """Copy a source profile into a new UUID destination off the UI thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        source_dir: str,
        profile_id: str,
        engine: str,
        *,
        maintenance_lease: MaintenanceProfileLease,
        lease_registry: BrowserProfileLeaseRegistry,
        profiles_root: str | Path | None = None,
        created_at: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._source_dir = source_dir
        self._profile_id = profile_id
        self._engine = engine
        self._maintenance_lease = maintenance_lease
        self._lease_registry = lease_registry
        self._profiles_root = profiles_root
        self._created_at = created_at

    def run(self) -> None:
        try:
            change = import_browser_profile(
                self._source_dir,
                self._profile_id,
                self._engine,
                maintenance_lease=self._maintenance_lease,
                lease_registry=self._lease_registry,
                profiles_root=self._profiles_root,
                created_at=self._created_at,
            )
            self.succeeded.emit(change)
        except BaseException:
            logger.exception("Browser profile copy worker failed")
            self.failed.emit(
                "The browser profile could not be copied. Review Diagnostics for "
                "details, then try again."
            )


class _ProfileReadinessThread(QThread):
    """Assess every profile engine without blocking the GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, data_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data_dir = data_dir

    def run(self) -> None:
        try:
            results = assess_browser_profile_readiness_many(
                _READINESS_ENGINES,
                self._data_dir,
            )
            self.succeeded.emit(results)
        except BaseException:
            logger.exception("Browser profile runtime readiness worker failed")
            self.failed.emit(_READINESS_FAILURE_MESSAGE)


class _ProfileIdentityRefreshThread(QThread):
    """Refresh one immutable managed-profile identity off the GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        profile_id: str,
        database: DatabaseService,
        *,
        lease_registry: BrowserProfileLeaseRegistry = browser_profile_leases,
        profiles_root: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile_id = profile_id
        self._database = database
        self._lease_registry = lease_registry
        self._profiles_root = profiles_root

    def run(self) -> None:
        try:
            outcome = refresh_browser_profile_identity(
                self.profile_id,
                self._database,
                lease_registry=self._lease_registry,
                profiles_root=self._profiles_root,
                interrupted=self.isInterruptionRequested,
            )
            self.succeeded.emit(outcome)
        except BaseException:
            logger.exception("Browser profile identity refresh worker failed")
            self.failed.emit(
                "The signed-in identity could not be verified. The stored values "
                "were kept. Review Diagnostics, then try again."
            )


@dataclass(frozen=True)
class _PendingImport:
    profile_id: str
    name: str
    engine: str
    identity: BrowserProfileIdentity
    existing_profile: bool


class BrowserProfileImportDialog(QDialog):
    """Create, import, inspect, and explicitly reconcile browser profiles."""

    def __init__(
        self,
        db: DatabaseService,
        parent: QWidget | None = None,
        *,
        lease_registry: BrowserProfileLeaseRegistry = browser_profile_leases,
        data_dir: str | Path | None = None,
        open_runtime_components: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._lease_registry = lease_registry
        self._data_dir = (
            Path(data_dir).expanduser().resolve()
            if data_dir is not None
            else Path(os.path.abspath(db.db_path)).parent
        )
        self._open_runtime_components_callback = open_runtime_components
        self._thread: _CopyProfileThread | None = None
        self._copy_busy = False
        self._copy_outcome: tuple[str, object] | None = None
        self._readiness_thread: _ProfileReadinessThread | None = None
        self._readiness_outcome: tuple[str, object] | None = None
        self._readiness_cache: dict[str, BrowserProfileReadiness] = {}
        self._readiness_refresh_pending = False
        self._readiness_started = False
        self._readiness_error_message = ""
        self._identity_thread: _ProfileIdentityRefreshThread | None = None
        self._identity_outcome: tuple[str, object] | None = None
        self._identity_start_uncertain = False
        self._identity_profile_label = ""
        self._identity_close_requested = False
        self._identity_close_result = int(QDialog.DialogCode.Rejected)
        self._pending_import: _PendingImport | None = None
        self._pending_import_lease: MaintenanceProfileLease | None = None
        self._retained_maintenance_leases: list[MaintenanceProfileLease] = []
        self._import_target_profile_id: str | None = None
        self._imported_profile: str | None = None
        self._profiles_changed = False
        self._identity = BrowserProfileIdentity()
        self._last_suggested_name = ""
        self._profile_inspections: dict[str, ProfileStorageInspection | None] = {}
        self._orphan_pending_deletes: dict[str, PendingProfileDelete] = {}
        self._unlinked_managed_storage: dict[str, ManagedProfileStorage] = {}
        self._staging_remnants: dict[str, ProfileStagingRemnant] = {}
        self._initial_refresh_complete = False

        self.setWindowTitle("Browser Profiles")
        self.resize(1060, 680)
        self._build_ui()
        self._populate_common_profiles()
        self._refresh_imported_profiles()
        self._update_runtime_presentations()

    def _acquire_maintenance(
        self,
        profile_id: str,
        operation: str,
    ) -> MaintenanceProfileLease | None:
        try:
            return self._lease_registry.acquire_maintenance(
                profile_id,
                ProfileUse("maintenance", operation, str(uuid.uuid4())),
            )
        except BrowserProfileInUseError as exc:
            current = exc.current
            active = current.borrowed_by or current.use
            QMessageBox.warning(
                self,
                "Browser Profile Is In Use",
                f"This profile cannot be changed while it is being used by "
                f"'{active.label}'.\n\nStop that browser activity and try again.",
            )
            return None

    def _release_maintenance(self, lease: MaintenanceProfileLease | None) -> bool:
        if lease is None:
            return True
        try:
            lease.release()
        except BaseException:
            logger.exception("Could not release browser profile maintenance lease")
            if getattr(lease, "released", False):
                if lease in self._retained_maintenance_leases:
                    self._retained_maintenance_leases.remove(lease)
                return True
            if lease not in self._retained_maintenance_leases:
                self._retained_maintenance_leases.append(lease)
            return False
        if not lease.released:
            if lease not in self._retained_maintenance_leases:
                self._retained_maintenance_leases.append(lease)
            return False
        if lease in self._retained_maintenance_leases:
            self._retained_maintenance_leases.remove(lease)
        return True

    def _release_import_maintenance(self) -> bool:
        lease = self._pending_import_lease
        if self._release_maintenance(lease) and lease is self._pending_import_lease:
            self._pending_import_lease = None
            return True
        return lease is None

    def _warn_maintenance_cleanup_uncertain(
        self,
        *,
        applied: bool,
        detail: str = "",
    ) -> None:
        QMessageBox.warning(
            self,
            "Profile Saved; Cleanup Uncertain" if applied else "Profile Cleanup Uncertain",
            (
                "The profile change was saved, but maintenance ownership cleanup "
                "could not be confirmed. Restart Automadin before using this profile."
                if applied
                else "The profile change was not completed, and maintenance ownership "
                "cleanup could not be confirmed. Restart Automadin before trying again."
            )
            + detail,
        )

    def _release_sync_maintenance(
        self,
        lease: MaintenanceProfileLease,
        *,
        applied: bool,
    ) -> bool:
        confirmed = self._release_maintenance(lease)
        if not confirmed and not applied:
            self._warn_maintenance_cleanup_uncertain(applied=False)
        return confirmed

    @property
    def imported_profile(self) -> str | None:
        return self._imported_profile

    @property
    def profiles_changed(self) -> bool:
        return self._profiles_changed

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        heading = QLabel("Browser profiles")
        heading.setStyleSheet("font-size: 15pt; font-weight: 700;")
        layout.addWidget(heading)
        intro = QLabel(
            "Create a persistent browser identity, copy one from an installed "
            "browser, or finish setting up profiles restored from Automadin."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(intro)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)
        self._build_manage_page()
        self._build_recovery_page()
        self._build_create_page()
        self._build_import_page()

        button_row = QHBoxLayout()
        button_row.addStretch()
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.accept)
        button_row.addWidget(self._close_btn)
        layout.addLayout(button_row)

    def _build_manage_page(self) -> None:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(12, 12, 12, 12)
        page_layout.setSpacing(10)

        header_row = QHBoxLayout()
        self._profile_count_label = QLabel("Managed profiles")
        self._profile_count_label.setStyleSheet("font-weight: 700;")
        header_row.addWidget(self._profile_count_label)
        header_row.addStretch()
        self._refresh_profiles_btn = QPushButton("Refresh list & runtime")
        self._refresh_profiles_btn.clicked.connect(self._refresh_profiles_and_readiness)
        header_row.addWidget(self._refresh_profiles_btn)
        page_layout.addLayout(header_row)

        self._profiles_table = QTableWidget(0, 8)
        self._profiles_table.setHorizontalHeaderLabels(
            [
                "Profile",
                "Profile data",
                "Runtime",
                "Browser",
                "Browser profile",
                "User",
                "Email",
                "Identity checked",
            ]
        )
        self._profiles_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._profiles_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._profiles_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._profiles_table.verticalHeader().setVisible(False)
        table_header = self._profiles_table.horizontalHeader()
        for column in (0, 4, 5, 6):
            table_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 7):
            table_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        self._profiles_table.itemSelectionChanged.connect(self._on_imported_profile_selected)
        page_layout.addWidget(self._profiles_table, 1)

        self._empty_profiles_label = QLabel(
            "No profiles yet. Create a clean profile or copy one from a browser."
        )
        self._empty_profiles_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_profiles_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; padding: 18px;"
        )
        page_layout.addWidget(self._empty_profiles_label)

        self._selected_profile_details = QLabel(
            "Select a profile to see its readiness and management options."
        )
        self._selected_profile_details.setWordWrap(True)
        self._selected_profile_details.setStyleSheet(f"color: {COLORS['text_secondary']};")
        page_layout.addWidget(self._selected_profile_details)
        self._identity_refresh_status = QLabel("")
        self._identity_refresh_status.setWordWrap(True)
        self._identity_refresh_status.setStyleSheet(
            f"color: {COLORS['text_secondary']};"
        )
        page_layout.addWidget(self._identity_refresh_status)

        actions = QHBoxLayout()
        self._adopt_profile_btn = QPushButton("Use Existing Folder…")
        self._adopt_profile_btn.setEnabled(False)
        self._adopt_profile_btn.clicked.connect(self._adopt_selected_profile)
        actions.addWidget(self._adopt_profile_btn)
        self._import_copy_btn = QPushButton("Copy from Browser…")
        self._import_copy_btn.setEnabled(False)
        self._import_copy_btn.clicked.connect(self._configure_selected_profile_import)
        actions.addWidget(self._import_copy_btn)
        self._refresh_identity_btn = QPushButton("Refresh selected identity")
        self._refresh_identity_btn.setEnabled(False)
        self._refresh_identity_btn.clicked.connect(self._refresh_selected_identity)
        actions.addWidget(self._refresh_identity_btn)
        actions.addStretch()
        self._delete_profile_btn = QPushButton("Delete Profile")
        self._delete_profile_btn.setProperty("cssClass", "danger")
        self._delete_profile_btn.setEnabled(False)
        self._delete_profile_btn.clicked.connect(self._delete_selected_profile)
        actions.addWidget(self._delete_profile_btn)
        page_layout.addLayout(actions)

        self._profiles_page = page
        self._tabs.addTab(page, "Profiles")

    def _build_recovery_page(self) -> None:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self._recovery_scroll = QScrollArea(page)
        self._recovery_scroll.setWidgetResizable(True)
        self._recovery_content = QWidget()
        recovery_layout = QVBoxLayout(self._recovery_content)
        recovery_layout.setContentsMargins(12, 12, 12, 12)
        recovery_layout.setSpacing(10)

        intro = QLabel(
            "Review browser-profile data left by interrupted operations or missing "
            "database records. Recovery actions are always explicit."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {COLORS['text_secondary']};")
        recovery_layout.addWidget(intro)

        self._orphan_cleanup_panel = QWidget()
        orphan_layout = QVBoxLayout(self._orphan_cleanup_panel)
        orphan_layout.setContentsMargins(0, 8, 0, 0)
        orphan_layout.setSpacing(6)
        self._orphan_cleanup_heading = QLabel("Interrupted profile cleanup")
        self._orphan_cleanup_heading.setStyleSheet(f"color: {COLORS['warning']}; font-weight: 700;")
        orphan_layout.addWidget(self._orphan_cleanup_heading)
        self._orphan_cleanup_note = QLabel(
            "Verified browser data remains after its profile record was deleted. "
            "Keeping it is the safe default."
        )
        self._orphan_cleanup_note.setWordWrap(True)
        self._orphan_cleanup_note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        orphan_layout.addWidget(self._orphan_cleanup_note)
        self._orphan_delete_table = QTableWidget(0, 4)
        self._orphan_delete_table.setHorizontalHeaderLabels(
            [
                "Internal profile ID",
                "Engine",
                "Protected cleanup path",
                "Status / diagnostic",
            ]
        )
        self._orphan_delete_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._orphan_delete_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._orphan_delete_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._orphan_delete_table.verticalHeader().setVisible(False)
        orphan_header = self._orphan_delete_table.horizontalHeader()
        orphan_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        orphan_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        orphan_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        orphan_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._orphan_delete_table.setMaximumHeight(150)
        self._orphan_delete_table.itemSelectionChanged.connect(self._on_orphan_delete_selected)
        orphan_layout.addWidget(self._orphan_delete_table)
        orphan_actions = QHBoxLayout()
        orphan_actions.addStretch()
        self._finish_orphan_delete_btn = QPushButton("Finish Deleting")
        self._finish_orphan_delete_btn.setProperty("cssClass", "danger")
        self._finish_orphan_delete_btn.setEnabled(False)
        self._finish_orphan_delete_btn.clicked.connect(self._finish_selected_orphan_delete)
        orphan_actions.addWidget(self._finish_orphan_delete_btn)
        orphan_layout.addLayout(orphan_actions)
        self._orphan_cleanup_panel.setVisible(False)
        recovery_layout.addWidget(self._orphan_cleanup_panel)
        self._build_unlinked_storage_panel(recovery_layout)
        self._build_staging_remnants_panel(recovery_layout)
        recovery_layout.addStretch()
        self._recovery_scroll.setWidget(self._recovery_content)
        page_layout.addWidget(self._recovery_scroll)
        self._recovery_page = page
        self._recovery_tab_index = self._tabs.addTab(page, "Recovery (0)")

    def _build_unlinked_storage_panel(self, page_layout: QVBoxLayout) -> None:
        self._unlinked_storage_panel = QWidget()
        layout = QVBoxLayout(self._unlinked_storage_panel)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)
        self._unlinked_storage_heading = QLabel("Unlinked managed data")
        self._unlinked_storage_heading.setStyleSheet(
            f"color: {COLORS['warning']}; font-weight: 700;"
        )
        layout.addWidget(self._unlinked_storage_heading)
        self._unlinked_storage_note = QLabel(
            "Verified managed browser data has no matching profile record. "
            "Nothing will be attached or deleted automatically."
        )
        self._unlinked_storage_note.setWordWrap(True)
        self._unlinked_storage_note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(self._unlinked_storage_note)
        self._unlinked_storage_table = QTableWidget(0, 3)
        self._unlinked_storage_table.setHorizontalHeaderLabels(
            ["Internal profile ID", "Engine", "Managed data path"]
        )
        self._configure_reconciliation_table(self._unlinked_storage_table)
        self._unlinked_storage_table.itemSelectionChanged.connect(
            self._on_unlinked_storage_selected
        )
        layout.addWidget(self._unlinked_storage_table)
        actions = QHBoxLayout()
        self._restore_unlinked_btn = QPushButton("Restore Profile Record")
        self._restore_unlinked_btn.setEnabled(False)
        self._restore_unlinked_btn.clicked.connect(self._restore_selected_unlinked_storage)
        actions.addWidget(self._restore_unlinked_btn)
        actions.addStretch()
        self._delete_unlinked_btn = QPushButton("Delete Unlinked Data")
        self._delete_unlinked_btn.setProperty("cssClass", "danger")
        self._delete_unlinked_btn.setEnabled(False)
        self._delete_unlinked_btn.clicked.connect(self._delete_selected_unlinked_storage)
        actions.addWidget(self._delete_unlinked_btn)
        layout.addLayout(actions)
        self._unlinked_storage_panel.setVisible(False)
        page_layout.addWidget(self._unlinked_storage_panel)

    def _build_staging_remnants_panel(self, page_layout: QVBoxLayout) -> None:
        self._staging_remnants_panel = QWidget()
        layout = QVBoxLayout(self._staging_remnants_panel)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)
        self._staging_remnants_heading = QLabel("Incomplete browser-profile copies")
        self._staging_remnants_heading.setStyleSheet(
            f"color: {COLORS['warning']}; font-weight: 700;"
        )
        layout.addWidget(self._staging_remnants_heading)
        self._staging_remnants_note = QLabel(
            "These interrupted creating/importing folders may contain partial browser "
            "data. Keeping them is the safe default."
        )
        self._staging_remnants_note.setWordWrap(True)
        self._staging_remnants_note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(self._staging_remnants_note)
        self._staging_remnants_table = QTableWidget(0, 4)
        self._staging_remnants_table.setHorizontalHeaderLabels(
            ["Operation", "Internal profile ID", "Detected state", "Staging path"]
        )
        self._configure_reconciliation_table(self._staging_remnants_table)
        self._staging_remnants_table.itemSelectionChanged.connect(self._on_staging_remnant_selected)
        layout.addWidget(self._staging_remnants_table)
        actions = QHBoxLayout()
        actions.addStretch()
        self._discard_staging_btn = QPushButton("Discard Incomplete Copy")
        self._discard_staging_btn.setProperty("cssClass", "danger")
        self._discard_staging_btn.setEnabled(False)
        self._discard_staging_btn.clicked.connect(self._discard_selected_staging_remnant)
        actions.addWidget(self._discard_staging_btn)
        layout.addLayout(actions)
        self._staging_remnants_panel.setVisible(False)
        page_layout.addWidget(self._staging_remnants_panel)

    @staticmethod
    def _configure_reconciliation_table(table: QTableWidget) -> None:
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setMaximumHeight(140)
        header = table.horizontalHeader()
        for column in range(table.columnCount() - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            table.columnCount() - 1,
            QHeaderView.ResizeMode.Stretch,
        )

    def _build_create_page(self) -> None:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(12, 12, 12, 12)
        page_layout.setSpacing(12)

        note = QLabel(
            "Create a clean browser identity stored under an internal UUID. Sign in "
            "during its first run and Automadin will retain its browser state."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        page_layout.addWidget(note)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self._create_name_edit = QLineEdit()
        self._create_name_edit.setPlaceholderText("For example: Personal, Work, Store account")
        form.addRow("Profile name", self._create_name_edit)
        self._create_engine_combo = QComboBox()
        self._create_engine_combo.addItem("Chromium (recommended)", "chromium")
        self._create_engine_combo.addItem("Firefox", "firefox")
        self._create_engine_combo.currentIndexChanged.connect(self._update_runtime_presentations)
        form.addRow("Browser", self._create_engine_combo)
        page_layout.addLayout(form)

        create_runtime_row = QHBoxLayout()
        self._create_runtime_status = QLabel("Runtime not checked yet.")
        self._create_runtime_status.setWordWrap(True)
        create_runtime_row.addWidget(self._create_runtime_status, 1)
        self._create_runtime_check_btn = QPushButton("Check Again")
        self._create_runtime_check_btn.clicked.connect(self._request_readiness_refresh)
        create_runtime_row.addWidget(self._create_runtime_check_btn)
        self._create_runtime_manager_btn = QPushButton("Open Runtime Components…")
        self._create_runtime_manager_btn.clicked.connect(self._open_runtime_components)
        self._create_runtime_manager_btn.setVisible(False)
        create_runtime_row.addWidget(self._create_runtime_manager_btn)
        page_layout.addLayout(create_runtime_row)

        storage_note = QLabel(f"Managed storage: {_profiles_dir()}")
        storage_note.setWordWrap(True)
        storage_note.setStyleSheet(f"color: {COLORS['text_tertiary']};")
        page_layout.addWidget(storage_note)
        page_layout.addStretch()

        button_row = QHBoxLayout()
        button_row.addStretch()
        self._create_btn = QPushButton("Create Profile")
        self._create_btn.setProperty("cssClass", "primary")
        self._create_btn.clicked.connect(self._create_profile)
        button_row.addWidget(self._create_btn)
        page_layout.addLayout(button_row)
        self._create_page = page
        self._tabs.addTab(page, "Create New")

    def _build_import_page(self) -> None:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(12, 12, 12, 12)
        page_layout.setSpacing(10)

        note = QLabel(
            "Copy a Chrome, Edge, Chromium, or Firefox profile into new UUID-backed "
            "storage. The source folder is never changed or replaced."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_secondary']};")
        page_layout.addWidget(note)

        self._import_target_row = QWidget()
        target_layout = QHBoxLayout(self._import_target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        self._import_target_note = QLabel("")
        self._import_target_note.setWordWrap(True)
        self._import_target_note.setStyleSheet(f"color: {COLORS['warning']};")
        target_layout.addWidget(self._import_target_note, 1)
        self._new_import_target_btn = QPushButton("Copy as New Profile")
        self._new_import_target_btn.clicked.connect(self._clear_import_target)
        target_layout.addWidget(self._new_import_target_btn)
        self._import_target_row.setVisible(False)
        page_layout.addWidget(self._import_target_row)

        self._quick_combo = QComboBox()
        self._quick_combo.addItem("Choose a detected profile...", "")
        self._quick_combo.currentIndexChanged.connect(self._on_quick_selected)
        page_layout.addWidget(self._quick_combo)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        source_row = QHBoxLayout()
        self._source_edit = QLineEdit()
        self._source_edit.setPlaceholderText("Select the real browser profile folder")
        self._source_edit.textChanged.connect(self._on_source_changed)
        source_row.addWidget(self._source_edit, 1)
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._browse_source)
        source_row.addWidget(browse_button)
        form.addRow("Source folder", source_row)

        self._source_browser_combo = QComboBox()
        self._source_browser_combo.addItem("Choose the source browser…", "")
        for browser in ("Chrome", "Edge", "Chromium", "Firefox"):
            self._source_browser_combo.addItem(browser, browser)
        self._source_browser_combo.currentIndexChanged.connect(self._on_source_browser_changed)
        form.addRow("Source browser", self._source_browser_combo)

        source_runtime_widget = QWidget()
        source_runtime_row = QHBoxLayout(source_runtime_widget)
        source_runtime_row.setContentsMargins(0, 0, 0, 0)
        self._source_runtime_status = QLabel("Choose a source browser to see its runtime.")
        self._source_runtime_status.setWordWrap(True)
        source_runtime_row.addWidget(self._source_runtime_status, 1)
        self._source_runtime_check_btn = QPushButton("Check Again")
        self._source_runtime_check_btn.clicked.connect(self._request_readiness_refresh)
        source_runtime_row.addWidget(self._source_runtime_check_btn)
        self._source_runtime_manager_btn = QPushButton("Open Runtime Components…")
        self._source_runtime_manager_btn.clicked.connect(self._open_runtime_components)
        self._source_runtime_manager_btn.setVisible(False)
        source_runtime_row.addWidget(self._source_runtime_manager_btn)
        form.addRow("Runtime", source_runtime_widget)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("A clear label for this profile in Automadin")
        form.addRow("Automadin profile label", self._name_edit)
        page_layout.addLayout(form)

        identity_title = QLabel("Detected identity")
        identity_title.setStyleSheet("font-weight: 700;")
        page_layout.addWidget(identity_title)
        identity_form = QFormLayout()
        self._browser_value = QLabel("Not detected")
        self._source_profile_value = QLabel("Not detected")
        self._account_name_value = QLabel("Not detected")
        self._account_email_value = QLabel("Not detected")
        identity_form.addRow("Browser", self._browser_value)
        identity_form.addRow("Browser profile", self._source_profile_value)
        identity_form.addRow("Signed-in user", self._account_name_value)
        identity_form.addRow("Email", self._account_email_value)
        page_layout.addLayout(identity_form)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        page_layout.addWidget(self._progress)
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(f"color: {COLORS['text_tertiary']};")
        page_layout.addWidget(self._status_label)
        page_layout.addStretch()

        button_row = QHBoxLayout()
        button_row.addStretch()
        self._import_btn = QPushButton("Copy Browser Profile")
        self._import_btn.setProperty("cssClass", "primary")
        self._import_btn.clicked.connect(self._start_import)
        button_row.addWidget(self._import_btn)
        page_layout.addLayout(button_row)
        self._import_page = page
        self._tabs.addTab(page, "Copy from Browser")

    def _create_profile(self) -> None:
        name = _sanitize_profile_name(self._create_name_edit.text())
        if not name:
            QMessageBox.warning(self, "Name Required", "Enter a profile name.")
            return
        engine = str(self._create_engine_combo.currentData())
        try:
            destination, retained_lease = _create_managed_profile(
                self._db,
                name,
                engine,
                lease_registry=self._lease_registry,
            )
            record = self._db.get_profile_by_name(name)
            if record is None:
                raise RuntimeError("The new profile could not be read from the database.")
        except _ManagedProfileCreationError as exc:
            self._retained_maintenance_leases.append(exc.maintenance_lease)
            QMessageBox.warning(self, "Profile Creation Failed", str(exc))
            self._warn_maintenance_cleanup_uncertain(applied=False)
            return
        except Exception as exc:
            QMessageBox.warning(self, "Profile Creation Failed", str(exc))
            return

        if retained_lease is not None:
            self._retained_maintenance_leases.append(retained_lease)
        self._imported_profile = record.name
        self._profiles_changed = True
        self._refresh_imported_profiles(record.profile_id)
        self._tabs.setCurrentWidget(self._profiles_page)
        if retained_lease is not None:
            self._warn_maintenance_cleanup_uncertain(applied=True)
        else:
            QMessageBox.information(
                self,
                "Profile Created",
                f"Created '{record.name}' in Automadin-managed storage:\n{destination}\n\n"
                "Choose it for a browser run, then sign in normally.",
            )

    @staticmethod
    def _display_date(value: str) -> str:
        try:
            return datetime.fromisoformat(value).astimezone().strftime("%d %b %Y, %H:%M")
        except (ValueError, TypeError):
            return value or "Unknown"

    @classmethod
    def _identity_checked_summary(cls, record: StoredBrowserProfile) -> str:
        if not record.identity_checked_at:
            return "Never checked"
        checked = cls._display_date(record.identity_checked_at)
        stale = ProfileIdentityField(record.identity_stale_mask)
        if stale == ProfileIdentityField(0):
            return f"Checked {checked}"
        if stale == ProfileIdentityField.ALL:
            return f"Could not verify {checked}"
        return f"Partial {checked}"

    @staticmethod
    def _identity_field_detail(
        label: str,
        value: str,
        bit: ProfileIdentityField,
        stale_mask: int,
        *,
        empty_account: bool = False,
    ) -> str:
        if stale_mask & int(bit):
            shown = value or "No stored value"
            return f"{label}: {shown} (stored value, not verified)"
        if value:
            return f"{label}: {value}"
        if empty_account:
            return f"{label}: No signed-in user detected"
        return f"{label}: Not detected"

    @staticmethod
    def _record_engine(record: StoredBrowserProfile) -> str:
        return record.engine or compatible_engine_for_browser(record.identity.browser)

    @staticmethod
    def _readiness_color(state: BrowserProfileReadinessState | None) -> str:
        if state is BrowserProfileReadinessState.AVAILABLE:
            return COLORS["success"]
        if state is BrowserProfileReadinessState.UNAVAILABLE:
            return COLORS["error"]
        return COLORS["warning"]

    def _runtime_presentation(
        self,
        engine: str,
    ) -> tuple[str, str, BrowserProfileReadinessState | None]:
        if engine not in _READINESS_ENGINES:
            return (
                "Unknown engine",
                "This profile's browser engine is not known yet. Profile management "
                "remains available; choose its browser type during setup.",
                None,
            )
        if self._readiness_thread is not None:
            return (
                "Checking…",
                f"Checking {_ENGINE_BROWSER_LABELS[engine]} runtime availability…",
                None,
            )
        result = self._readiness_cache.get(engine)
        if result is not None:
            labels = {
                BrowserProfileReadinessState.AVAILABLE: "Available",
                BrowserProfileReadinessState.UNAVAILABLE: "Missing",
                BrowserProfileReadinessState.UNKNOWN: "Could not check",
            }
            return labels[result.state], result.message, result.state
        if self._readiness_error_message:
            return "Could not check", self._readiness_error_message, None
        return (
            "Not checked",
            "Runtime availability has not been checked yet. Profile management remains "
            "available; Automadin checks again before launch.",
            None,
        )

    def _desired_import_engine(self) -> str:
        if self._import_target_profile_id:
            record = self._db.get_profile(self._import_target_profile_id)
            if record is not None:
                engine = self._record_engine(record)
                if engine in _READINESS_ENGINES:
                    return engine
        return compatible_engine_for_browser(self._selected_source_browser())

    def _set_runtime_status(
        self,
        label: QLabel,
        manager_button: QPushButton,
        engine: str,
    ) -> None:
        _summary, detail, state = self._runtime_presentation(engine)
        label.setText(detail)
        label.setStyleSheet(f"color: {self._readiness_color(state)};")
        manager_button.setVisible(
            bool(
                self._open_runtime_components_callback
                and engine in _MANAGED_RUNTIME_ENGINES
                and state is BrowserProfileReadinessState.UNAVAILABLE
            )
        )

    def _update_runtime_presentations(self, *_args) -> None:
        create_engine = str(self._create_engine_combo.currentData() or "")
        self._set_runtime_status(
            self._create_runtime_status,
            self._create_runtime_manager_btn,
            create_engine,
        )
        self._set_runtime_status(
            self._source_runtime_status,
            self._source_runtime_manager_btn,
            self._desired_import_engine(),
        )
        for row in range(self._profiles_table.rowCount()):
            profile_item = self._profiles_table.item(row, 0)
            runtime_item = self._profiles_table.item(row, 2)
            if profile_item is None or runtime_item is None:
                continue
            engine = str(profile_item.data(_PROFILE_ENGINE_ROLE) or "")
            summary, detail, state = self._runtime_presentation(engine)
            runtime_item.setText(summary)
            runtime_item.setToolTip(detail)
            runtime_item.setForeground(QBrush(QColor(self._readiness_color(state))))
        if self._selected_profile_id() is not None:
            self._on_imported_profile_selected()

    def _refresh_profiles_and_readiness(self, *_args) -> None:
        selected_profile_id = self._selected_profile_id() or ""
        self._refresh_imported_profiles(selected_profile_id)
        self._request_readiness_refresh()

    def _request_readiness_refresh(self, *_args) -> None:
        if self._readiness_thread is not None:
            self._readiness_refresh_pending = True
            self._update_runtime_presentations()
            return
        self._readiness_started = True
        self._readiness_refresh_pending = False
        self._readiness_error_message = ""
        thread: _ProfileReadinessThread | None = None
        try:
            thread = _ProfileReadinessThread(self._data_dir, self)
            self._readiness_thread = thread
            self._readiness_outcome = None
            thread.succeeded.connect(
                lambda results, worker=thread: self._record_readiness_outcome(
                    worker,
                    "success",
                    results,
                )
            )
            thread.failed.connect(
                lambda message, worker=thread: self._record_readiness_outcome(
                    worker,
                    "failure",
                    message,
                )
            )
            thread.finished.connect(
                lambda worker=thread: self._on_readiness_thread_finished(worker)
            )
            self._update_runtime_presentations()
            self._update_controls()
            thread.start()
        except BaseException:
            logger.exception("Could not start browser profile readiness worker")
            startup_uncertain = qthread_start_may_have_succeeded(thread)
            if startup_uncertain:
                self._readiness_error_message = (
                    "The runtime check may have started. Wait for it to finish before "
                    "closing this window."
                )
            else:
                self._readiness_thread = None
                self._readiness_outcome = None
                self._readiness_error_message = _READINESS_FAILURE_MESSAGE
                if thread is not None:
                    try:
                        thread.deleteLater()
                    except Exception:
                        logger.exception("Could not release failed profile readiness worker")
            self._update_runtime_presentations()
            self._update_controls()

    def _record_readiness_outcome(
        self,
        thread: _ProfileReadinessThread,
        kind: str,
        payload: object,
    ) -> None:
        if self._readiness_thread is thread:
            self._readiness_outcome = (kind, payload)

    def _on_readiness_thread_finished(
        self,
        thread: _ProfileReadinessThread,
    ) -> None:
        if self._readiness_thread is not thread or thread.isRunning():
            return
        outcome = self._readiness_outcome
        pending_refresh = self._readiness_refresh_pending
        self._readiness_thread = None
        self._readiness_outcome = None
        self._readiness_refresh_pending = False
        try:
            thread.deleteLater()
        except Exception:
            logger.exception("Could not release completed profile readiness worker")

        if not pending_refresh:
            if outcome is not None and outcome[0] == "success":
                payload = outcome[1]
                if isinstance(payload, dict) and all(
                    isinstance(payload.get(engine), BrowserProfileReadiness)
                    and payload[engine].engine == engine
                    for engine in _READINESS_ENGINES
                ):
                    self._readiness_cache = {
                        engine: payload[engine] for engine in _READINESS_ENGINES
                    }
                    self._readiness_error_message = ""
                else:
                    self._readiness_cache = {}
                    self._readiness_error_message = _READINESS_FAILURE_MESSAGE
            elif outcome is not None and outcome[0] == "failure":
                self._readiness_cache = {}
                self._readiness_error_message = str(outcome[1])
            else:
                self._readiness_cache = {}
                self._readiness_error_message = _READINESS_FAILURE_MESSAGE

        self._update_runtime_presentations()
        self._update_controls()
        if pending_refresh and not self._identity_close_requested:
            self._request_readiness_refresh()
        self._complete_identity_close_if_idle()

    def _open_runtime_components(self) -> None:
        callback = self._open_runtime_components_callback
        if callback is not None:
            callback()

    def _inspect_record(
        self,
        record: StoredBrowserProfile,
    ) -> ProfileStorageInspection | None:
        try:
            return inspect_profile_storage(
                record.profile_id,
                self._record_engine(record) or None,
                legacy_storage_name=record.legacy_storage_name,
            )
        except Exception:
            return None

    def _refresh_imported_profiles(self, select_profile_id: str = "") -> None:
        """Refresh from database/filesystem without mutating either source."""
        records = self._db.list_profile_records()
        self._profile_inspections = {}
        self._profiles_table.setRowCount(0)
        selected_row = -1
        for row_index, record in enumerate(records):
            inspection = self._inspect_record(record)
            self._profile_inspections[record.profile_id] = inspection
            status = (
                _STATUS_LABELS.get(inspection.status, "Problem")
                if inspection is not None
                else "Inspection failed"
            )
            identity = record.identity
            engine = self._record_engine(record)
            runtime_summary, runtime_detail, runtime_state = self._runtime_presentation(engine)
            values = (
                record.name,
                status,
                runtime_summary,
                identity.browser or "Unknown",
                identity.source_profile or "Unknown",
                identity.account_name or "Not signed in",
                identity.account_email or "—",
                self._identity_checked_summary(record),
            )
            self._profiles_table.insertRow(row_index)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record.profile_id)
                    item.setData(_PROFILE_ENGINE_ROLE, engine)
                if column == 2:
                    item.setToolTip(runtime_detail)
                    item.setForeground(QBrush(QColor(self._readiness_color(runtime_state))))
                self._profiles_table.setItem(row_index, column, item)
            if record.profile_id == select_profile_id:
                selected_row = row_index

        count = len(records)
        self._profile_count_label.setText(f"{count} managed profile{'s' if count != 1 else ''}")
        self._empty_profiles_label.setVisible(count == 0)
        self._profiles_table.setVisible(count > 0)
        known_profile_ids = {record.profile_id for record in records}
        recovery_count = sum(
            (
                self._refresh_orphaned_deletes(known_profile_ids),
                self._refresh_unlinked_storage(known_profile_ids),
                self._refresh_staging_remnants(),
            )
        )
        self._tabs.setTabText(
            self._recovery_tab_index,
            f"Recovery ({recovery_count})",
        )
        if not self._initial_refresh_complete:
            rows_need_setup = any(
                inspection is None or inspection.status is not ProfileStorageStatus.READY
                for inspection in self._profile_inspections.values()
            )
            if recovery_count and not rows_need_setup:
                self._tabs.setCurrentWidget(self._recovery_page)
            self._initial_refresh_complete = True
        if selected_row >= 0:
            self._profiles_table.selectRow(selected_row)
        else:
            self._profiles_table.clearSelection()
            self._set_no_profile_selected()

    def _refresh_orphaned_deletes(self, known_profile_ids: set[str]) -> int:
        """List verified orphan tombstones without changing storage."""
        self._orphan_pending_deletes = {}
        self._orphan_delete_table.setRowCount(0)
        self._finish_orphan_delete_btn.setEnabled(False)
        try:
            inventory = inspect_pending_profile_deletes()
            pending_deletes = [
                item for item in inventory.pending if item.profile_id not in known_profile_ids
            ]
        except Exception as exc:
            self._orphan_cleanup_note.setText(
                "Interrupted profile data could not be inspected safely. "
                f"Nothing was changed.\n\n{exc}"
            )
            self._orphan_cleanup_panel.setVisible(True)
            return 1

        if not pending_deletes and not inventory.blocked:
            self._orphan_cleanup_panel.setVisible(False)
            return 0

        note = (
            "Verified browser data remains after its profile record was deleted. "
            "Keeping it is the safe default; finish deleting only if the profile "
            "should remain deleted."
        )
        if inventory.blocked:
            note += (
                "\nEntries marked Blocked require manual review and cannot be "
                "restored or deleted here."
            )
        self._orphan_cleanup_note.setText(note)
        for row, pending in enumerate(pending_deletes):
            self._orphan_pending_deletes[pending.profile_id] = pending
            values = (
                pending.profile_id,
                pending.engine,
                str(pending.tombstone_path),
                "Ready to finish",
            )
            self._orphan_delete_table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, pending.profile_id)
                self._orphan_delete_table.setItem(row, column, item)
        for diagnostic in inventory.blocked:
            row = self._orphan_delete_table.rowCount()
            paths = "\n".join(str(path) for path in diagnostic.candidate_paths)
            values = (
                diagnostic.profile_id,
                "—",
                paths,
                f"Blocked — {diagnostic.reason.value}: {diagnostic.detail}",
            )
            self._orphan_delete_table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self._orphan_delete_table.setItem(row, column, item)
        self._orphan_cleanup_panel.setVisible(True)
        return self._orphan_delete_table.rowCount()

    def _refresh_unlinked_storage(self, known_profile_ids: set[str]) -> int:
        """List valid UUID storage without binding it to the database."""
        self._unlinked_managed_storage = {}
        self._unlinked_storage_table.setRowCount(0)
        self._restore_unlinked_btn.setEnabled(False)
        self._delete_unlinked_btn.setEnabled(False)
        try:
            storage_items = [
                item
                for item in list_managed_profile_storage()
                if item.profile_id not in known_profile_ids
            ]
        except Exception as exc:
            self._unlinked_storage_note.setText(
                "Managed profile data could not be inventoried completely. "
                "Restore and delete actions are blocked.\n\n"
                f"{exc}"
            )
            self._unlinked_storage_panel.setVisible(True)
            return 1
        if not storage_items:
            self._unlinked_storage_panel.setVisible(False)
            return 0

        self._unlinked_storage_note.setText(
            "Verified managed browser data has no matching profile record. "
            "Nothing will be attached or deleted automatically."
        )
        for row, storage in enumerate(storage_items):
            self._unlinked_managed_storage[storage.profile_id] = storage
            values = (
                storage.profile_id,
                storage.engine,
                str(storage.path),
            )
            self._unlinked_storage_table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, storage.profile_id)
                self._unlinked_storage_table.setItem(row, column, item)
        self._unlinked_storage_panel.setVisible(True)
        return len(storage_items)

    def _refresh_staging_remnants(self) -> int:
        """List exact interrupted staging folders without changing them."""
        self._staging_remnants = {}
        self._staging_remnants_table.setRowCount(0)
        self._discard_staging_btn.setEnabled(False)
        try:
            remnants = list_profile_staging_remnants()
        except Exception as exc:
            self._staging_remnants_note.setText(
                "Incomplete-copy data could not be inventoried completely. "
                "Discard is blocked.\n\n"
                f"{exc}"
            )
            self._staging_remnants_panel.setVisible(True)
            return 1
        if not remnants:
            self._staging_remnants_panel.setVisible(False)
            return 0

        self._staging_remnants_note.setText(
            "These interrupted creating/importing folders may contain partial browser "
            "data. Keeping them is the safe default."
        )
        for row, remnant in enumerate(remnants):
            storage_key = str(remnant.path)
            self._staging_remnants[storage_key] = remnant
            if remnant.has_matching_manifest:
                state = remnant.manifest_engine
                if not remnant.layout_ready:
                    state += " — incomplete layout"
            else:
                state = "Manifest missing or incomplete"
            values = (
                remnant.operation.value,
                remnant.profile_id,
                state,
                storage_key,
            )
            self._staging_remnants_table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, storage_key)
                self._staging_remnants_table.setItem(row, column, item)
        self._staging_remnants_panel.setVisible(True)
        return len(remnants)

    def _selected_orphan_delete(self) -> PendingProfileDelete | None:
        rows = self._orphan_delete_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._orphan_delete_table.item(rows[0].row(), 0)
        profile_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return self._orphan_pending_deletes.get(str(profile_id))

    def _on_orphan_delete_selected(self) -> None:
        self._finish_orphan_delete_btn.setEnabled(
            self._identity_thread is None
            and self._selected_orphan_delete() is not None
        )

    def _confirm_orphan_delete(self, pending: PendingProfileDelete) -> bool:
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Finish Interrupted Profile Deletion")
        message.setText("Permanently finish deleting this browser profile data?")
        message.setInformativeText(
            f"Internal profile ID: {pending.profile_id}\n"
            f"Engine: {pending.engine}\n"
            f"Path: {pending.tombstone_path}\n\n"
            "This cannot be undone. Keep Data is the safe default."
        )
        finish_button = message.addButton(
            "Finish Deleting",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        keep_button = message.addButton(
            "Keep Data",
            QMessageBox.ButtonRole.RejectRole,
        )
        message.setDefaultButton(keep_button)
        message.setEscapeButton(keep_button)
        message.exec()
        return message.clickedButton() is finish_button

    def _finish_selected_orphan_delete(self) -> None:
        if self._copy_blocks_recovery_action():
            return
        pending = self._selected_orphan_delete()
        if pending is None or not self._confirm_orphan_delete(pending):
            return
        lease = self._acquire_maintenance(
            pending.profile_id,
            "finishing interrupted profile deletion",
        )
        if lease is None:
            return

        operation_applied = False
        try:
            if self._db.get_profile(pending.profile_id) is not None:
                raise RuntimeError(
                    "A database profile now uses this identity. Refresh and restore "
                    "its managed data instead."
                )
            change = recover_pending_profile_delete(
                pending.profile_id,
                maintenance_lease=lease,
                lease_registry=self._lease_registry,
                engine=pending.engine,
            )
            if self._db.get_profile(pending.profile_id) is not None:
                change.rollback()
                raise RuntimeError(
                    "A database profile appeared during cleanup, so its data was "
                    "restored instead of deleted."
                )
            change.commit()
            operation_applied = True
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Profile Cleanup Failed",
                f"Cleanup could not be completed. No database profile was deleted "
                f"by this action.\n\n"
                f"Engine: {pending.engine}\n"
                f"Path: {pending.tombstone_path}\n\n{exc}",
            )
            self._refresh_imported_profiles()
            return
        finally:
            cleanup_confirmed = self._release_sync_maintenance(
                lease, applied=operation_applied
            )

        if not cleanup_confirmed:
            self._profiles_changed = True
            self._refresh_imported_profiles()
            self._warn_maintenance_cleanup_uncertain(applied=True)
            return

        self._profiles_changed = True
        self._refresh_imported_profiles()
        QMessageBox.information(
            self,
            "Profile Cleanup Complete",
            f"Finished deleting the interrupted browser profile data:\n{pending.tombstone_path}",
        )

    def _copy_blocks_recovery_action(self) -> bool:
        if self._thread is None and self._identity_thread is None:
            return False
        QMessageBox.warning(
            self,
            "Profile Change In Progress",
            "Wait for the active browser-profile operation to finish before changing "
            "recovery data.",
        )
        return True

    def _selected_unlinked_storage(self) -> ManagedProfileStorage | None:
        rows = self._unlinked_storage_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._unlinked_storage_table.item(rows[0].row(), 0)
        profile_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return self._unlinked_managed_storage.get(str(profile_id))

    def _on_unlinked_storage_selected(self) -> None:
        selected = (
            self._identity_thread is None
            and self._selected_unlinked_storage() is not None
        )
        self._restore_unlinked_btn.setEnabled(selected)
        self._delete_unlinked_btn.setEnabled(selected)

    def _fresh_unlinked_storage(
        self,
        selected: ManagedProfileStorage,
    ) -> ManagedProfileStorage:
        if self._db.get_profile(selected.profile_id) is not None:
            raise RuntimeError(
                "A profile record now uses this internal identity. Refresh before continuing."
            )
        matches = [
            item
            for item in list_managed_profile_storage()
            if item.profile_id == selected.profile_id
        ]
        if len(matches) != 1:
            raise RuntimeError("The selected managed data is no longer uniquely available.")
        current = matches[0]
        if current.engine != selected.engine or current.path != selected.path:
            raise RuntimeError(
                "The selected managed data changed during review. Refresh before continuing."
            )
        return current

    @staticmethod
    def _identity_for_managed_storage(
        storage: ManagedProfileStorage,
    ) -> BrowserProfileIdentity:
        browser = _ENGINE_BROWSER_LABELS.get(storage.engine, storage.engine)
        identity_root = (
            storage.path / "Default"
            if storage.engine in {"chromium", "chrome", "msedge"}
            else storage.path
        )
        return detect_browser_profile(identity_root, browser)

    def _restore_selected_unlinked_storage(self) -> None:
        if self._copy_blocks_recovery_action():
            return
        selected = self._selected_unlinked_storage()
        if selected is None:
            return
        try:
            current = self._fresh_unlinked_storage(selected)
            identity = self._identity_for_managed_storage(current)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Cannot Restore Profile Record",
                str(exc),
            )
            self._refresh_imported_profiles()
            return

        fallback_name = f"Recovered {current.profile_id[:8]}"
        suggested_name = _sanitize_profile_name(identity.suggested_name(fallback_name))
        name, accepted = QInputDialog.getText(
            self,
            "Restore Browser Profile Record",
            "Choose a meaningful profile name.\n\n"
            f"Engine: {current.engine}\n"
            f"Managed data: {current.path}",
            QLineEdit.EchoMode.Normal,
            suggested_name,
        )
        if not accepted:
            return
        name = _sanitize_profile_name(name)
        if not name:
            QMessageBox.warning(
                self,
                "Name Required",
                "Enter a meaningful profile name.",
            )
            return

        lease = self._acquire_maintenance(
            current.profile_id,
            f"restoring profile record '{name}'",
        )
        if lease is None:
            return
        operation_applied = False
        try:
            current = self._fresh_unlinked_storage(selected)
            identity = self._identity_for_managed_storage(current)
            self._db.create_profile(
                name,
                profile_id=current.profile_id,
                browser=identity.browser,
                source_profile=identity.source_profile,
                account_name=identity.account_name,
                account_email=identity.account_email,
                engine=current.engine,
            )
            operation_applied = True
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Profile Record Restore Failed",
                f"The managed browser data was not changed.\n\n{exc}",
            )
            self._refresh_imported_profiles()
            return
        finally:
            cleanup_confirmed = self._release_sync_maintenance(
                lease, applied=operation_applied
            )

        if not cleanup_confirmed:
            self._profiles_changed = True
            self._refresh_imported_profiles()
            self._warn_maintenance_cleanup_uncertain(applied=True)
            return

        self._profiles_changed = True
        self._imported_profile = name
        self._refresh_imported_profiles(current.profile_id)
        self._tabs.setCurrentWidget(self._profiles_page)
        QMessageBox.information(
            self,
            "Profile Record Restored",
            f"Restored '{name}' using the existing managed browser data.",
        )

    def _confirm_unlinked_storage_delete(
        self,
        storage: ManagedProfileStorage,
    ) -> bool:
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Delete Unlinked Managed Data")
        message.setText("Permanently delete this unlinked browser profile data?")
        message.setInformativeText(
            f"Internal profile ID: {storage.profile_id}\n"
            f"Engine: {storage.engine}\n"
            f"Path: {storage.path}\n\n"
            "This cannot be undone. Keep Data is the safe default."
        )
        delete_button = message.addButton(
            "Delete Unlinked Data",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        keep_button = message.addButton(
            "Keep Data",
            QMessageBox.ButtonRole.RejectRole,
        )
        message.setDefaultButton(keep_button)
        message.setEscapeButton(keep_button)
        message.exec()
        return message.clickedButton() is delete_button

    def _delete_selected_unlinked_storage(self) -> None:
        if self._copy_blocks_recovery_action():
            return
        selected = self._selected_unlinked_storage()
        if selected is None or not self._confirm_unlinked_storage_delete(selected):
            return

        lease = self._acquire_maintenance(
            selected.profile_id,
            "deleting unlinked browser-profile data",
        )
        if lease is None:
            return
        change: ProfileStorageChange | None = None
        operation_applied = False
        try:
            current = self._fresh_unlinked_storage(selected)
            change = begin_profile_delete(
                current.profile_id,
                maintenance_lease=lease,
                lease_registry=self._lease_registry,
                engine=current.engine,
            )
            if self._db.get_profile(current.profile_id) is not None:
                change.rollback()
                raise RuntimeError(
                    "A profile record appeared during deletion, so its managed data "
                    "was restored instead."
                )
            change.commit()
            operation_applied = True
        except Exception as exc:
            rollback_note = ""
            if change is not None and change.active:
                try:
                    change.rollback()
                except Exception as rollback_exc:
                    rollback_note = f"\n\nAutomatic storage restoration also failed: {rollback_exc}"
            QMessageBox.warning(
                self,
                "Unlinked Data Deletion Failed",
                f"Profile ID: {selected.profile_id}\n"
                f"Engine: {selected.engine}\n"
                f"Path: {selected.path}\n\n{exc}{rollback_note}",
            )
            self._refresh_imported_profiles()
            return
        finally:
            cleanup_confirmed = self._release_sync_maintenance(
                lease, applied=operation_applied
            )

        if not cleanup_confirmed:
            self._profiles_changed = True
            self._refresh_imported_profiles()
            self._warn_maintenance_cleanup_uncertain(applied=True)
            return

        self._profiles_changed = True
        self._refresh_imported_profiles()
        QMessageBox.information(
            self,
            "Unlinked Data Deleted",
            f"Deleted the unlinked managed browser data:\n{selected.path}",
        )

    def _selected_staging_remnant(self) -> ProfileStagingRemnant | None:
        rows = self._staging_remnants_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._staging_remnants_table.item(rows[0].row(), 0)
        storage_key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return self._staging_remnants.get(str(storage_key))

    def _on_staging_remnant_selected(self) -> None:
        self._discard_staging_btn.setEnabled(
            self._identity_thread is None
            and self._selected_staging_remnant() is not None
        )

    def _confirm_staging_discard(self, remnant: ProfileStagingRemnant) -> bool:
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Discard Incomplete Browser Profile Copy")
        message.setText("Permanently discard this incomplete browser-profile copy?")
        message.setInformativeText(
            f"Operation: {remnant.operation.value}\n"
            f"Internal profile ID: {remnant.profile_id}\n"
            f"Path: {remnant.path}\n\n"
            "It may contain partial browser data. Keep Data is the safe default."
        )
        discard_button = message.addButton(
            "Discard Incomplete Copy",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        keep_button = message.addButton(
            "Keep Data",
            QMessageBox.ButtonRole.RejectRole,
        )
        message.setDefaultButton(keep_button)
        message.setEscapeButton(keep_button)
        message.exec()
        return message.clickedButton() is discard_button

    def _discard_selected_staging_remnant(self) -> None:
        if self._copy_blocks_recovery_action():
            return
        remnant = self._selected_staging_remnant()
        if remnant is None or not self._confirm_staging_discard(remnant):
            return

        lease = self._acquire_maintenance(
            remnant.profile_id,
            f"discarding interrupted {remnant.operation.value} data",
        )
        if lease is None:
            return
        operation_applied = False
        try:
            if self._thread is not None:
                raise RuntimeError(
                    "A browser-profile copy started during review. The incomplete copy was kept."
                )
            discard_profile_staging_remnant(
                remnant,
                maintenance_lease=lease,
                lease_registry=self._lease_registry,
            )
            operation_applied = True
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Incomplete Copy Cleanup Failed",
                f"Cleanup could not be completed. Review the remaining path before "
                f"retrying.\n\n"
                f"Operation: {remnant.operation.value}\n"
                f"Profile ID: {remnant.profile_id}\n"
                f"Path: {remnant.path}\n\n{exc}",
            )
            self._refresh_imported_profiles()
            return
        finally:
            cleanup_confirmed = self._release_sync_maintenance(
                lease, applied=operation_applied
            )

        if not cleanup_confirmed:
            self._profiles_changed = True
            self._refresh_imported_profiles()
            self._warn_maintenance_cleanup_uncertain(applied=True)
            return

        self._profiles_changed = True
        self._refresh_imported_profiles()
        QMessageBox.information(
            self,
            "Incomplete Copy Discarded",
            f"Discarded the interrupted {remnant.operation.value} data:\n{remnant.path}",
        )

    def _set_no_profile_selected(self) -> None:
        self._delete_profile_btn.setEnabled(False)
        self._adopt_profile_btn.setEnabled(False)
        self._adopt_profile_btn.setText("Use Existing Folder…")
        self._import_copy_btn.setEnabled(False)
        self._refresh_identity_btn.setEnabled(False)
        self._selected_profile_details.setText(
            "Select a profile to see its readiness and management options."
        )

    def _selected_profile_id(self) -> str | None:
        rows = self._profiles_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._profiles_table.item(rows[0].row(), 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return str(value) if value else None

    def _selected_profile_name(self) -> str | None:
        """Compatibility accessor returning the selected display label."""
        record = self._selected_profile_record()
        return record.name if record is not None else None

    def _selected_profile_record(self) -> StoredBrowserProfile | None:
        profile_id = self._selected_profile_id()
        return self._db.get_profile(profile_id) if profile_id else None

    def _on_imported_profile_selected(self) -> None:
        record = self._selected_profile_record()
        if record is None:
            self._set_no_profile_selected()
            return
        inspection = self._profile_inspections.get(record.profile_id)
        mutating = self._copy_busy or self._thread is not None or self._identity_thread is not None
        self._delete_profile_btn.setEnabled(not mutating)

        can_finish = bool(
            record.legacy_storage_name
            and inspection is not None
            and inspection.status is ProfileStorageStatus.NEEDS_SETUP
            and inspection.manifest is not None
            and inspection.legacy_candidate is None
            and inspection.recovery_path is None
            and os.path.lexists(inspection.path)
        )
        can_adopt = bool(
            record.legacy_storage_name
            and inspection is not None
            and inspection.status is ProfileStorageStatus.NEEDS_SETUP
            and inspection.legacy_candidate is not None
        )
        can_restore = bool(
            inspection is not None
            and inspection.recovery_path is not None
            and inspection.manifest is not None
        )
        self._adopt_profile_btn.setEnabled(
            not mutating and (can_restore or can_finish or can_adopt)
        )
        if can_restore:
            self._adopt_profile_btn.setText("Restore Managed Data…")
        elif can_finish:
            self._adopt_profile_btn.setText("Finish Setup…")
        else:
            self._adopt_profile_btn.setText("Use Existing Folder…")
        can_import_copy = bool(
            inspection is not None
            and not os.path.lexists(inspection.path)
            and inspection.recovery_path is None
        )
        self._import_copy_btn.setEnabled(not mutating and can_import_copy)
        can_refresh_identity = bool(
            not mutating
            and not record.legacy_storage_name
            and inspection is not None
            and inspection.status is ProfileStorageStatus.READY
        )
        self._refresh_identity_btn.setEnabled(can_refresh_identity)

        if inspection is None:
            self._selected_profile_details.setText(
                f"{record.name}\nStorage inspection failed. No profile data "
                "will be changed until this can be inspected safely."
            )
            return
        details = [
            record.name,
            f"Profile data: {_STATUS_LABELS.get(inspection.status, 'Problem')}",
            f"Runtime: {self._runtime_presentation(self._record_engine(record))[0]}",
            f"Identity checked: {self._identity_checked_summary(record)}",
            self._identity_field_detail(
                "Browser profile",
                record.identity.source_profile,
                ProfileIdentityField.SOURCE_PROFILE,
                record.identity_stale_mask,
            ),
            self._identity_field_detail(
                "Signed-in user",
                record.identity.account_name,
                ProfileIdentityField.ACCOUNT_NAME,
                record.identity_stale_mask,
                empty_account=True,
            ),
            self._identity_field_detail(
                "Email",
                record.identity.account_email,
                ProfileIdentityField.ACCOUNT_EMAIL,
                record.identity_stale_mask,
                empty_account=True,
            ),
            f"Created: {self._display_date(record.created_at)}",
            inspection.detail,
            f"Managed copy: {inspection.path}",
        ]
        if inspection.legacy_candidate is not None:
            details.append(f"Existing-folder candidate: {inspection.legacy_candidate}")
        if inspection.recovery_path is not None:
            details.append(f"Recovery data: {inspection.recovery_path}")
        details.extend(
            (
                "After signing in or changing browser accounts, close that browser "
                "and choose Refresh selected identity.",
                f"Internal ID: {record.profile_id[:8]}…",
            )
        )
        self._selected_profile_details.setText("\n".join(details))

    def _confirm_adoption(
        self,
        record: StoredBrowserProfile,
        source_path: Path,
        *,
        finishing: bool,
    ) -> bool:
        account = record.identity.friendly_label("No signed-in identity recorded")
        action = "finish binding" if finishing else "move and use"
        reply = QMessageBox.question(
            self,
            "Finish Browser Profile Setup",
            f"Profile: {record.name}\n"
            f"Identity: {account}\n"
            f"Folder: {source_path}\n\n"
            f"Automadin will {action} this folder for the selected profile. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _adopt_selected_profile(self) -> None:
        record = self._selected_profile_record()
        if record is None:
            return
        inspection = self._profile_inspections.get(record.profile_id)
        if inspection is not None and inspection.recovery_path is not None:
            self._restore_interrupted_delete(record, inspection)
            return
        if not record.legacy_storage_name:
            return
        if inspection is None or inspection.status is not ProfileStorageStatus.NEEDS_SETUP:
            QMessageBox.warning(
                self,
                "Cannot Use Folder",
                "This profile does not have a safely adoptable folder.",
            )
            return
        engine = self._record_engine(record) or inspection.engine
        if not engine:
            choice, accepted = QInputDialog.getItem(
                self,
                "Choose Browser Type",
                f"Which browser type does '{record.name}' use?",
                tuple(_LEGACY_ENGINE_CHOICES),
                2,
                False,
            )
            if not accepted:
                return
            engine = _LEGACY_ENGINE_CHOICES[choice]

        finishing = inspection.manifest is not None and inspection.legacy_candidate is None
        source_path = inspection.path if finishing else inspection.legacy_candidate
        if source_path is None or not self._confirm_adoption(
            record,
            source_path,
            finishing=finishing,
        ):
            return

        lease = self._acquire_maintenance(
            record.profile_id,
            f"setting up profile '{record.name}'",
        )
        if lease is None:
            return
        change: ProfileStorageChange | None = None
        operation_applied = False
        setup_error: str | None = None
        applied_record: StoredBrowserProfile | None = None
        try:
            if not finishing:
                change = adopt_legacy_profile(
                    record.legacy_storage_name,
                    record.profile_id,
                    engine,
                    maintenance_lease=lease,
                    lease_registry=self._lease_registry,
                    created_at=record.created_at,
                )
            if not self._db.mark_profile_storage_resolved(
                record.profile_id,
                engine=engine,
            ):
                raise RuntimeError("The profile database record no longer exists.")
            applied_record = self._db.get_profile(record.profile_id)
            if applied_record is None:
                raise RuntimeError("The updated profile database record could not be read.")
            if change is not None:
                change.commit()
            operation_applied = True
        except Exception as exc:
            compensation_error = ""
            current_record = self._db.get_profile(record.profile_id)
            if current_record != record:
                try:
                    if applied_record is None or not self._db.restore_profile_snapshot(
                        record,
                        expected_current=applied_record,
                    ):
                        raise RuntimeError("The original database profile no longer exists.")
                except Exception as compensation_exc:
                    compensation_error = (
                        "\n\nThe database update could not be restored exactly: "
                        f"{compensation_exc}"
                    )
            rollback_error = ""
            if change is not None and change.active:
                try:
                    change.rollback()
                except Exception as recovery_exc:
                    rollback_error = f"\n\nAutomatic folder recovery failed: {recovery_exc}"
            operation_applied = bool(compensation_error or rollback_error)
            recovery_note = (
                "\n\nSetup may be partially applied. Refresh the Recovery tab "
                "before using this profile."
                if operation_applied
                else ""
            )
            setup_error = (
                f"{exc}{compensation_error}{rollback_error}{recovery_note}"
            )
        finally:
            cleanup_confirmed = self._release_sync_maintenance(
                lease, applied=operation_applied
            )
        if setup_error is not None:
            if operation_applied:
                self._profiles_changed = True
                self._refresh_imported_profiles(record.profile_id)
            QMessageBox.warning(self, "Profile Setup Failed", setup_error)
            if not cleanup_confirmed and operation_applied:
                self._warn_maintenance_cleanup_uncertain(applied=True)
            return
        if not cleanup_confirmed:
            self._profiles_changed = True
            self._refresh_imported_profiles(record.profile_id)
            self._warn_maintenance_cleanup_uncertain(applied=True)
            return

        self._profiles_changed = True
        self._imported_profile = record.name
        self._refresh_imported_profiles(record.profile_id)
        QMessageBox.information(
            self,
            "Profile Ready",
            f"'{record.name}' is now available for browser runs.",
        )

    def _restore_interrupted_delete(
        self,
        record: StoredBrowserProfile,
        inspection: ProfileStorageInspection,
    ) -> None:
        reply = QMessageBox.question(
            self,
            "Restore Browser Profile Data",
            f"Automadin found browser data from an interrupted deletion:\n"
            f"{inspection.recovery_path}\n\n"
            f"Restore it for '{record.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        lease = self._acquire_maintenance(
            record.profile_id,
            f"restoring profile data for '{record.name}'",
        )
        if lease is None:
            return
        operation_applied = False
        try:
            change = recover_pending_profile_delete(
                record.profile_id,
                maintenance_lease=lease,
                lease_registry=self._lease_registry,
                engine=self._record_engine(record) or inspection.engine or None,
            )
            change.rollback()
            operation_applied = True
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Profile Restore Failed",
                str(exc),
            )
            return
        finally:
            cleanup_confirmed = self._release_sync_maintenance(
                lease, applied=operation_applied
            )
        if not cleanup_confirmed:
            self._profiles_changed = True
            self._refresh_imported_profiles()
            self._warn_maintenance_cleanup_uncertain(applied=True)
            return
        self._profiles_changed = True
        self._refresh_imported_profiles(record.profile_id)
        QMessageBox.information(
            self,
            "Profile Data Restored",
            f"Restored the managed browser data for '{record.name}'.",
        )

    def _configure_selected_profile_import(self) -> None:
        record = self._selected_profile_record()
        if record is None:
            return
        inspection = self._profile_inspections.get(record.profile_id)
        if (
            inspection is None
            or os.path.lexists(inspection.path)
            or inspection.recovery_path is not None
        ):
            QMessageBox.warning(
                self,
                "Managed Copy Already Exists",
                "This profile has managed or recovery storage that must be resolved "
                "first. Copying never overwrites UUID storage.",
            )
            return
        self._import_target_profile_id = record.profile_id
        self._name_edit.setText(record.name)
        self._name_edit.setReadOnly(True)
        self._import_target_note.setText(
            f"Copying browser data for existing profile: {record.display_label}"
        )
        self._import_target_row.setVisible(True)
        if (
            inspection is not None
            and inspection.legacy_candidate is not None
            and inspection.legacy_candidate.is_dir()
        ):
            self._source_edit.setText(str(inspection.legacy_candidate))
        engine = self._record_engine(record)
        browser_label = _ENGINE_BROWSER_LABELS.get(engine)
        if browser_label:
            browser_index = self._source_browser_combo.findData(browser_label)
            if browser_index >= 0:
                self._source_browser_combo.setCurrentIndex(browser_index)
        else:
            self._source_browser_combo.setCurrentIndex(0)
        self._update_runtime_presentations()
        self._tabs.setCurrentWidget(self._import_page)

    def _clear_import_target(self) -> None:
        self._import_target_profile_id = None
        self._name_edit.setReadOnly(False)
        self._name_edit.clear()
        self._import_target_note.clear()
        self._import_target_row.setVisible(False)
        self._update_runtime_presentations()

    def _delete_selected_profile(self) -> None:
        record = self._selected_profile_record()
        if record is None:
            return
        try:
            references = self._db.list_run_configs_referencing_profile(record.profile_id)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Cannot Check Profile Usage",
                "Automadin could not safely check saved run setups, so nothing was "
                f"deleted.\n\n{exc}",
            )
            return
        if references:
            labels = []
            for config in references:
                group = normalize_group_path(config.group_path)
                labels.append(f"{group}/{config.name}" if group else config.name)
            affected = "\n".join(f"• {label}" for label in labels)
            QMessageBox.warning(
                self,
                "Profile Is In Use",
                f"Browser profile '{record.name}' is used by:\n\n{affected}\n\n"
                "Edit or delete those run setups first.",
            )
            return

        inspection = self._profile_inspections.get(record.profile_id)
        if inspection is None:
            QMessageBox.warning(
                self,
                "Cannot Delete Profile",
                "Storage could not be inspected safely, so nothing was deleted.",
            )
            return
        unresolved = bool(record.legacy_storage_name)
        storage_delete = not unresolved and inspection.status is ProfileStorageStatus.READY
        managed_artifact_exists = bool(
            os.path.lexists(inspection.path) or inspection.recovery_path is not None
        )
        database_only = (unresolved and not managed_artifact_exists) or (
            not unresolved
            and inspection.status is ProfileStorageStatus.MISSING
            and not managed_artifact_exists
        )
        if not storage_delete and not database_only:
            QMessageBox.warning(
                self,
                "Cannot Delete Profile",
                "This profile has mismatched, corrupt, incompatible, or recoverable "
                "storage. Resolve that condition before deleting it.",
            )
            return
        if not self._confirm_profile_deletion(
            record,
            inspection,
            removes_storage=storage_delete,
        ):
            return

        lease = self._acquire_maintenance(
            record.profile_id,
            f"deleting profile '{record.name}'",
        )
        if lease is None:
            return
        change: ProfileStorageChange | None = None
        operation_applied = False
        try:
            if storage_delete:
                change = begin_profile_delete(
                    record.profile_id,
                    maintenance_lease=lease,
                    lease_registry=self._lease_registry,
                    engine=record.engine or None,
                )
            if not self._db.delete_profile_by_id(record.profile_id):
                raise RuntimeError("The profile database record no longer exists.")
            operation_applied = True
        except Exception as exc:
            rollback_error = ""
            if change is not None and change.active:
                try:
                    change.rollback()
                except Exception as recovery_exc:
                    rollback_error = f"\n\nAutomatic folder recovery failed: {recovery_exc}"
            QMessageBox.warning(
                self,
                "Profile Deletion Failed",
                f"{exc}{rollback_error}",
            )
            return
        else:
            cleanup_warning = ""
            if change is not None:
                try:
                    change.commit()
                except Exception as exc:
                    cleanup_warning = (
                        "\n\nThe database record was deleted, but protected cleanup "
                        f"data remains at:\n{change.working_path}\n\n{exc}"
                    )
        finally:
            cleanup_confirmed = self._release_sync_maintenance(
                lease, applied=operation_applied
            )
        if not cleanup_confirmed:
            self._profiles_changed = True
            self._status_label.setText("")
            self._refresh_imported_profiles()
            self._warn_maintenance_cleanup_uncertain(
                applied=True,
                detail=cleanup_warning,
            )
            return
        self._profiles_changed = True
        self._status_label.setText("")
        self._refresh_imported_profiles()
        deletion_message = (
            f"Deleted Automadin profile '{record.name}'."
            + ("\n\nIts existing legacy folder was not changed." if unresolved else "")
            + cleanup_warning
        )
        if cleanup_warning:
            QMessageBox.warning(
                self,
                "Profile Deleted; Cleanup Incomplete",
                deletion_message,
            )
        else:
            QMessageBox.information(self, "Profile Deleted", deletion_message)

    def _confirm_profile_deletion(
        self,
        record: StoredBrowserProfile,
        inspection: ProfileStorageInspection,
        *,
        removes_storage: bool,
    ) -> bool:
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Delete Browser Profile")
        message.setText(f"Delete Automadin profile '{record.name}'?")
        if removes_storage:
            consequence = f"Managed browser data will also be deleted:\n{inspection.path}"
        else:
            consequence = (
                "Only the Automadin database record will be deleted. Existing legacy "
                f"or source folders will not be changed.\n\nCandidate: "
                f"{inspection.legacy_candidate or 'none'}"
            )
        message.setInformativeText(consequence)
        delete_button = message.addButton(
            "Delete Profile",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.exec()
        return message.clickedButton() is delete_button

    def _populate_common_profiles(self) -> None:
        for label, path in _common_profile_dirs():
            browser_hint = label.split(":", 1)[0]
            identity = detect_browser_profile(path, browser_hint)
            self._quick_combo.addItem(identity.friendly_label(label), path)

    def _on_quick_selected(self, index: int) -> None:
        path = self._quick_combo.itemData(index)
        if path:
            self._source_edit.setText(str(path))

    def _browse_source(self) -> None:
        start_dir = str(Path.home())
        current = self._source_edit.text().strip()
        if current and Path(current).exists():
            start_dir = current
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Browser Profile Folder",
            start_dir,
        )
        if path:
            self._source_edit.setText(path)

    def _on_source_changed(self, source: str) -> None:
        source_path = Path(source.strip())
        if not source_path.name:
            self._identity = BrowserProfileIdentity()
            self._source_browser_combo.setCurrentIndex(0)
            for label in (
                self._browser_value,
                self._source_profile_value,
                self._account_name_value,
                self._account_email_value,
            ):
                label.setText("Not detected")
            return
        self._identity = detect_browser_profile(source_path)
        detected_browser = self._identity.browser
        if compatible_engine_for_browser(detected_browser):
            index = self._source_browser_combo.findData(detected_browser)
            if index >= 0:
                self._source_browser_combo.setCurrentIndex(index)
        selected_browser = self._selected_source_browser()
        self._browser_value.setText(selected_browser or detected_browser or "Choose source browser")
        self._source_profile_value.setText(
            self._identity.source_profile or source_path.name or "Not detected"
        )
        self._account_name_value.setText(self._identity.account_name or "Not detected")
        self._account_email_value.setText(self._identity.account_email or "Not detected")

        if self._import_target_profile_id is not None:
            return
        current = self._name_edit.text().strip()
        suggested = _sanitize_profile_name(
            self._effective_import_identity().suggested_name(source_path.name)
        )
        if suggested and (not current or current == self._last_suggested_name):
            self._name_edit.setText(suggested)
            self._last_suggested_name = suggested

    def _selected_source_browser(self) -> str:
        return str(self._source_browser_combo.currentData() or "")

    def _effective_import_identity(self) -> BrowserProfileIdentity:
        return BrowserProfileIdentity(
            browser=self._selected_source_browser() or self._identity.browser,
            source_profile=self._identity.source_profile,
            account_name=self._identity.account_name,
            account_email=self._identity.account_email,
        )

    def _on_source_browser_changed(self, _index: int = -1) -> None:
        browser = self._selected_source_browser()
        self._browser_value.setText(browser or self._identity.browser or "Choose source browser")
        self._update_runtime_presentations()

    def _start_import(self) -> None:
        source = self._source_edit.text().strip()
        if not source:
            QMessageBox.warning(self, "Source Required", "Choose a source profile folder.")
            return
        browser = self._selected_source_browser()
        if not browser:
            QMessageBox.warning(
                self,
                "Browser Required",
                "Choose whether the source folder belongs to Chrome, Edge, Chromium, or Firefox.",
            )
            return
        engine = compatible_engine_for_browser(browser)
        if not engine:
            QMessageBox.warning(
                self,
                "Unsupported Browser Profile",
                "Direct import supports Chrome, Edge, Chromium, and Firefox profiles.",
            )
            return

        existing_target = self._import_target_profile_id is not None
        if existing_target:
            record = self._db.get_profile(self._import_target_profile_id)
            if record is None:
                QMessageBox.warning(
                    self,
                    "Profile No Longer Available",
                    "Select the profile again.",
                )
                return
            if record.engine and record.engine != engine:
                QMessageBox.warning(
                    self,
                    "Browser Does Not Match",
                    f"'{record.name}' requires {record.engine}, but the selected source "
                    f"was detected as {engine}.",
                )
                return
            inspection = self._inspect_record(record)
            if (
                inspection is None
                or os.path.lexists(inspection.path)
                or inspection.recovery_path is not None
            ):
                QMessageBox.warning(
                    self,
                    "Managed Copy Already Exists",
                    "Copying never overwrites UUID storage. Finish setup or resolve the "
                    "existing storage first.",
                )
                return
            profile_id = record.profile_id
            name = record.name
            created_at = record.created_at
        else:
            name = _sanitize_profile_name(self._name_edit.text())
            if not name:
                QMessageBox.warning(self, "Name Required", "Enter a profile name.")
                return
            try:
                collision = self._db.get_profile_by_name(name)
            except Exception as exc:
                QMessageBox.warning(self, "Invalid Profile Name", str(exc))
                return
            if collision is not None:
                QMessageBox.warning(
                    self,
                    "Profile Exists",
                    f"A browser profile named '{collision.name}' already exists.",
                )
                return
            profile_id = new_profile_id()
            created_at = None

        destination = resolve_profile_storage_path(profile_id)
        reply = QMessageBox.question(
            self,
            "Copy Browser Profile",
            "Close the source browser before copying to avoid a partial profile.\n\n"
            f"Profile: {name}\nSource: {source}\nManaged copy: {destination}\n\n"
            "The source folder will not be changed. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        lease = self._acquire_maintenance(
            profile_id,
            f"importing browser data for profile '{name}'",
        )
        if lease is None:
            return
        self._pending_import_lease = lease
        self._pending_import = _PendingImport(
            profile_id=profile_id,
            name=name,
            engine=engine,
            identity=self._effective_import_identity(),
            existing_profile=existing_target,
        )
        self._set_running(True)
        self._status_label.setStyleSheet(f"color: {COLORS['warning']};")
        self._status_label.setText("Copying profile into protected managed storage…")
        thread: _CopyProfileThread | None = None
        try:
            thread = _CopyProfileThread(
                source,
                profile_id,
                engine,
                maintenance_lease=lease,
                lease_registry=self._lease_registry,
                created_at=created_at,
                parent=self,
            )
            self._thread = thread
            self._copy_outcome = None
            thread.succeeded.connect(
                lambda change, worker=thread: self._record_copy_outcome(
                    worker,
                    "success",
                    change,
                )
            )
            thread.failed.connect(
                lambda message, worker=thread: self._record_copy_outcome(
                    worker,
                    "failure",
                    message,
                )
            )
            finished_signal = getattr(thread, "finished", None)
            if finished_signal is not None:
                finished_signal.connect(lambda worker=thread: self._on_copy_thread_finished(worker))
            thread.start()
        except BaseException:
            logger.exception("Could not start browser profile copy worker")
            startup_uncertain = qthread_start_may_have_succeeded(thread)
            if startup_uncertain:
                self._status_label.setText(
                    "The copy started but startup could not be confirmed. Wait "
                    "for it to finish before closing this window."
                )
            else:
                self._thread = None
                self._copy_outcome = None
                self._pending_import = None
                self._release_import_maintenance()
                self._set_running(False)
                if thread is not None:
                    try:
                        thread.deleteLater()
                    except Exception:
                        logger.exception("Could not release failed profile copy worker")
            QMessageBox.warning(
                self,
                (
                    "Profile Copy Startup Uncertain"
                    if startup_uncertain
                    else "Profile Copy Could Not Start"
                ),
                (
                    "The copy may have begun, but startup could not be confirmed. "
                    "Wait for the retained worker's final result before trying again."
                    if startup_uncertain
                    else "The profile copy worker could not be started. Review "
                    "Diagnostics for details, then try again."
                ),
            )

    def _set_running(self, running: bool) -> None:
        self._copy_busy = running
        self._update_controls()

    def _refresh_selected_identity(self, *_args) -> None:
        if self._identity_thread is not None or self._copy_busy or self._thread is not None:
            return
        record = self._selected_profile_record()
        if record is None:
            return
        inspection = self._profile_inspections.get(record.profile_id)
        if (
            record.legacy_storage_name
            or inspection is None
            or inspection.status is not ProfileStorageStatus.READY
        ):
            self._on_imported_profile_selected()
            return

        thread: _ProfileIdentityRefreshThread | None = None
        try:
            thread = _ProfileIdentityRefreshThread(
                record.profile_id,
                self._db,
                lease_registry=self._lease_registry,
                parent=self,
            )
            self._identity_thread = thread
            self._identity_outcome = None
            self._identity_start_uncertain = False
            self._identity_profile_label = record.name
            self._identity_refresh_status.setText(
                f"Refreshing identity for '{record.name}'…"
            )
            thread.succeeded.connect(
                lambda outcome, worker=thread: self._record_identity_outcome(
                    worker,
                    "success",
                    outcome,
                )
            )
            thread.failed.connect(
                lambda message, worker=thread: self._record_identity_outcome(
                    worker,
                    "failure",
                    message,
                )
            )
            thread.finished.connect(
                lambda worker=thread: self._on_identity_thread_finished(worker)
            )
            self._update_controls()
            thread.start()
        except BaseException:
            logger.exception("Could not start browser profile identity refresh worker")
            startup_uncertain = qthread_start_may_have_succeeded(thread)
            self._identity_start_uncertain = startup_uncertain
            if not startup_uncertain:
                self._identity_thread = None
                self._identity_outcome = None
                if thread is not None:
                    try:
                        thread.deleteLater()
                    except Exception:
                        logger.exception(
                            "Could not release failed profile identity refresh worker"
                        )
                self._identity_refresh_status.setText(
                    "Identity refresh could not start. Stored values were not changed."
                )
            else:
                self._identity_refresh_status.setText(
                    "Identity refresh startup is uncertain; retaining the worker "
                    "until it finishes."
                )
            self._update_controls()
            QMessageBox.warning(
                self,
                (
                    "Identity Refresh Startup Uncertain"
                    if startup_uncertain
                    else "Identity Refresh Could Not Start"
                ),
                (
                    "The identity refresh may have started. Wait for the retained "
                    "worker to finish before closing this window or trying again."
                    if startup_uncertain
                    else "The identity refresh could not be started. The stored "
                    "identity was not changed. Review Diagnostics, then try again."
                ),
            )

    def _record_identity_outcome(
        self,
        thread: _ProfileIdentityRefreshThread,
        kind: str,
        payload: object,
    ) -> None:
        if self._identity_thread is thread:
            self._identity_outcome = (kind, payload)

    def _on_identity_thread_finished(
        self,
        thread: _ProfileIdentityRefreshThread,
    ) -> None:
        if self._identity_thread is not thread or thread.isRunning():
            return
        outcome = self._identity_outcome
        profile_label = self._identity_profile_label or f"profile {thread.profile_id[:8]}…"
        selected_profile_id = self._selected_profile_id() or ""
        self._identity_thread = None
        self._identity_outcome = None
        self._identity_start_uncertain = False
        self._identity_profile_label = ""
        try:
            thread.deleteLater()
        except Exception:
            logger.exception("Could not release completed profile identity worker")

        public_message = (
            "The signed-in identity could not be verified. The stored values were "
            "kept. Review Diagnostics, then try again."
        )
        if (
            outcome is not None
            and outcome[0] == "success"
            and isinstance(outcome[1], ProfileIdentityRefreshOutcome)
            and outcome[1].profile_id == thread.profile_id
        ):
            result = outcome[1]
            public_message = result.message
            if result.status is IdentityRefreshStatus.CLEANUP_FAILED:
                self._profiles_changed = True
                self._identity_refresh_status.setText(
                    f"{profile_label}: {public_message}"
                )
                QMessageBox.warning(
                    self,
                    "Identity Saved; Cleanup Uncertain",
                    public_message,
                )
            elif result.status in {
                IdentityRefreshStatus.UPDATED,
                IdentityRefreshStatus.UNCHANGED,
                IdentityRefreshStatus.PARTIAL,
            }:
                self._profiles_changed = True
                self._identity_refresh_status.setText(
                    f"{profile_label}: {public_message}"
                )
            elif result.status not in {
                IdentityRefreshStatus.CANCELLED,
            }:
                self._identity_refresh_status.setText(
                    f"{profile_label}: {public_message}"
                )
                QMessageBox.warning(self, "Identity Refresh Not Applied", public_message)
            else:
                self._identity_refresh_status.setText(
                    f"{profile_label}: {public_message}"
                )
        elif outcome is not None and outcome[0] == "failure":
            public_message = str(outcome[1])
            self._identity_refresh_status.setText(
                f"{profile_label}: {public_message}"
            )
            QMessageBox.warning(self, "Identity Refresh Failed", public_message)
        else:
            self._identity_refresh_status.setText(
                f"{profile_label}: {public_message}"
            )
            QMessageBox.warning(self, "Identity Refresh Failed", public_message)

        self._refresh_imported_profiles(selected_profile_id)
        self._update_controls()
        self._complete_identity_close_if_idle()

    def _update_controls(self) -> None:
        copying = self._copy_busy or self._thread is not None
        checking = self._readiness_thread is not None
        refreshing_identity = self._identity_thread is not None
        self._progress.setVisible(copying)
        mutation_busy = copying or refreshing_identity
        self._import_btn.setEnabled(not mutation_busy)
        self._create_btn.setEnabled(not mutation_busy)
        self._close_btn.setEnabled(not mutation_busy and not checking)
        self._tabs.setEnabled(not copying)
        self._refresh_profiles_btn.setEnabled(not copying)
        self._create_runtime_check_btn.setEnabled(not copying)
        self._source_runtime_check_btn.setEnabled(not copying)
        self._create_runtime_manager_btn.setEnabled(not copying)
        self._source_runtime_manager_btn.setEnabled(not copying)
        if refreshing_identity:
            self._finish_orphan_delete_btn.setEnabled(False)
            self._restore_unlinked_btn.setEnabled(False)
            self._delete_unlinked_btn.setEnabled(False)
            self._discard_staging_btn.setEnabled(False)
        else:
            self._on_orphan_delete_selected()
            self._on_unlinked_storage_selected()
            self._on_staging_remnant_selected()
        if self._selected_profile_id() is not None:
            self._on_imported_profile_selected()

    def _record_copy_outcome(
        self,
        thread: _CopyProfileThread,
        kind: str,
        payload: object,
    ) -> None:
        if self._thread is thread:
            self._copy_outcome = (kind, payload)

    def _on_copy_thread_finished(self, thread: _CopyProfileThread) -> None:
        if self._thread is not thread or thread.isRunning():
            return
        outcome = self._copy_outcome
        self._copy_outcome = None
        self._thread = None
        try:
            thread.deleteLater()
        except Exception:
            logger.exception("Could not release completed profile copy worker")
        if outcome is None:
            self._on_import_failed(
                "The profile copy worker stopped without confirming a result. "
                "No managed profile was registered."
            )
            return
        kind, payload = outcome
        if kind == "success" and isinstance(payload, ProfileStorageChange):
            self._on_import_succeeded(payload)
        else:
            self._on_import_failed(str(payload))

    def _release_copy_thread(self) -> bool:
        thread = self._thread
        if thread is not None:
            is_running = getattr(thread, "isRunning", None)
            if callable(is_running) and is_running():
                logger.error("Refused to release a browser profile worker that is still running")
                return False
            try:
                thread.deleteLater()
            except Exception:
                logger.exception("Could not release browser profile copy worker")
        self._thread = None
        self._copy_outcome = None
        self._set_running(False)
        return True

    def _finish_copy_operation(self) -> bool:
        if self._release_copy_thread():
            return self._release_import_maintenance()
        return False

    def _on_import_succeeded(self, change: ProfileStorageChange) -> None:
        pending = self._pending_import
        self._pending_import = None
        if pending is None:
            rollback_error = ""
            try:
                change.rollback()
            except Exception as exc:
                rollback_error = (
                    "\n\nAutomatic rollback could not be confirmed. The copied data "
                    f"was preserved for Recovery at:\n{change.working_path}\n\n{exc}"
                )
                logger.exception("Could not roll back import after copy context was lost")
            finally:
                cleanup_confirmed = self._finish_copy_operation()
                QMessageBox.warning(
                    self,
                    (
                        "Profile Copy Recovery Required"
                        if rollback_error
                        else "Profile Copy Cancelled"
                    ),
                    (
                        "The copy context was lost, so the incomplete managed copy "
                        "was removed."
                        if not rollback_error
                        else "The copy context was lost."
                    )
                    + rollback_error,
                )
                if not cleanup_confirmed:
                    self._warn_maintenance_cleanup_uncertain(applied=False)
            return

        previous_record: StoredBrowserProfile | None = None
        applied_record: StoredBrowserProfile | None = None
        try:
            if pending.existing_profile:
                previous_record = self._db.get_profile(pending.profile_id)
                if previous_record is None:
                    raise RuntimeError("The profile database record no longer exists.")
                if not self._db.finalize_profile_storage(
                    pending.profile_id,
                    engine=pending.engine,
                    browser=pending.identity.browser,
                    source_profile=pending.identity.source_profile or None,
                    account_name=pending.identity.account_name or None,
                    account_email=pending.identity.account_email or None,
                ):
                    raise RuntimeError("The profile database record no longer exists.")
                applied_record = self._db.get_profile(pending.profile_id)
                if applied_record is None:
                    raise RuntimeError("The updated profile database record could not be read.")
            else:
                applied_record = self._db.create_profile(
                    pending.name,
                    profile_id=pending.profile_id,
                    browser=pending.identity.browser,
                    source_profile=pending.identity.source_profile,
                    account_name=pending.identity.account_name,
                    account_email=pending.identity.account_email,
                    engine=pending.engine,
                )
            change.commit()
        except Exception:
            logger.exception("Could not register copied browser profile")
            compensation_failed = False
            try:
                current_record = self._db.get_profile(pending.profile_id)
                if pending.existing_profile:
                    if current_record != previous_record:
                        if (
                            previous_record is None
                            or applied_record is None
                            or not self._db.restore_profile_snapshot(
                                previous_record,
                                expected_current=applied_record,
                            )
                        ):
                            raise RuntimeError(
                                "The original database profile could not be restored."
                            )
                elif current_record is not None:
                    if applied_record is None or not self._db.delete_profile_snapshot(
                        applied_record
                    ):
                        raise RuntimeError(
                            "The newly-created database profile could not be removed."
                        )
            except Exception:
                compensation_failed = True
                logger.exception(
                    "Could not compensate browser profile database mutation"
                )
            rollback_failed = False
            if change.active:
                try:
                    change.rollback()
                except Exception:
                    rollback_failed = True
                    logger.exception("Could not roll back copied browser profile after DB failure")
            operation_applied = compensation_failed or rollback_failed
            message = (
                "The copied profile could not be registered. Review Diagnostics for details."
            )
            if operation_applied:
                message += (
                    " Automatic database or storage recovery could not be confirmed; use the "
                    "Recovery tab before trying again."
                )
            self._status_label.setStyleSheet(f"color: {COLORS['error']};")
            self._status_label.setText(message)
            cleanup_confirmed = self._finish_copy_operation()
            if operation_applied:
                self._profiles_changed = True
                self._refresh_imported_profiles(pending.profile_id)
            QMessageBox.warning(
                self,
                "Profile Copy Failed",
                message,
            )
            if not cleanup_confirmed:
                self._warn_maintenance_cleanup_uncertain(applied=operation_applied)
            return

        cleanup_confirmed = self._finish_copy_operation()
        self._imported_profile = pending.name
        self._profiles_changed = True
        self._refresh_imported_profiles(pending.profile_id)
        self._tabs.setCurrentWidget(self._profiles_page)
        self._status_label.setStyleSheet(f"color: {COLORS['success']};")
        self._status_label.setText(f"Copied '{pending.name}' to:\n{change.path}")
        self._clear_import_target()
        if cleanup_confirmed:
            QMessageBox.information(
                self,
                "Browser Profile Copied",
                f"Copied '{pending.identity.friendly_label(pending.name)}'.\n\n"
                "It is now available in browser device selection.",
            )
        else:
            self._warn_maintenance_cleanup_uncertain(applied=True)

    def _on_import_failed(self, error_msg: str) -> None:
        self._pending_import = None
        cleanup_confirmed = self._finish_copy_operation()
        self._status_label.setStyleSheet(f"color: {COLORS['error']};")
        self._status_label.setText(error_msg)
        QMessageBox.warning(self, "Profile Copy Failed", error_msg)
        if not cleanup_confirmed:
            self._warn_maintenance_cleanup_uncertain(applied=False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._readiness_started:
            self._request_readiness_refresh()

    def _has_active_worker(self) -> bool:
        return (
            self._thread is not None
            or self._readiness_thread is not None
            or self._identity_thread is not None
            or self._pending_import_lease is not None
            or bool(self._retained_maintenance_leases)
        )

    def _request_worker_interruptions(self) -> None:
        for thread in (
            self._thread,
            self._readiness_thread,
            self._identity_thread,
        ):
            if thread is None:
                continue
            request = getattr(thread, "requestInterruption", None)
            if callable(request):
                try:
                    request()
                except Exception:
                    logger.exception("Could not request browser profile worker interruption")

    def _defer_identity_close(self, result: int) -> bool:
        if self._identity_thread is None:
            return False
        self._identity_close_requested = True
        self._identity_close_result = result
        self._identity_refresh_status.setText("Finishing identity refresh…")
        self._request_worker_interruptions()
        return True

    def _complete_identity_close_if_idle(self) -> None:
        if not self._identity_close_requested or self._has_active_worker():
            return
        result = self._identity_close_result
        self._identity_close_requested = False
        QDialog.done(self, result)

    def closeEvent(self, event) -> None:
        if self._has_active_worker():
            self._defer_identity_close(int(QDialog.DialogCode.Rejected))
            self._request_worker_interruptions()
            event.ignore()
            return
        super().closeEvent(event)

    def accept(self) -> None:
        if self._has_active_worker():
            self._defer_identity_close(int(QDialog.DialogCode.Accepted))
            self._request_worker_interruptions()
            return
        super().accept()

    def reject(self) -> None:
        if self._has_active_worker():
            self._defer_identity_close(int(QDialog.DialogCode.Rejected))
            self._request_worker_interruptions()
            return
        super().reject()

    def done(self, result: int) -> None:
        if self._has_active_worker():
            self._defer_identity_close(result)
            self._request_worker_interruptions()
            return
        super().done(result)
