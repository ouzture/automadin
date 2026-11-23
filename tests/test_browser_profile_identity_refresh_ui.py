from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from automadin.services.browser_profile_identity_refresh import (
    IdentityRefreshStatus,
    ProfileIdentityRefreshOutcome,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.services.browser_profile_metadata import (
    BrowserProfileIdentity,
    ProfileIdentityField,
)
from automadin.services.browser_profile_storage import create_clean_profile
from automadin.services.database import DatabaseService
from automadin.ui import browser_profile_import_dialog as dialog_module
from automadin.ui.browser_profile_import_dialog import (
    BrowserProfileImportDialog,
    _PendingImport,
)


def _maintenance(profile_id: str):
    registry = BrowserProfileLeaseRegistry()
    lease = registry.acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", "identity UI test", str(uuid.uuid4())),
    )
    return registry, lease


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)


class _DeferredIdentityThread:
    instances = []

    def __init__(self, profile_id, _database, **_kwargs) -> None:
        self.profile_id = profile_id
        self.succeeded = _Signal()
        self.failed = _Signal()
        self.finished = _Signal()
        self.running = False
        self.deleted = 0
        self.interruptions = 0
        self.wait_calls = 0
        type(self).instances.append(self)

    def start(self) -> None:
        self.running = True

    def isRunning(self) -> bool:
        return self.running

    def requestInterruption(self) -> None:
        self.interruptions += 1

    def wait(self, *_args) -> bool:
        self.wait_calls += 1
        raise AssertionError("The GUI thread must not wait for identity refresh")

    def deleteLater(self) -> None:
        self.deleted += 1

    def finish(self, outcome=None) -> None:
        if outcome is not None:
            self.succeeded.emit(outcome)
        self.running = False
        self.finished.emit()


class _UncertainStartIdentityThread(_DeferredIdentityThread):
    def start(self) -> None:
        self.running = True
        raise RuntimeError("raised after native start")


