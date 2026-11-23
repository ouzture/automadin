from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from automadin import app
from automadin.services import (
    browser_profile_paths,
    database_transfer,
    portability_activation,
)
from automadin.services.browser_profile_leases import BrowserProfileLeaseRegistry
from automadin.services.database import DatabaseService
from automadin.services.database_transfer import DatabaseInspectionCancelledError
from automadin.ui import main_window
from automadin.ui.browser_profile_import_dialog import BrowserProfileImportDialog
from automadin.ui.setup_center_view import SetupCenterView


@pytest.fixture(scope="module")
def application():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_portable_activation_takes_precedence_over_database_only_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portable_result = SimpleNamespace(applied=True)
    database_calls: list[Path] = []
    profile_root = tmp_path / "profiles"

    monkeypatch.setattr(
        browser_profile_paths,
        "managed_profiles_root",
        lambda: profile_root,
    )
    monkeypatch.setattr(
        portability_activation,
        "apply_pending_portability_activation",
        lambda data_dir, *, profiles_root: (
            portable_result
            if Path(data_dir) == tmp_path and profiles_root == profile_root
            else pytest.fail("unexpected activation arguments")
        ),
    )
    monkeypatch.setattr(
        database_transfer,
        "apply_pending_database_import",
        lambda path: database_calls.append(Path(path)),
    )

    portable, database = app._apply_pending_startup_imports(
        tmp_path,
        tmp_path / "automadin.db",
    )

    assert portable is portable_result
    assert database is None
    assert database_calls == []


def test_database_only_import_runs_when_no_portable_plan_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_result = SimpleNamespace(applied=True)
    database_path = tmp_path / "automadin.db"

    monkeypatch.setattr(
        browser_profile_paths,
        "managed_profiles_root",
        lambda: tmp_path / "profiles",
    )
    monkeypatch.setattr(
        portability_activation,
        "apply_pending_portability_activation",
        lambda _data_dir, *, profiles_root: None,
    )
    monkeypatch.setattr(
        database_transfer,
        "apply_pending_database_import",
        lambda path: database_result if Path(path) == database_path else None,
    )

    portable, database = app._apply_pending_startup_imports(tmp_path, database_path)

    assert portable is None
    assert database is database_result


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, False),
        (SimpleNamespace(applied=False), False),
        (
            SimpleNamespace(applied=False, database_activated=False),
            False,
        ),
        (
            SimpleNamespace(
                applied=False,
                active_database_matches_import=False,
            ),
            False,
        ),
        (
            SimpleNamespace(
                applied=False,
                active_database_matches_import=True,
            ),
            True,
        ),
        (
            SimpleNamespace(applied=False, database_activated=True),
            True,
        ),
        (SimpleNamespace(applied=True), True),
    ],
    ids=[
        "no-result",
        "failed-generic",
        "failed-portable",
        "failed-database-only",
        "partial-database-only-db-active",
        "partial-portable-db-active",
        "complete-import",
    ],
)
def test_imported_database_activation_truth(
    result: object | None,
    expected: bool,
) -> None:
    assert app._imported_database_is_active(result) is expected


class _MessageRecorder:
    calls: list[tuple[str, str, str]] = []

    @classmethod
    def information(cls, _parent, title: str, message: str) -> None:
        cls.calls.append(("information", title, message))

    @classmethod
    def warning(cls, _parent, title: str, message: str) -> None:
        cls.calls.append(("warning", title, message))


class _CleanupScope:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.released = False
        self.release_calls = 0

    def release(self) -> bool:
        self.release_calls += 1
        if self.fail:
            raise RuntimeError("cleanup remains blocked")
        self.released = True
        return True


def test_portable_startup_success_explains_backup_and_reauthentication() -> None:
    _MessageRecorder.calls = []
    result = SimpleNamespace(
        applied=True,
        backup_path=Path("previous-database.db"),
    )

    app._show_startup_import_result(object(), _MessageRecorder, result, None)

    assert len(_MessageRecorder.calls) == 1
    kind, title, message = _MessageRecorder.calls[0]
    assert kind == "information"
    assert title == "Portable Backup Restored"
    assert "safety backup" in message
    assert "previous-database.db" in message
    assert "sign in again" in message
    assert "Tools > Browser Profiles" in message


