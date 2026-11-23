from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseCleanupError,
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.services.browser_profile_storage import create_clean_profile
from automadin.services.database import DatabaseService
from automadin.services.database_transfer import DatabaseInspection
from automadin.services.portability_bundle import (
    BUNDLE_DATABASE_PATH,
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    BundleDatabase,
    BundleDatabaseProfile,
    BundleProgress,
    BundleProfile,
    LocalProfileCollision,
    OmittedBundleProfile,
    OmittedProfileCollision,
    OmittedProfileCollisionInspection,
    PortabilityBundleInspection,
    PortabilityBundleManifest,
    ProfileCollisionInspection,
)
from automadin.services.portability_staging import ProfileRestoreDecision
from automadin.ui.portability_dialog import (
    PORTABLE_BACKUP_EXTENSION,
    ExportPortableBackupDialog,
    ImportPortableBackupDialog,
    _ExportPortableBackupThread,
    _InspectPortableBackupThread,
    _StagePortableBackupThread,
    _installed_app_version,
    _portable_destination,
)
from automadin.ui import portability_dialog


@pytest.fixture(scope="module")
def application():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_portability_export_uses_shared_packaged_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        portability_dialog,
        "packaged_app_version",
        lambda development_label: "0.2.0",
    )

    assert _installed_app_version() == "0.2.0"


@pytest.fixture
def workspace(application, tmp_path: Path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    monkeypatch.setenv("AUTOMADIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTOMADIN_PROFILES_DIR", str(profiles_root))
    database = DatabaseService(str(tmp_path / "automadin.db"))

    ready_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    with registry.acquire_maintenance(
        ready_id,
        ProfileUse("maintenance", "create portability UI fixture", "ui-fixture-1"),
    ) as lease:
        change = create_clean_profile(
            ready_id,
            "firefox",
            lease_registry=registry,
            maintenance_lease=lease,
            profiles_root=profiles_root,
        )
        (change.path / "prefs.js").write_text("user_pref('ready', true);", encoding="utf-8")
        change.commit()
    ready = database.create_profile(
        "Jane Work",
        profile_id=ready_id,
        browser="Firefox",
        source_profile="default-release",
        account_name="Jane",
        account_email="jane@example.test",
        engine="firefox",
    )
    unavailable = database.create_profile(
        "Needs Setup",
        profile_id=str(uuid.uuid4()),
        legacy_storage_name="Old Profile",
        browser="Chrome",
        source_profile="Default",
        engine="chrome",
    )
    dialogs: list[ExportPortableBackupDialog] = []
    try:
        yield database, ready, unavailable, dialogs
    finally:
        for dialog in dialogs:
            dialog._thread = None
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        database.close()


def _dialog(workspace) -> ExportPortableBackupDialog:
    database, _ready, _unavailable, dialogs = workspace
    dialog = ExportPortableBackupDialog(
        database,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    dialogs.append(dialog)
    return dialog


def _import_dialog(workspace) -> ImportPortableBackupDialog:
    database, _ready, _unavailable, dialogs = workspace
    dialog = ImportPortableBackupDialog(
        database,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    dialogs.append(dialog)  # type: ignore[arg-type]
    return dialog


def _inspection(
    tmp_path: Path,
    outcomes: list[LocalProfileCollision],
    *,
    omitted: bool = False,
    omitted_outcome: OmittedProfileCollision = OmittedProfileCollision.ABSENT,
) -> PortabilityBundleInspection:
    included_profiles: list[BundleProfile] = []
    database_profiles: list[BundleDatabaseProfile] = []
    collisions: list[ProfileCollisionInspection] = []
    for index, outcome in enumerate(outcomes, start=1):
        profile_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"profile-{index}"))
        included_profiles.append(
            BundleProfile(
                profile_id=profile_id,
                display_name=f"Portable Profile {index}",
                engine="firefox",
                created_at="2026-07-18T00:00:00Z",
                total_size=0,
                files=(),
            )
        )
        database_profiles.append(
            BundleDatabaseProfile(
                profile_id=profile_id,
                display_name=f"Portable Profile {index}",
                engine="firefox",
                included=True,
            )
        )
        collisions.append(
            ProfileCollisionInspection(
                profile_id,
                outcome,
                tmp_path / "profiles" / profile_id,
                "Local diagnostic" if outcome is not LocalProfileCollision.ABSENT else "",
            )
        )
    omitted_profiles: tuple[OmittedBundleProfile, ...] = ()
    omitted_collisions: tuple[OmittedProfileCollisionInspection, ...] = ()
    if omitted:
        omitted_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "omitted-profile"))
        omitted_profiles = (
            OmittedBundleProfile(omitted_id, "Not selected during export"),
        )
        database_profiles.append(
            BundleDatabaseProfile(
                omitted_id,
                "Database-only Profile",
                "chromium",
                False,
            )
        )
        omitted_collisions = (
            OmittedProfileCollisionInspection(
                omitted_id,
                omitted_outcome,
                tmp_path / "profiles" / omitted_id,
                "chromium",
                "Omitted local diagnostic"
                if omitted_outcome is not OmittedProfileCollision.ABSENT
                else "",
            ),
        )
    source = tmp_path / f"source{PORTABLE_BACKUP_EXTENSION}"
    return PortabilityBundleInspection(
        path=source,
        manifest=PortabilityBundleManifest(
            format=BUNDLE_FORMAT,
            bundle_version=BUNDLE_VERSION,
            bundle_id=str(uuid.uuid4()),
            created_at="2026-07-18T00:00:00Z",
            source_app_version="0.1.0",
            source_schema_version=2,
            database=BundleDatabase(BUNDLE_DATABASE_PATH, 100, "0" * 64),
            profiles=tuple(included_profiles),
            omitted_profiles=omitted_profiles,
        ),
        database=DatabaseInspection(
            path=tmp_path / "inspected.db",
            schema_version=2,
            table_names=frozenset(),
            flow_count=12,
            image_count=34,
            profile_count=len(database_profiles),
        ),
        database_profiles=tuple(database_profiles),
        collisions=tuple(collisions),
        omitted_collisions=omitted_collisions,
    )