@pytest.fixture
def profile_dialog(tmp_path: Path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setenv("AUTOMADIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTOMADIN_PROFILES_DIR", str(profiles))
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)
    database = DatabaseService(str(tmp_path / "automadin.db"))
    profile_id = str(uuid.uuid4())
    registry, lease = _maintenance(profile_id)
    change = create_clean_profile(
        profile_id,
        "chromium",
        lease_registry=registry,
        maintenance_lease=lease,
        profiles_root=profiles,
    )
    change.commit()
    record = database.create_profile(
        "Work",
        profile_id=profile_id,
        browser="Chromium",
        source_profile="Default",
        account_name="Stored User",
        account_email="stored@example.com",
        engine="chromium",
    )
    dialog = BrowserProfileImportDialog(
        database,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    for row in range(dialog._profiles_table.rowCount()):
        item = dialog._profiles_table.item(row, 0)
        if item.data(Qt.ItemDataRole.UserRole) == profile_id:
            dialog._profiles_table.selectRow(row)
            break
    app.processEvents()
    yield dialog, database, record, app
    if not dialog._has_active_worker():
        dialog.close()
    dialog.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    database.close()


def test_identity_status_literals_and_stale_details(profile_dialog) -> None:
    dialog, database, record, _app = profile_dialog
    assert dialog._identity_checked_summary(record) == "Never checked"

    database._conn.execute(
        "UPDATE profiles SET identity_checked_at = ?, identity_stale_mask = ? "
        "WHERE profile_id = ?",
        ("2026-07-20T10:30:00+00:00", int(ProfileIdentityField.ACCOUNT_EMAIL), record.profile_id),
    )
    database._conn.commit()
    dialog._refresh_imported_profiles(record.profile_id)
    current = database.get_profile(record.profile_id)
    assert current is not None
    assert dialog._identity_checked_summary(current).startswith("Partial ")
    details = dialog._selected_profile_details.text()
    assert "stored@example.com (stored value, not verified)" in details
    assert "Identity: Chromium / Default" not in details
    assert not details.startswith(record.display_label)
    assert "Created:" in details
    assert "After signing in or changing browser accounts" in details

    database._conn.execute(
        "UPDATE profiles SET identity_stale_mask = ? WHERE profile_id = ?",
        (int(ProfileIdentityField.ALL), record.profile_id),
    )
    database._conn.commit()
    assert dialog._identity_checked_summary(
        database.get_profile(record.profile_id)
    ).startswith("Could not verify ")


def test_failed_storage_inspection_does_not_repeat_stale_identity(
    profile_dialog,
) -> None:
    dialog, _database, record, _app = profile_dialog
    dialog._profile_inspections[record.profile_id] = None

    dialog._on_imported_profile_selected()

    details = dialog._selected_profile_details.text()
    assert details.startswith(record.name + "\n")
    assert record.identity.account_name not in details
    assert record.identity.account_email not in details


def test_refresh_eligibility_selection_and_runtime_coexistence(profile_dialog) -> None:
    dialog, _database, record, _app = profile_dialog
    assert dialog._refresh_profiles_btn.text() == "Refresh list & runtime"
    assert (
        dialog._profiles_table.horizontalHeaderItem(7).text() == "Identity checked"
    )
    assert dialog._refresh_identity_btn.isEnabled()
    dialog._profiles_table.clearSelection()
    assert not dialog._refresh_identity_btn.isEnabled()
    dialog._refresh_imported_profiles(record.profile_id)
    dialog._readiness_thread = object()  # type: ignore[assignment]
    dialog._on_imported_profile_selected()
    assert dialog._refresh_identity_btn.isEnabled()
    dialog._readiness_thread = None


def test_identity_worker_rejects_duplicates_stale_signals_and_never_waits(
    profile_dialog,
    monkeypatch,
) -> None:
    dialog, _database, record, _app = profile_dialog
    _DeferredIdentityThread.instances = []
    monkeypatch.setattr(
        dialog_module, "_ProfileIdentityRefreshThread", _DeferredIdentityThread
    )
    dialog._refresh_selected_identity()
    worker = _DeferredIdentityThread.instances[-1]
    assert "Work" in dialog._identity_refresh_status.text()
    dialog._profiles_table.clearSelection()
    dialog._refresh_selected_identity()
    assert len(_DeferredIdentityThread.instances) == 1
    stale = _DeferredIdentityThread(record.profile_id, None)
    stale.succeeded.emit(
        ProfileIdentityRefreshOutcome(
            record.profile_id,
            IdentityRefreshStatus.UPDATED,
            "2026-07-20T10:00:00Z",
        )
    )
    assert dialog._identity_outcome is None
    worker.finish(
        ProfileIdentityRefreshOutcome(
            record.profile_id,
            IdentityRefreshStatus.UNCHANGED,
            "2026-07-20T10:00:00Z",
            message="Identity checked; stored values are current.",
        )
    )
    assert worker.wait_calls == 0
    assert worker.deleted == 1
    assert dialog.profiles_changed
    assert dialog._identity_refresh_status.text().startswith("Work:")


def test_identity_refresh_disables_recovery_mutations(profile_dialog, monkeypatch) -> None:
    dialog, _database, _record, _app = profile_dialog
    _DeferredIdentityThread.instances = []
    monkeypatch.setattr(
        dialog_module, "_ProfileIdentityRefreshThread", _DeferredIdentityThread
    )
    for button in (
        dialog._finish_orphan_delete_btn,
        dialog._restore_unlinked_btn,
        dialog._delete_unlinked_btn,
        dialog._discard_staging_btn,
    ):
        button.setEnabled(True)

    dialog._refresh_selected_identity()

    assert not dialog._finish_orphan_delete_btn.isEnabled()
    assert not dialog._restore_unlinked_btn.isEnabled()
    assert not dialog._delete_unlinked_btn.isEnabled()
    assert not dialog._discard_staging_btn.isEnabled()


def test_cleanup_failed_warns_that_cleanup_is_uncertain(
    profile_dialog,
    monkeypatch,
) -> None:
    dialog, _database, record, _app = profile_dialog
    _DeferredIdentityThread.instances = []
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dialog_module, "_ProfileIdentityRefreshThread", _DeferredIdentityThread
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )

    dialog._refresh_selected_identity()
    worker = _DeferredIdentityThread.instances[-1]
    worker.finish(
        ProfileIdentityRefreshOutcome(
            record.profile_id,
            IdentityRefreshStatus.CLEANUP_FAILED,
            "2026-07-20T10:00:00Z",
            message="Identity saved; cleanup could not be confirmed.",
        )
    )

    assert warnings
    assert warnings[-1][0] == "Identity Saved; Cleanup Uncertain"
    assert dialog.profiles_changed


