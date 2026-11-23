from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from automadin.services.browser_profile_leases import BrowserProfileLeaseRegistry
from automadin.ui import main_window, portability_dialog
from automadin.ui.main_window import (
    MainWindow,
    _DatabaseInspectionThread,
    _DatabaseStagingThread,
)
from automadin.ui.portability_dialog import (
    ExportPortableBackupDialog,
    ImportPortableBackupDialog,
    _ExportPortableBackupThread,
    _InspectPortableBackupThread,
    _StagePortableBackupThread,
)


@pytest.fixture(scope="module")
def application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


class _Signal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _StartFailureWorker:
    def __init__(self, *, remains_running: bool) -> None:
        self.progress_changed = _Signal()
        self.succeeded = _Signal()
        self.failed = _Signal()
        self.cancelled = _Signal()
        self.finished = _Signal()
        self.remains_running = remains_running
        self.deleted = False

    def start(self) -> None:
        raise OSError("private worker startup detail")

    def isRunning(self) -> bool:
        return self.remains_running

    def deleteLater(self) -> None:
        self.deleted = True

    def requestInterruption(self) -> None:
        pass


class _Label:
    def __init__(self) -> None:
        self.value = ""

    def setText(self, value: str) -> None:
        self.value = value


class _Progress:
    def __init__(self) -> None:
        self.canceled = _Signal()
        self.closed = False
        self.deleted = False
        self.label = ""

    def close(self) -> None:
        self.closed = True

    def deleteLater(self) -> None:
        self.deleted = True

    def setLabelText(self, value: str) -> None:
        self.label = value


@pytest.mark.parametrize(
    ("worker_factory", "patch_target", "expected_message", "log_message"),
    [
        (
            lambda: _DatabaseInspectionThread(Path("unsafe.db")),
            "inspect_database",
            "The selected database could not be inspected safely. "
            "Review Diagnostics for details.",
            "Database-only import inspection failed",
        ),
        (
            lambda: _DatabaseStagingThread(
                Path("unsafe.db"),
                Path("active.db"),
            ),
            "stage_database_import",
            "The database could not be staged safely. "
            "Review Diagnostics for details.",
            "Database-only import staging failed",
        ),
    ],
)
def test_database_workers_log_private_failures_and_store_safe_messages(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    worker_factory,
    patch_target: str,
    expected_message: str,
    log_message: str,
) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("private database failure detail")

    monkeypatch.setattr(main_window, patch_target, fail)
    worker = worker_factory()

    with caplog.at_level(logging.ERROR, logger=main_window.__name__):
        worker.run()

    assert worker.error == expected_message
    assert "private database failure detail" not in worker.error
    assert log_message in caplog.text
    assert "private database failure detail" in caplog.text


@pytest.mark.parametrize(
    ("worker", "method_name", "expected_title", "expected_message", "log_message"),
    [
        (
            _InspectPortableBackupThread(Path("unsafe.automadin-backup")),
            "_inspect_bundle",
            "Portable Backup Is Not Valid",
            "Automadin could not safely inspect this portable backup. Nothing "
            "was staged or changed. Review Diagnostics for details.",
            "Portable backup inspection failed",
        ),
        (
            _StagePortableBackupThread(
                SimpleNamespace(db_path=Path("active.db")),
                Path("unsafe.automadin-backup"),
                SimpleNamespace(),
                {},
                BrowserProfileLeaseRegistry(),
            ),
            "_stage_bundle",
            "Portable Backup Staging Failed",
            "Nothing was activated. Review Diagnostics for details before "
            "trying again.",
            "Portable backup staging failed",
        ),
    ],
)
def test_portability_workers_log_private_failures_and_emit_safe_messages(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    worker,
    method_name: str,
    expected_title: str,
    expected_message: str,
    log_message: str,
) -> None:
    def fail():
        raise RuntimeError("private portability failure detail")

    monkeypatch.setattr(worker, method_name, fail)
    failures: list[tuple[str, str]] = []
    worker.failed.connect(
        lambda title, message: failures.append((title, message)),
        Qt.ConnectionType.DirectConnection,
    )

    with caplog.at_level(logging.ERROR, logger=portability_dialog.__name__):
        worker.run()

    assert failures == [(expected_title, expected_message)]
    assert "private portability failure detail" not in failures[0][1]
    assert log_message in caplog.text
    assert "private portability failure detail" in caplog.text