def _row_for(dialog: ExportPortableBackupDialog, profile_id: str) -> int:
    for row in range(dialog._profiles_table.rowCount()):
        item = dialog._profiles_table.item(row, dialog._COL_INCLUDE)
        if item.data(Qt.ItemDataRole.UserRole) == profile_id:
            return row
    raise AssertionError(f"Profile {profile_id} was not listed")


def test_profile_selection_is_meaningful_and_strictly_ready_by_default(
    workspace,
) -> None:
    _database, ready, unavailable, _dialogs = workspace
    dialog = _dialog(workspace)
    ready_row = _row_for(dialog, ready.profile_id)
    unavailable_row = _row_for(dialog, unavailable.profile_id)

    ready_include = dialog._profiles_table.item(ready_row, dialog._COL_INCLUDE)
    unavailable_include = dialog._profiles_table.item(
        unavailable_row, dialog._COL_INCLUDE
    )

    assert ready_include.checkState() == Qt.CheckState.Checked
    assert ready_include.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert unavailable_include.checkState() == Qt.CheckState.Unchecked
    assert not unavailable_include.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert dialog._selected_profile_ids() == (ready.profile_id,)

    assert dialog._profiles_table.item(ready_row, dialog._COL_PROFILE).text() == (
        "Jane Work"
    )
    assert dialog._profiles_table.item(
        ready_row, dialog._COL_BROWSER_PROFILE
    ).text() == "default-release"
    assert "Jane" in dialog._profiles_table.item(
        ready_row, dialog._COL_USER
    ).text()
    assert "jane@example.test" in dialog._profiles_table.item(
        ready_row, dialog._COL_USER
    ).text()
    assert dialog._profiles_table.item(ready_row, dialog._COL_STATUS).text() == "Ready"
    assert dialog._profiles_table.item(ready_row, dialog._COL_SIZE).text() == (
        "Calculated during export"
    )
    assert "Database included" in dialog._selection_summary.text()