def test_applied_portable_startup_retries_and_reports_cleanup_completion() -> None:
    _MessageRecorder.calls = []
    scope = _CleanupScope()
    result = SimpleNamespace(
        applied=True,
        error="initial maintenance cleanup failed",
        maintenance_cleanup_scope=scope,
    )

    app._show_startup_import_result(object(), _MessageRecorder, result, None)

    assert scope.release_calls == 1
    assert scope.released
    kind, title, message = _MessageRecorder.calls[0]
    assert kind == "warning"
    assert title == "Portable Backup Restored; Cleanup Completed"
    assert "workspace and imported database are active" in message
    assert "released it successfully" in message


def test_applied_portable_startup_retains_scope_and_requires_restart_on_retry_failure() -> None:
    _MessageRecorder.calls = []
    scope = _CleanupScope(fail=True)
    result = SimpleNamespace(
        applied=True,
        error="initial maintenance cleanup failed",
        maintenance_cleanup_scope=scope,
    )

    app._show_startup_import_result(object(), _MessageRecorder, result, None)

    assert result.maintenance_cleanup_scope is scope
    assert scope.release_calls == 1
    assert not scope.released
    kind, title, message = _MessageRecorder.calls[0]
    assert kind == "warning"
    assert title == "Portable Backup Restored; Restart Required"
    assert "workspace and imported database are active" in message
    assert "Restart Automadin" in message
    assert "cleanup remains blocked" in message


def test_portable_startup_failure_preserves_recovery_without_overclaiming() -> None:
    _MessageRecorder.calls = []
    rejected = Path("rejected.db")
    safety_backup = Path("automadin-before-import.db")
    result = SimpleNamespace(
        applied=False,
        error="verification failed",
        recovery_paths=(Path("recovery-profile"), safety_backup, rejected),
        backup_path=safety_backup,
        rejected_path=rejected,
        failed_plan_path=Path("failed-plan.json"),
        unresolved_paths=(Path("unresolved-private.db"),),
        database_activated=False,
    )

    app._show_startup_import_result(object(), _MessageRecorder, result, None)

    assert len(_MessageRecorder.calls) == 1
    kind, title, message = _MessageRecorder.calls[0]
    assert kind == "warning"
    assert title == "Portable Backup Restore Failed"
    assert message.startswith(
        "Automadin stopped the restore and preserved all recoverable data."
    )
    assert "did not become active" in message
    assert "existing database was kept unchanged" not in message
    assert "verification failed" in message
    assert message.count("automadin-before-import.db") == 1
    assert message.count("rejected.db") == 1
    assert "recovery-profile" in message
    assert "failed-plan.json" in message
    assert "unresolved-private.db" in message


def test_portable_partial_activation_reports_that_imported_database_is_active() -> None:
    _MessageRecorder.calls = []
    result = SimpleNamespace(
        applied=False,
        database_activated=True,
        error="profile publication failed",
        recovery_paths=(Path("recover-profile"),),
        backup_path=Path("previous-database.db"),
    )

    app._show_startup_import_result(object(), _MessageRecorder, result, None)

    assert len(_MessageRecorder.calls) == 1
    kind, title, message = _MessageRecorder.calls[0]
    assert kind == "warning"
    assert title == "Portable Backup Needs Recovery"
    assert "imported Automadin database is active" in message
    assert "browser profile restore did not complete" in message
    assert "profile publication failed" in message
    assert "recover-profile" in message
    assert "previous-database.db" in message


def test_database_only_partial_activation_reports_truth_and_recovery_paths() -> None:
    _MessageRecorder.calls = []
    result = SimpleNamespace(
        applied=False,
        active_database_matches_import=True,
        error="post-activation verification failed",
        backup_path=Path("previous-database.db"),
        unresolved_paths=(Path("pending-sidecar"),),
    )

    app._show_startup_import_result(object(), _MessageRecorder, None, result)

    kind, title, message = _MessageRecorder.calls[0]
    assert kind == "warning"
    assert title == "Database Import Needs Recovery"
    assert "imported Automadin database is active" in message
    assert "post-activation verification failed" in message
    assert "previous-database.db" in message
    assert "pending-sidecar" in message


def test_database_only_failure_does_not_claim_a_preexisting_database() -> None:
    _MessageRecorder.calls = []
    result = SimpleNamespace(
        applied=False,
        active_database_matches_import=False,
        error="invalid staged database",
        rejected_path=Path("rejected.db"),
    )

    app._show_startup_import_result(object(), _MessageRecorder, None, result)

    kind, title, message = _MessageRecorder.calls[0]
    assert kind == "warning"
    assert title == "Database Import Failed"
    assert "did not become active" in message
    assert "existing database was kept unchanged" not in message
    assert "rejected.db" in message