def test_export_worker_reports_safe_failure_and_protects_database_release(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = SimpleNamespace(
        release_current_thread_connection=lambda: (_ for _ in ()).throw(
            RuntimeError("private release failure detail")
        )
    )
    worker = _ExportPortableBackupThread(
        database,
        Path("backup.automadin-backup"),
        (),
        BrowserProfileLeaseRegistry(),
    )

    def fail():
        raise RuntimeError("private export failure detail")

    monkeypatch.setattr(worker, "_export_bundle", fail)
    failures: list[tuple[str, str]] = []
    worker.failed.connect(
        lambda title, message: failures.append((title, message)),
        Qt.ConnectionType.DirectConnection,
    )

    with caplog.at_level(logging.ERROR, logger=portability_dialog.__name__):
        worker.run()

    assert failures == [
        (
            "Portable Backup Failed",
            "The portable backup was not created. Review Diagnostics for "
            "details, then try again.",
        )
    ]
    assert "private export failure detail" not in failures[0][1]
    assert "Portable backup export failed" in caplog.text
    assert "private export failure detail" in caplog.text
    assert "Could not release portable export database connection" in caplog.text
    assert "private release failure detail" in caplog.text


@pytest.mark.parametrize("remains_running", [False, True])
def test_database_inspection_start_rolls_back_or_retains_live_worker(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    remains_running: bool,
) -> None:
    worker = _StartFailureWorker(remains_running=remains_running)
    progress = _Progress()
    critical: list[tuple[str, str]] = []
    window = SimpleNamespace(
        _database_import_thread=None,
        _database_import_progress=None,
        _database_import_progress_dialog=lambda *_args, **_kwargs: progress,
    )
    monkeypatch.setattr(main_window, "_DatabaseInspectionThread", lambda *_args: worker)
    monkeypatch.setattr(
        main_window.QMessageBox,
        "critical",
        lambda _parent, title, message: critical.append((title, message)),
    )

    with caplog.at_level(logging.ERROR, logger=main_window.__name__):
        MainWindow._start_database_inspection(window, Path("source.db"))

    if remains_running:
        assert window._database_import_thread is worker
        assert window._database_import_progress is progress
        assert not progress.closed
        assert not worker.deleted
        assert "startup could not be confirmed" in progress.label
    else:
        assert window._database_import_thread is None
        assert window._database_import_progress is None
        assert progress.closed and progress.deleted and worker.deleted
    expected_dialog = (
        (
            "Database Review Startup Uncertain",
            "Database review may have begun, but startup could not be confirmed. "
            "Wait for its final result before trying again.",
        )
        if remains_running
        else (
            "Database Review Could Not Start",
            "The database review worker could not be started. Review Diagnostics "
            "for details, then try again.",
        )
    )
    assert critical == [expected_dialog]
    assert "private worker startup detail" not in critical[0][1]
    assert "private worker startup detail" in caplog.text


@pytest.mark.parametrize("remains_running", [False, True])
def test_portability_import_start_rolls_back_or_retains_live_worker(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    remains_running: bool,
) -> None:
    worker = _StartFailureWorker(remains_running=remains_running)
    busy_states: list[bool] = []
    critical: list[tuple[str, str]] = []
    dialog = SimpleNamespace(
        _thread=None,
        _pending_outcome=("old", object()),
        _status_label=_Label(),
        _set_busy=busy_states.append,
        _on_progress=lambda *_args: None,
        _on_worker_succeeded=lambda *_args: None,
        _on_worker_failed=lambda *_args: None,
        _on_worker_cancelled=lambda *_args: None,
        _on_thread_finished=lambda *_args: None,
    )
    monkeypatch.setattr(
        portability_dialog.QMessageBox,
        "critical",
        lambda _parent, title, message: critical.append((title, message)),
    )

    with caplog.at_level(logging.ERROR, logger=portability_dialog.__name__):
        ImportPortableBackupDialog._start_worker(dialog, worker)

    if remains_running:
        assert dialog._thread is worker
        assert busy_states == [True]
        assert not worker.deleted
        assert "startup could not be confirmed" in dialog._status_label.value
    else:
        assert dialog._thread is None
        assert dialog._pending_outcome is None
        assert busy_states == [True, False]
        assert worker.deleted
    expected_dialog = (
        (
            "Portable Backup Startup Uncertain",
            "The operation may have begun, but startup could not be confirmed. "
            "Wait for its final result before trying again.",
        )
        if remains_running
        else (
            "Portable Backup Operation Could Not Start",
            "The portable backup worker could not be started. Review Diagnostics "
            "for details, then try again.",
        )
    )
    assert critical == [expected_dialog]
    assert "private worker startup detail" not in critical[0][1]
    assert "private worker startup detail" in caplog.text


@pytest.mark.parametrize("remains_running", [False, True])
def test_portability_export_start_rolls_back_or_retains_live_worker(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    remains_running: bool,
) -> None:
    worker = _StartFailureWorker(remains_running=remains_running)
    busy_states: list[bool] = []
    critical: list[tuple[str, str]] = []
    destination = Path("worker-hardening-new-backup.automadin-backup")
    destination_edit = SimpleNamespace(
        text=lambda: str(destination),
        setText=lambda _value: None,
    )
    dialog = SimpleNamespace(
        _thread=None,
        _pending_outcome=("old", object()),
        _destination_edit=destination_edit,
        _db=object(),
        _lease_registry=BrowserProfileLeaseRegistry(),
        _selected_profile_ids=lambda: (),
        _set_exporting=busy_states.append,
        _status_label=_Label(),
        _on_progress=lambda *_args: None,
        _on_export_succeeded=lambda *_args: None,
        _on_export_failed=lambda *_args: None,
        _on_export_cancelled=lambda *_args: None,
        _on_thread_finished=lambda *_args: None,
    )
    monkeypatch.setattr(portability_dialog.os.path, "lexists", lambda _path: False)
    monkeypatch.setattr(
        portability_dialog,
        "_ExportPortableBackupThread",
        lambda *_args: worker,
    )
    monkeypatch.setattr(
        portability_dialog.QMessageBox,
        "critical",
        lambda _parent, title, message: critical.append((title, message)),
    )

    with caplog.at_level(logging.ERROR, logger=portability_dialog.__name__):
        ExportPortableBackupDialog._start_export(dialog)

    if remains_running:
        assert dialog._thread is worker
        assert busy_states == [True]
        assert not worker.deleted
        assert "startup could not be confirmed" in dialog._status_label.value
    else:
        assert dialog._thread is None
        assert dialog._pending_outcome is None
        assert busy_states == [True, False]
        assert worker.deleted
    expected_dialog = (
        (
            "Portable Backup Startup Uncertain",
            "The export may have begun, but startup could not be confirmed. "
            "Wait for its final result before trying again.",
        )
        if remains_running
        else (
            "Portable Backup Could Not Start",
            "The portable backup worker could not be started. Review Diagnostics "
            "for details, then try again.",
        )
    )
    assert critical == [expected_dialog]
    assert "private worker startup detail" not in critical[0][1]
    assert "private worker startup detail" in caplog.text


def test_database_finish_clears_references_even_if_ui_cleanup_fails(
    application: QApplication,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingProgress(_Progress):
        def deleteLater(self) -> None:
            raise RuntimeError("private cleanup failure detail")

    thread = SimpleNamespace(
        cancelled=True,
        isInterruptionRequested=lambda: False,
        deleteLater=lambda: None,
    )
    progress = FailingProgress()
    messages: list[str] = []
    window = SimpleNamespace(
        _database_import_thread=thread,
        _database_import_progress=progress,
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda message, _duration: messages.append(message)
        ),
    )

    with caplog.at_level(logging.ERROR, logger=main_window.__name__):
        MainWindow._finish_database_inspection(window, thread, progress)

    assert window._database_import_thread is None
    assert window._database_import_progress is None
    assert progress.closed
    assert "review cancelled" in messages[-1]
    assert "Could not release database inspection UI resources" in caplog.text
    assert "private cleanup failure detail" in caplog.text


def _closing_window(android_files, session_service, messages: list[str]):
    return SimpleNamespace(
        _database_import_thread=None,
        _setup_center=SimpleNamespace(shutdown=lambda: True),
        _flow_editor=SimpleNamespace(
            confirm_discard_changes=lambda: True,
            shutdown=lambda: True,
        ),
        _capture_controller=SimpleNamespace(shutdown=lambda: True),
        _flow_runner=SimpleNamespace(shutdown=lambda: True),
        _launcher=SimpleNamespace(shutdown=lambda: True),
        _device_selector=SimpleNamespace(shutdown=lambda: True),
        _android_files=android_files,
        _session_service=session_service,
        _db=SimpleNamespace(close=lambda: None),
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda message, _duration: messages.append(message)
        ),
    )