def test_sensitive_unencrypted_and_reauthentication_wording_is_visible(
    workspace,
) -> None:
    dialog = _dialog(workspace)
    wording = dialog._warning_label.text().casefold()

    assert "sensitive browser data" in wording
    assert "not encrypted" in wording
    assert "sign in again" in wording
    assert dialog.accessibleName()
    assert dialog._profiles_table.accessibleName()
    assert dialog._profiles_table.accessibleDescription()
    assert dialog._destination_edit.accessibleName()
    assert dialog._status_label.accessibleName()
    assert "not encrypted or authenticated" in dialog._warning_label.text()


def test_database_only_portable_export_remains_available(workspace) -> None:
    _database, ready, _unavailable, _dialogs = workspace
    dialog = _dialog(workspace)
    row = _row_for(dialog, ready.profile_id)
    dialog._profiles_table.item(row, dialog._COL_INCLUDE).setCheckState(
        Qt.CheckState.Unchecked
    )

    assert dialog._selected_profile_ids() == ()
    assert "0 browser profiles selected" in dialog._selection_summary.text()
    assert dialog._export_btn.isEnabled()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("portable", f"portable{PORTABLE_BACKUP_EXTENSION}"),
        (
            f"portable{PORTABLE_BACKUP_EXTENSION}",
            f"portable{PORTABLE_BACKUP_EXTENSION}",
        ),
        (
            "PORTABLE.AUTOMADIN-BUNDLE",
            "PORTABLE.AUTOMADIN-BUNDLE",
        ),
    ],
)
def test_destination_uses_portable_bundle_extension(raw: str, expected: str) -> None:
    assert str(_portable_destination(raw)).endswith(expected)


def test_worker_isolates_service_api_and_forwards_cancel_and_progress(
    workspace, tmp_path: Path
) -> None:
    database, ready, _unavailable, _dialogs = workspace
    registry = BrowserProfileLeaseRegistry()
    result = SimpleNamespace(path=tmp_path / "result.automadin-bundle")
    worker = _ExportPortableBackupThread(
        database,
        result.path,
        (ready.profile_id,),
        registry,
    )
    updates: list[tuple[str, int, int]] = []
    worker.progress_changed.connect(
        lambda phase, completed, total: updates.append((phase, completed, total))
    )

    def fake_export(db, destination, profile_ids, **kwargs):
        assert db is database
        assert destination == result.path
        assert profile_ids == (ready.profile_id,)
        assert kwargs["lease_registry"] is registry
        assert callable(kwargs["cancel"])
        assert kwargs["source_app_version"]
        kwargs["progress"](BundleProgress("exporting_profiles", 25, 100))
        return result

    with patch(
        "automadin.ui.portability_dialog.export_portability_bundle",
        side_effect=fake_export,
    ):
        assert worker._export_bundle() is result

    assert updates == [("exporting_profiles", 25, 100)]


def test_worker_turns_profile_lease_conflict_into_actionable_error(
    workspace, tmp_path: Path
) -> None:
    database, ready, _unavailable, _dialogs = workspace
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(
        ready.profile_id,
        ProfileUse("interactive-browser", "Design browser", "owner-1"),
    )
    requested = ProfileUse("maintenance", "exporting portable backup", "export-1")
    conflict = BrowserProfileInUseError(
        registry.snapshot(ready.profile_id),
        SimpleNamespace(
            profile_id=ready.profile_id,
            mode="maintenance",
            use=requested,
            owner_generation=None,
        ),
    )
    worker = _ExportPortableBackupThread(
        database,
        tmp_path / f"blocked{PORTABLE_BACKUP_EXTENSION}",
        (ready.profile_id,),
        registry,
    )
    failures: list[tuple[str, str, object | None]] = []
    worker.failed.connect(
        lambda title, message, scope: failures.append((title, message, scope))
    )

    with (
        patch(
            "automadin.ui.portability_dialog.export_portability_bundle",
            side_effect=conflict,
        ),
        patch.object(
            database,
            "release_current_thread_connection",
        ) as release_connection,
    ):
        worker.run()

    assert failures
    assert failures[0][0] == "Browser Profile Is In Use"
    assert "Design browser" in failures[0][1]
    assert "Stop that browser activity" in failures[0][1]
    release_connection.assert_called_once_with()
    owner.release()