def test_file_menu_presents_portable_and_database_only_workflows_in_order(
    application: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOMADIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTOMADIN_PROFILES_DIR", str(tmp_path / "profiles"))
    database = DatabaseService(str(tmp_path / "automadin.db"))
    window = main_window.MainWindow(database)
    try:
        menu_bar = window.menuBar()
        file_action = menu_bar.actions()[0]
        file_menu = file_action.menu()
        assert file_menu is not None
        file_actions = file_menu.actions()
        labels = [
            "<separator>" if action.isSeparator() else action.text()
            for action in file_actions
        ]
        assert labels[:8] == [
            "Export Portable Backup...",
            "Import Portable Backup...",
            "<separator>",
            "Export Database Only...",
            "Import Database Only...",
            "Backup Help...",
            "<separator>",
            "E&xit",
        ]
    finally:
        window.deleteLater()
        database.close()


def test_portability_dialogs_receive_the_session_lease_registry(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BrowserProfileLeaseRegistry()
    captured: list[tuple[str, object]] = []
    prompts: list[tuple[str, str]] = []

    class _ExportDialog:
        def __init__(self, _db, _parent, *, lease_registry) -> None:
            captured.append(("export", lease_registry))

        def exec(self) -> None:
            return None

    class _ImportDialog:
        staged_result = SimpleNamespace(source=Path("backup.automadin-bundle"))

        def __init__(self, _db, _parent, *, lease_registry) -> None:
            captured.append(("import", lease_registry))

        def exec(self) -> None:
            return None

    closed: list[bool] = []
    window = SimpleNamespace(
        _db=object(),
        _session_service=SimpleNamespace(browser_profile_lease_registry=registry),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(main_window, "ExportPortableBackupDialog", _ExportDialog)
    monkeypatch.setattr(main_window, "ImportPortableBackupDialog", _ImportDialog)
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda _parent, title, message, _buttons: (
            prompts.append((title, message)) or QMessageBox.StandardButton.Yes
        ),
    )

    main_window.MainWindow._show_export_portable_backup(window)  # type: ignore[arg-type]
    main_window.MainWindow._show_import_portable_backup(window)  # type: ignore[arg-type]

    assert captured == [("export", registry), ("import", registry)]
    assert closed == [True]
    assert prompts[0][0] == "Restart to Restore Portable Backup"
    assert "edits you make before restarting will not become part" in prompts[0][1]
    assert "automatic safety backup" in prompts[0][1]


def test_database_only_ready_prompt_explains_post_staging_edits(
    application: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_database = tmp_path / "automadin.db"
    selected_database = tmp_path / "selected.db"
    prompts: list[tuple[str, str]] = []
    staged: list[Path] = []
    window = SimpleNamespace(
        _db=SimpleNamespace(db_path=str(active_database)),
        _start_database_staging=lambda source: staged.append(source),
    )
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda _parent, title, message, _buttons: (
            prompts.append((title, message)) or QMessageBox.StandardButton.Yes
        ),
    )

    main_window.MainWindow._confirm_database_import(  # type: ignore[arg-type]
        window,
        selected_database,
        SimpleNamespace(flow_count=3, image_count=4, profile_count=5),
    )

    assert staged == [selected_database]
    assert prompts[-1][0] == "Import Database Only"


def test_database_inspection_worker_forwards_cooperative_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "selected.db"
    inspection = SimpleNamespace(flow_count=3, image_count=4, profile_count=5)
    worker = main_window._DatabaseInspectionThread(source)

    def fake_inspect(path, *, cancel):
        assert path == source
        assert callable(cancel)
        assert not cancel()
        return inspection

    monkeypatch.setattr(main_window, "inspect_database", fake_inspect)

    worker.run()

    assert worker.inspection is inspection
    assert worker.error is None
    assert not worker.cancelled


def test_database_inspection_worker_reports_cancellation_without_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = main_window._DatabaseInspectionThread(tmp_path / "selected.db")
    monkeypatch.setattr(
        main_window,
        "inspect_database",
        lambda _path, *, cancel: (
            pytest.fail("cancel callback missing")
            if not callable(cancel)
            else (_ for _ in ()).throw(
                DatabaseInspectionCancelledError("cancelled")
            )
        ),
    )

    worker.run()

    assert worker.cancelled
    assert worker.inspection is None
    assert worker.error is None


def test_late_database_review_cancellation_wins_before_confirmation(
    tmp_path: Path,
) -> None:
    class _LateCancelledInspection(main_window._DatabaseInspectionThread):
        def isInterruptionRequested(self) -> bool:
            return True

    class _Progress:
        def close(self) -> None:
            return None

        def deleteLater(self) -> None:
            return None

    worker = _LateCancelledInspection(tmp_path / "selected.db")
    worker.inspection = SimpleNamespace(flow_count=3, image_count=4, profile_count=5)
    progress = _Progress()
    messages: list[str] = []
    window = SimpleNamespace(
        _database_import_thread=worker,
        _database_import_progress=progress,
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda message, _duration: messages.append(message)
        ),
        _confirm_database_import=lambda *_args: pytest.fail(
            "A late cancellation must suppress confirmation"
        ),
    )

    main_window.MainWindow._finish_database_inspection(  # type: ignore[arg-type]
        window,
        worker,
        progress,  # type: ignore[arg-type]
    )

    assert window._database_import_thread is None
    assert "cancelled" in messages[-1]


def test_database_staging_worker_runs_off_ui_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "selected.db"
    active = tmp_path / "automadin.db"
    staged: list[tuple[Path, Path]] = []
    worker = main_window._DatabaseStagingThread(source, active)
    monkeypatch.setattr(
        main_window,
        "stage_database_import",
        lambda selected, database: staged.append((selected, database)),
    )

    worker.run()

    assert staged == [(source, active)]
    assert worker.staged
    assert worker.error is None


def test_database_staging_completion_explains_post_staging_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[tuple[str, str]] = []

    class _FinishedStage:
        staged = True
        error = None

        def deleteLater(self) -> None:
            return None

    class _Progress:
        def close(self) -> None:
            return None

        def deleteLater(self) -> None:
            return None

    thread = _FinishedStage()
    progress = _Progress()
    window = SimpleNamespace(
        _database_import_thread=thread,
        _database_import_progress=progress,
        close=lambda: pytest.fail("No should leave the window open"),
    )
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda _parent, title, message, _buttons: (
            prompts.append((title, message)) or QMessageBox.StandardButton.No
        ),
    )

    main_window.MainWindow._finish_database_staging(  # type: ignore[arg-type]
        window,
        thread,  # type: ignore[arg-type]
        progress,  # type: ignore[arg-type]
    )

    assert window._database_import_thread is None
    assert prompts[-1][0] == "Import Ready"
    assert "edits you make before restarting will not become part" in prompts[-1][1]
    assert "automatic safety backup" in prompts[-1][1]