def test_main_window_aborts_android_shutdown_when_worker_refuses_close() -> None:
    aborts: list[bool] = []
    session_closes: list[bool] = []
    messages: list[str] = []
    window = _closing_window(
        SimpleNamespace(
            shutdown=lambda: False,
            abort_shutdown=lambda: aborts.append(True),
        ),
        SimpleNamespace(close_all=lambda: session_closes.append(True)),
        messages,
    )
    event = QCloseEvent()

    MainWindow.closeEvent(window, event)

    assert not event.isAccepted()
    assert aborts == [True]
    assert session_closes == []
    assert "Stopping an Android file operation" in messages[-1]


def test_main_window_aborts_android_shutdown_when_session_close_fails(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aborts: list[bool] = []
    messages: list[str] = []
    warnings: list[tuple[str, str]] = []

    def fail_close() -> None:
        raise RuntimeError("private session close detail")

    window = _closing_window(
        SimpleNamespace(
            shutdown=lambda: True,
            abort_shutdown=lambda: aborts.append(True),
        ),
        SimpleNamespace(close_all=fail_close),
        messages,
    )
    monkeypatch.setattr(
        main_window.QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    event = QCloseEvent()

    with caplog.at_level(logging.ERROR, logger=main_window.__name__):
        MainWindow.closeEvent(window, event)

    assert not event.isAccepted()
    assert aborts == [True]
    assert warnings == [
        (
            "Device Still Closing",
            "Automadin kept the window open because a device session could not "
            "be closed safely. Review Diagnostics for details, then try again.",
        )
    ]
    assert "private session close detail" not in warnings[0][1]
    assert "private session close detail" in caplog.text
    assert "A device session is still closing" in messages[-1]