def test_export_worker_retries_survivor_scope_and_reports_primary_failure(
    workspace, tmp_path: Path
) -> None:
    database, ready, _unavailable, _dialogs = workspace
    registry = BrowserProfileLeaseRegistry()
    scope = registry.acquire_maintenance_many(
        (ready.profile_id,), ProfileUse("maintenance", "failed export", "export-failure")
    )
    primary = RuntimeError("primary export failure")
    primary.__cause__ = BrowserProfileLeaseCleanupError(scope)
    worker = _ExportPortableBackupThread(
        database,
        tmp_path / f"failed{PORTABLE_BACKUP_EXTENSION}",
        (ready.profile_id,),
        registry,
    )
    failures: list[tuple[str, str, object | None]] = []
    worker.failed.connect(
        lambda title, message, retained: failures.append((title, message, retained))
    )

    with (
        patch.object(worker, "_export_bundle", side_effect=primary),
        patch.object(database, "release_current_thread_connection"),
    ):
        worker.run()

    assert failures[0][0] == "Portable Backup Failed"
    assert "cleanup completed on retry" in failures[0][1]
    assert failures[0][2] is None
    assert registry.snapshot(ready.profile_id) is None
    replacement = registry.acquire_maintenance(
        ready.profile_id, ProfileUse("maintenance", "after worker retry", "replacement")
    )
    replacement.release()


def test_export_worker_emits_success_for_completed_cleanup_warning(
    workspace, tmp_path: Path
) -> None:
    database, ready, _unavailable, _dialogs = workspace
    result = SimpleNamespace(
        path=tmp_path / f"created{PORTABLE_BACKUP_EXTENSION}",
        cleanup_error="cleanup uncertain",
        maintenance_cleanup_scope=None,
    )
    worker = _ExportPortableBackupThread(
        database, result.path, (ready.profile_id,), BrowserProfileLeaseRegistry()
    )
    successes: list[object] = []
    failures: list[tuple[str, str, object | None]] = []
    worker.succeeded.connect(successes.append)
    worker.failed.connect(lambda *args: failures.append(args))
    with (
        patch.object(worker, "_export_bundle", return_value=result),
        patch.object(database, "release_current_thread_connection"),
    ):
        worker.run()
    assert successes == [result]
    assert failures == []


def test_export_worker_retries_survivor_scope_on_cancellation(
    workspace, tmp_path: Path
) -> None:
    database, ready, _unavailable, _dialogs = workspace
    registry = BrowserProfileLeaseRegistry()
    scope = registry.acquire_maintenance_many(
        (ready.profile_id,), ProfileUse("maintenance", "cancelled export", "cancel-export")
    )
    cancelled_error = portability_dialog.PortabilityBundleCancelledError("cancelled")
    cancelled_error.__cause__ = BrowserProfileLeaseCleanupError(scope)
    worker = _ExportPortableBackupThread(
        database,
        tmp_path / f"cancelled{PORTABLE_BACKUP_EXTENSION}",
        (ready.profile_id,),
        registry,
    )
    outcomes: list[tuple[str, object | None]] = []
    worker.cancelled.connect(lambda message, retained: outcomes.append((message, retained)))
    with (
        patch.object(worker, "_export_bundle", side_effect=cancelled_error),
        patch.object(database, "release_current_thread_connection"),
    ):
        worker.run()
    assert "cleanup completed on retry" in outcomes[0][0]
    assert outcomes[0][1] is None
    assert registry.snapshot(ready.profile_id) is None