def test_close_is_deferred_then_completed_after_exact_identity_worker(
    profile_dialog,
    monkeypatch,
) -> None:
    dialog, _database, record, _app = profile_dialog
    _DeferredIdentityThread.instances = []
    monkeypatch.setattr(
        dialog_module, "_ProfileIdentityRefreshThread", _DeferredIdentityThread
    )
    dialog._refresh_selected_identity()
    worker = _DeferredIdentityThread.instances[-1]
    dialog.accept()
    assert dialog._identity_close_requested
    assert worker.interruptions >= 1
    assert "Finishing identity refresh" in dialog._identity_refresh_status.text()
    worker.finish(
        ProfileIdentityRefreshOutcome(
            record.profile_id,
            IdentityRefreshStatus.CANCELLED,
            "",
            message="Identity refresh was cancelled.",
        )
    )
    assert dialog.result() == int(QDialog.DialogCode.Accepted)
    assert worker.wait_calls == 0


def test_deferred_close_waits_for_concurrent_readiness_worker(
    profile_dialog,
    monkeypatch,
) -> None:
    dialog, _database, record, _app = profile_dialog
    _DeferredIdentityThread.instances = []
    monkeypatch.setattr(
        dialog_module, "_ProfileIdentityRefreshThread", _DeferredIdentityThread
    )
    dialog._refresh_selected_identity()
    worker = _DeferredIdentityThread.instances[-1]

    readiness = _DeferredIdentityThread("readiness", None)
    dialog._readiness_thread = readiness  # type: ignore[assignment]
    dialog.accept()
    worker.finish(
        ProfileIdentityRefreshOutcome(
            record.profile_id,
            IdentityRefreshStatus.CANCELLED,
            "",
            message="Identity refresh was cancelled.",
        )
    )
    assert dialog._identity_close_requested
    assert dialog.result() != int(QDialog.DialogCode.Accepted)

    readiness.running = False
    dialog._readiness_outcome = None
    dialog._on_readiness_thread_finished(readiness)  # type: ignore[arg-type]
    assert dialog.result() == int(QDialog.DialogCode.Accepted)


def test_uncertain_start_retains_worker_and_blocks_duplicate(profile_dialog, monkeypatch) -> None:
    dialog, _database, _record, _app = profile_dialog
    _UncertainStartIdentityThread.instances = []
    monkeypatch.setattr(
        dialog_module,
        "_ProfileIdentityRefreshThread",
        _UncertainStartIdentityThread,
    )
    dialog._refresh_selected_identity()
    worker = _UncertainStartIdentityThread.instances[-1]
    assert dialog._identity_thread is worker
    assert dialog._identity_start_uncertain
    dialog._refresh_selected_identity()
    assert len(_UncertainStartIdentityThread.instances) == 1
    assert worker.wait_calls == 0
    worker.finish(
        ProfileIdentityRefreshOutcome(
            worker.profile_id,
            IdentityRefreshStatus.CANCELLED,
            "",
            message="Identity refresh was cancelled.",
        )
    )


def test_existing_profile_copy_repair_does_not_erase_stored_identity(
    profile_dialog,
) -> None:
    dialog, database, record, _app = profile_dialog

    class _CommittedChange:
        active = True
        path = Path("managed")

        def commit(self) -> None:
            self.active = False

        def rollback(self) -> None:
            self.active = False

    dialog._pending_import = _PendingImport(
        profile_id=record.profile_id,
        name=record.name,
        engine="chromium",
        identity=BrowserProfileIdentity(browser="Chromium"),
        existing_profile=True,
    )
    dialog._on_import_succeeded(_CommittedChange())  # type: ignore[arg-type]
    repaired = database.get_profile(record.profile_id)
    assert repaired is not None
    assert repaired.identity.source_profile == "Default"
    assert repaired.identity.account_name == "Stored User"
    assert repaired.identity.account_email == "stored@example.com"