def test_close_waits_for_database_review_and_requests_cancellation(
    tmp_path: Path,
) -> None:
    class _ControllableInspection(main_window._DatabaseInspectionThread):
        interruption_requested = False

        def requestInterruption(self) -> None:
            self.interruption_requested = True

    worker = _ControllableInspection(tmp_path / "selected.db")
    messages: list[str] = []
    window = SimpleNamespace(
        _database_import_thread=worker,
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda message, _duration: messages.append(message)
        ),
    )
    event = QCloseEvent()

    main_window.MainWindow.closeEvent(window, event)  # type: ignore[arg-type]

    assert not event.isAccepted()
    assert worker.interruption_requested
    assert "cancellation was requested" in messages[-1]


def test_setup_center_offers_portable_restore_without_hiding_database_only(
    application: QApplication,
    tmp_path: Path,
) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"))
    view = SetupCenterView(database, tmp_path)
    actions: list[str] = []
    view.action_requested.connect(actions.append)
    try:
        buttons = {
            button.text(): button for button in view.findChildren(QPushButton)
        }
        buttons["Restore portable backup"].click()
        buttons["Import database only"].click()

        assert actions == ["restore_portable", "import_database"]
    finally:
        view.deleteLater()
        application.processEvents()
        database.close()


def test_browser_profile_copy_wording_is_unambiguous(
    application: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = tmp_path / "profiles"
    monkeypatch.setenv("AUTOMADIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTOMADIN_PROFILES_DIR", str(profiles))
    database = DatabaseService(str(tmp_path / "automadin.db"))
    dialog = BrowserProfileImportDialog(
        database,
        lease_registry=BrowserProfileLeaseRegistry(),
    )
    try:
        index = dialog._tabs.indexOf(dialog._import_page)
        assert dialog._tabs.tabText(index) == "Copy from Browser"
        assert dialog._import_btn.text() == "Copy Browser Profile"
        assert dialog._import_copy_btn.text() == "Copy from Browser…"
        assert dialog._new_import_target_btn.text() == "Copy as New Profile"
    finally:
        dialog.close()
        dialog.deleteLater()
        database.close()