class _RunningThread:
    def __init__(self) -> None:
        self.interruption_requested = False
        self.running = True
        self.deleted = False

    def isRunning(self) -> bool:
        return self.running

    def requestInterruption(self) -> None:
        self.interruption_requested = True

    def deleteLater(self) -> None:
        self.deleted = True

    @staticmethod
    def wait() -> None:
        raise AssertionError("The GUI must never wait for an outcome-emitting worker")


def test_close_is_rejected_while_export_is_active(workspace) -> None:
    dialog = _dialog(workspace)
    thread = _RunningThread()
    dialog._thread = thread  # type: ignore[assignment]
    event = QCloseEvent()

    dialog.closeEvent(event)

    assert not event.isAccepted()
    assert thread.interruption_requested
    assert dialog._thread is thread
    assert "remain open" in dialog._status_label.text()
    dialog._thread = None


def test_outcome_waits_for_finished_before_dropping_worker_ownership(
    workspace, tmp_path: Path
) -> None:
    dialog = _dialog(workspace)
    thread = _RunningThread()
    dialog._thread = thread  # type: ignore[assignment]
    dialog._set_exporting(True)
    result = SimpleNamespace(
        path=tmp_path / f"complete{PORTABLE_BACKUP_EXTENSION}"
    )

    dialog._on_export_succeeded(result)

    assert dialog._thread is thread
    assert dialog.export_active
    assert not thread.deleted
    assert dialog.export_result is None

    close_event = QCloseEvent()
    dialog.closeEvent(close_event)
    assert not close_event.isAccepted()
    assert dialog._thread is thread

    thread.running = False
    with patch("automadin.ui.portability_dialog.QMessageBox.information"):
        dialog._on_thread_finished(thread)  # type: ignore[arg-type]

    assert dialog._thread is None
    assert thread.deleted
    assert dialog.export_result is result
    assert dialog._cancel_btn.isEnabled()
    assert dialog._cancel_btn.text() == "Close"


def test_progress_changes_from_indeterminate_phase_to_bytes(workspace) -> None:
    dialog = _dialog(workspace)

    dialog._on_progress("exporting_database", 0, 0)
    assert dialog._progress.minimum() == 0
    assert dialog._progress.maximum() == 0
    assert "Copying database" in dialog._status_label.text()

    dialog._on_progress("exporting_profiles", 50, 100)
    assert dialog._progress.maximum() == 1000
    assert dialog._progress.value() == 500
    assert "Copying browser profiles" in dialog._progress.format()


def test_import_wording_explains_full_replace_safety_and_sensitive_data(
    workspace,
) -> None:
    dialog = _import_dialog(workspace)
    consequence = dialog._consequence_label.text().casefold()
    warning = dialog._warning_label.text().casefold()

    assert "entire automadin database" in consequence
    assert "replace" in consequence
    assert "safety backup" in consequence
    assert "sensitive browser data" in warning
    assert "not encrypted" in warning
    assert "sign in again" in warning
    assert dialog.accessibleName()
    assert dialog._profiles_table.accessibleName()
    assert dialog._source_edit.accessibleDescription()
    assert "not encrypted or authenticated" in dialog._warning_label.text()
    assert "not who created it" in dialog._warning_label.text()


def test_import_preview_requires_explicit_keep_for_different_local_bytes(
    workspace, tmp_path: Path
) -> None:
    dialog = _import_dialog(workspace)
    inspection = _inspection(
        tmp_path,
        [
            LocalProfileCollision.ABSENT,
            LocalProfileCollision.IDENTICAL,
            LocalProfileCollision.DIFFERENT,
        ],
        omitted=True,
    )

    dialog._show_inspection(inspection)

    assert dialog.source == inspection.path
    assert "12 flows" in dialog._database_summary.text()
    assert "34 images" in dialog._database_summary.text()
    assert "3 included" in dialog._database_summary.text()
    assert "1 omitted" in dialog._database_summary.text()
    assert inspection.manifest.created_at in dialog._database_summary.text()
    assert inspection.manifest.source_app_version in dialog._database_summary.text()
    assert dialog._profiles_table.rowCount() == 4
    assert not dialog._stage_btn.isEnabled()
    assert "Explicitly confirm" in dialog._decision_note.text()

    different_id = inspection.collisions[2].profile_id
    acknowledgement = dialog._different_acknowledgements[different_id]
    assert "Keep this PC's existing data" == acknowledgement.text()
    assert acknowledgement.accessibleDescription()
    acknowledgement.setChecked(True)

    assert dialog._stage_btn.isEnabled()
    assert dialog._restore_decisions() == {
        inspection.collisions[0].profile_id: ProfileRestoreDecision.INSTALL,
        inspection.collisions[1].profile_id: ProfileRestoreDecision.KEEP_LOCAL,
        inspection.collisions[2].profile_id: ProfileRestoreDecision.KEEP_LOCAL,
    }
    omitted_row = dialog._profiles_table.rowCount() - 1
    assert dialog._profiles_table.item(
        omitted_row, dialog._COL_BUNDLE
    ).text() == "Omitted"


@pytest.mark.parametrize(
    "outcome",
    [LocalProfileCollision.ENGINE_MISMATCH, LocalProfileCollision.UNSAFE],
)
def test_import_preview_blocks_unsafe_or_engine_mismatched_profiles(
    workspace,
    tmp_path: Path,
    outcome: LocalProfileCollision,
) -> None:
    dialog = _import_dialog(workspace)
    dialog._show_inspection(_inspection(tmp_path, [outcome]))

    assert not dialog._stage_btn.isEnabled()
    assert dialog._restore_decisions() is None
    assert "Restore is blocked" in dialog._decision_note.text()
    assert dialog._profiles_table.item(0, dialog._COL_ACTION).text() == "Blocked"


def test_omitted_ready_profile_requires_explicit_keep_local_decision(
    workspace, tmp_path: Path
) -> None:
    dialog = _import_dialog(workspace)
    inspection = _inspection(
        tmp_path,
        [],
        omitted=True,
        omitted_outcome=OmittedProfileCollision.READY,
    )

    dialog._show_inspection(inspection)

    omitted_id = inspection.omitted_collisions[0].profile_id
    acknowledgement = dialog._omitted_acknowledgements[omitted_id]
    assert not dialog._stage_btn.isEnabled()
    assert acknowledgement.text() == "Keep this PC's existing profile"
    acknowledgement.setChecked(True)

    assert dialog._stage_btn.isEnabled()
    assert dialog._restore_decisions() == {
        omitted_id: ProfileRestoreDecision.KEEP_LOCAL
    }


@pytest.mark.parametrize(
    "outcome",
    [OmittedProfileCollision.ENGINE_MISMATCH, OmittedProfileCollision.UNSAFE],
)
def test_omitted_profile_mismatch_or_unsafe_storage_blocks_staging(
    workspace,
    tmp_path: Path,
    outcome: OmittedProfileCollision,
) -> None:
    dialog = _import_dialog(workspace)
    inspection = _inspection(
        tmp_path,
        [],
        omitted=True,
        omitted_outcome=outcome,
    )

    dialog._show_inspection(inspection)

    assert not dialog._stage_btn.isEnabled()
    assert dialog._restore_decisions() is None
    assert dialog._profiles_table.item(0, dialog._COL_ACTION).text() == "Blocked"


def test_inspection_worker_requests_full_integrity_and_local_classification(
    tmp_path: Path,
) -> None:
    source = tmp_path / f"inspect{PORTABLE_BACKUP_EXTENSION}"
    result = _inspection(tmp_path, [])
    worker = _InspectPortableBackupThread(source)
    updates: list[tuple[str, int, int]] = []
    worker.progress_changed.connect(
        lambda phase, completed, total: updates.append((phase, completed, total))
    )

    def fake_inspect(path, **kwargs):
        assert path == source
        assert kwargs["classify_local_profiles"] is True
        assert callable(kwargs["cancel"])
        kwargs["progress"](BundleProgress("validating_payloads", 5, 10))
        return result

    with patch(
        "automadin.ui.portability_dialog.inspect_portability_bundle",
        side_effect=fake_inspect,
    ):
        assert worker._inspect_bundle() is result

    assert updates == [("validating_payloads", 5, 10)]


def test_staging_worker_forwards_reviewed_decisions_and_cooperative_callbacks(
    workspace, tmp_path: Path
) -> None:
    database, _ready, _unavailable, _dialogs = workspace
    inspection = _inspection(tmp_path, [LocalProfileCollision.ABSENT])
    decision = {
        inspection.collisions[0].profile_id: ProfileRestoreDecision.INSTALL,
    }
    registry = BrowserProfileLeaseRegistry()
    result = SimpleNamespace(source=inspection.path)
    worker = _StagePortableBackupThread(
        database,
        inspection.path,
        inspection,
        decision,
        registry,
    )
    updates: list[tuple[str, int, int]] = []
    worker.progress_changed.connect(
        lambda phase, completed, total: updates.append((phase, completed, total))
    )

    def fake_stage(source, inspected, active_db_path, decisions, **kwargs):
        assert source == inspection.path
        assert inspected is inspection
        assert active_db_path == database.db_path
        assert decisions == decision
        assert kwargs["lease_registry"] is registry
        assert callable(kwargs["cancel"])
        kwargs["progress"](BundleProgress("staging_database", 20, 100))
        return result

    with patch(
        "automadin.ui.portability_dialog.stage_portability_bundle_import",
        side_effect=fake_stage,
    ):
        assert worker._stage_bundle() is result

    assert updates == [("staging_database", 20, 100)]


def test_staging_worker_retries_survivor_scope_and_reports_primary_failure(
    workspace, tmp_path: Path
) -> None:
    database, ready, _unavailable, _dialogs = workspace
    inspection = _inspection(tmp_path, [LocalProfileCollision.ABSENT])
    registry = BrowserProfileLeaseRegistry()
    scope = registry.acquire_maintenance_many(
        (ready.profile_id,), ProfileUse("maintenance", "failed staging", "stage-failure")
    )
    primary = RuntimeError("primary staging failure")
    primary.__cause__ = BrowserProfileLeaseCleanupError(scope)
    worker = _StagePortableBackupThread(
        database,
        inspection.path,
        inspection,
        {},
        registry,
    )
    failures: list[tuple[str, str, object | None]] = []
    worker.failed.connect(
        lambda title, message, retained: failures.append((title, message, retained))
    )
    with patch.object(worker, "_stage_bundle", side_effect=primary):
        worker.run()

    assert failures[0][0] == "Portable Backup Staging Failed"
    assert "cleanup completed on retry" in failures[0][1]
    assert failures[0][2] is None
    assert registry.snapshot(ready.profile_id) is None


def test_staging_worker_retries_survivor_scope_on_cancellation(
    workspace, tmp_path: Path
) -> None:
    database, ready, _unavailable, _dialogs = workspace
    inspection = _inspection(tmp_path, [LocalProfileCollision.ABSENT])
    registry = BrowserProfileLeaseRegistry()
    scope = registry.acquire_maintenance_many(
        (ready.profile_id,), ProfileUse("maintenance", "cancelled stage", "cancel-stage")
    )
    cancelled_error = portability_dialog.PortabilityBundleCancelledError("cancelled")
    cancelled_error.__cause__ = BrowserProfileLeaseCleanupError(scope)
    worker = _StagePortableBackupThread(
        database, inspection.path, inspection, {}, registry
    )
    outcomes: list[tuple[str, object | None]] = []
    worker.cancelled.connect(lambda message, retained: outcomes.append((message, retained)))
    with patch.object(worker, "_stage_bundle", side_effect=cancelled_error):
        worker.run()
    assert "cleanup completed on retry" in outcomes[0][0]
    assert outcomes[0][1] is None
    assert registry.snapshot(ready.profile_id) is None


def test_uncertain_scope_is_retained_by_application_for_restart(
    application, workspace
) -> None:
    _database, ready, _unavailable, _dialogs = workspace
    registry = BrowserProfileLeaseRegistry()
    scope = registry.acquire_maintenance_many(
        (ready.profile_id,), ProfileUse("maintenance", "restart required", "retained")
    )
    portability_dialog._retain_maintenance_cleanup_scope(scope)
    retained = getattr(application, "_automadin_maintenance_cleanup_scopes")
    assert scope in retained
    assert scope.release()
    retained.remove(scope)


def test_final_confirmation_lists_all_omitted_and_kept_profile_names(
    workspace, tmp_path: Path
) -> None:
    dialog = _import_dialog(workspace)
    inspection = _inspection(
        tmp_path,
        [
            LocalProfileCollision.ABSENT,
            LocalProfileCollision.IDENTICAL,
            LocalProfileCollision.DIFFERENT,
        ],
        omitted=True,
    )
    dialog._show_inspection(inspection)
    dialog._different_acknowledgements[
        inspection.collisions[2].profile_id
    ].setChecked(True)
    decisions = dialog._restore_decisions()
    assert decisions is not None

    details = dialog._staging_confirmation_details(inspection, decisions)

    assert "Database-only Profile" in details
    assert "Portable Profile 2" in details
    assert "Portable Profile 3" in details
    assert "Profiles omitted from portable browser data" in details
    assert "Existing browser profiles kept on this PC" in details


def test_declining_final_confirmation_does_not_start_staging_worker(
    workspace, tmp_path: Path
) -> None:
    dialog = _import_dialog(workspace)
    inspection = _inspection(tmp_path, [LocalProfileCollision.ABSENT])
    dialog._show_inspection(inspection)

    with (
        patch(
            "automadin.ui.portability_dialog.QMessageBox.exec",
            return_value=QMessageBox.StandardButton.No,
        ) as confirm,
        patch(
            "automadin.ui.portability_dialog._StagePortableBackupThread"
        ) as worker_type,
    ):
        dialog._start_staging()

    confirm.assert_called_once()
    worker_type.assert_not_called()
    assert dialog._thread is None
    assert dialog.staged_result is None
    assert "current workspace is unchanged" in dialog._status_label.text()


def test_import_outcome_is_retained_until_finished_and_exposes_staged_result(
    workspace, tmp_path: Path
) -> None:
    dialog = _import_dialog(workspace)
    thread = _RunningThread()
    dialog._thread = thread  # type: ignore[assignment]
    dialog._set_busy(True)
    staged = SimpleNamespace(
        source=tmp_path / f"staged{PORTABLE_BACKUP_EXTENSION}",
        cleanup_paths=(),
    )

    dialog._on_worker_succeeded(staged)

    assert dialog._thread is thread
    assert dialog.import_active
    assert dialog.staged_result is None
    assert not thread.deleted

    close_event = QCloseEvent()
    dialog.closeEvent(close_event)
    assert not close_event.isAccepted()
    assert dialog._thread is thread

    thread.running = False
    with patch("automadin.ui.portability_dialog.QMessageBox.information"):
        dialog._on_thread_finished(thread)  # type: ignore[arg-type]

    assert dialog._thread is None
    assert thread.deleted
    assert dialog.staged_result is staged
    assert dialog.source == staged.source
