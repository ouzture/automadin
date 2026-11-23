from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from automadin.services.database import CURRENT_SCHEMA_VERSION, DatabaseService
from automadin.services.support_report import build_support_report
from automadin.ui import main_window
from automadin.ui.diagnostics_dialog import (
    DiagnosticsDialog,
    DiagnosticsReportLoaderDialog,
)
from automadin.ui.theme import get_stylesheet


class _Database:
    @staticmethod
    def get_workspace_statistics() -> dict[str, int]:
        return {
            "schema_version": 2,
            "flows": 0,
            "images": 0,
            "run_setups": 0,
            "browser_profiles": 0,
        }


class _ContendedClipboard:
    def __init__(self) -> None:
        self.attempted_text = ""

    def setText(self, text: str, _mode: object = None) -> None:
        self.attempted_text = text

    @staticmethod
    def text(_mode: object = None) -> str:
        return "stale clipboard content"


@pytest.fixture(scope="module")
def application() -> QApplication:
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(get_stylesheet())
    return instance


def _wait_until(
    application: QApplication,
    predicate,
    *,
    timeout_seconds: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Timed out waiting for Diagnostics UI state")


def test_loader_start_failure_is_logged_and_shown_safely(
    application: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loader = DiagnosticsReportLoaderDialog(
        tmp_path / "automadin.db",
        tmp_path,
    )

    def fail_start() -> None:
        raise OSError("private worker startup detail")

    monkeypatch.setattr(loader._thread, "start", fail_start)
    caplog.set_level(logging.ERROR)
    try:
        loader._start_collection(deferred_reveal=False)
        application.processEvents()

        assert loader.collection_failed
        assert loader.title_label.text() == "Support report unavailable"
        assert "private worker startup detail" not in loader.status_label.text()
        assert "Could not start Diagnostics collection worker" in caplog.text
        assert "private worker startup detail" in caplog.text
    finally:
        loader.close()
        loader.deleteLater()
        application.processEvents()


def test_dialog_previews_exact_clipboard_text_and_opens_only_validated_logs(
    application: QApplication,
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "automadin.log").write_text("private log contents", encoding="utf-8")
    report = build_support_report(_Database(), tmp_path)
    opened: list[QUrl] = []
    dialog = DiagnosticsDialog(
        report,
        directory_opener=lambda url: opened.append(url) or True,
    )
    try:
        dialog.show()
        application.processEvents()
        assert dialog.preview.toPlainText() == report.text
        assert dialog.preview.isReadOnly()
        assert dialog.preview.font().pointSizeF() >= 10.5
        assert "private log contents" not in dialog.preview.toPlainText()
        privacy_notice = dialog.findChild(QLabel, "diagnosticsPrivacyNotice")
        log_warning = dialog.findChild(QLabel, "diagnosticsLogWarning")
        assert privacy_notice is not None
        assert privacy_notice.font().pointSizeF() >= 10.5
        assert "Review exactly what Copy Support Report" in privacy_notice.text()
        assert log_warning is not None
        assert log_warning.font().pointSizeF() >= 10.5
        assert "Warning:" in log_warning.text()
        assert "sensitive automation" in log_warning.text()
        for button in (
            dialog.copy_button,
            dialog.open_logs_button,
            dialog.copy_logs_path_button,
        ):
            assert button.sizeHint().height() >= 36

        application.clipboard().setText("previous")
        dialog.copy_button.click()
        application.processEvents()

        assert application.clipboard().text() == dialog.preview.toPlainText()
        assert application.clipboard().text() == report.text
        assert "copied" in dialog.status_label.text().casefold()

        assert dialog.open_logs_button.isEnabled()
        dialog.open_logs_button.click()
        application.processEvents()

        assert len(opened) == 1
        assert Path(opened[0].toLocalFile()) == logs.resolve()
        assert dialog.status_label.text() == "Open request sent to File Explorer."

        dialog.copy_logs_path_button.click()
        application.processEvents()
        assert application.clipboard().text() == str(logs.resolve())
        assert "path copied" in dialog.status_label.text().casefold()
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_dialog_disables_open_when_log_directory_does_not_exist(
    application: QApplication,
    tmp_path: Path,
) -> None:
    report = build_support_report(_Database(), tmp_path)
    dialog = DiagnosticsDialog(report)
    try:
        assert report.logs_directory is None
        assert not dialog.open_logs_button.isEnabled()
        assert not dialog.copy_logs_path_button.isEnabled()
        assert dialog.copy_button.isEnabled()
        assert "Not created yet" in dialog.status_label.text()
        assert "Not created yet" in dialog.open_logs_button.toolTip()
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_dialog_revalidates_logs_directory_before_opening(
    application: QApplication,
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    report = build_support_report(_Database(), tmp_path)
    opened: list[QUrl] = []
    dialog = DiagnosticsDialog(
        report,
        directory_opener=lambda url: opened.append(url) or True,
    )
    try:
        logs.rmdir()
        dialog.open_logs_button.click()
        application.processEvents()

        assert opened == []
        assert not dialog.open_logs_button.isEnabled()
        assert not dialog.copy_logs_path_button.isEnabled()
        assert dialog.status_label.text() == "The logs folder is no longer available."
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_dialog_never_reports_clipboard_success_without_readback(
    application: QApplication,
    tmp_path: Path,
) -> None:
    clipboard = _ContendedClipboard()
    report = build_support_report(_Database(), tmp_path)
    dialog = DiagnosticsDialog(report, clipboard=clipboard)
    try:
        dialog.copy_button.click()
        application.processEvents()

        assert clipboard.attempted_text == report.text
        assert "Could not confirm" in dialog.status_label.text()
        assert "copied to the clipboard" not in dialog.status_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_dialog_open_failure_points_to_separate_path_action(
    application: QApplication,
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    report = build_support_report(_Database(), tmp_path)
    dialog = DiagnosticsDialog(report, directory_opener=lambda _url: False)
    try:
        dialog.open_logs_button.click()
        application.processEvents()

        assert "Use Copy Logs Path" in dialog.status_label.text()
        assert dialog.copy_logs_path_button.isEnabled()
        assert str(logs.resolve()) not in report.text
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_loader_collects_report_off_the_gui_thread(
    application: QApplication,
    tmp_path: Path,
) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"), enable_wal=False)
    loader = DiagnosticsReportLoaderDialog(database.db_path, tmp_path)
    try:
        loader.show()
        _wait_until(
            application,
            lambda: loader.result() == loader.DialogCode.Accepted,
        )

        assert loader.report is not None
        assert f"Database schema: {CURRENT_SCHEMA_VERSION}" in loader.report.text
    finally:
        loader.close()
        loader.deleteLater()
        application.processEvents()
        database.close()


def test_loader_does_not_flash_for_fast_collection(
    application: QApplication,
    tmp_path: Path,
) -> None:
    report = build_support_report(_Database(), tmp_path)
    release_worker = threading.Event()

    def fast_builder(_db_path: str | Path, _data_dir: str | Path):
        release_worker.wait(timeout=1)
        return report

    parent = QWidget()
    button = QPushButton("Focus anchor", parent)
    parent.show()
    parent.activateWindow()
    button.setFocus()
    application.processEvents()
    assert button.hasFocus()
    assert application.activeWindow() is parent

    loader = DiagnosticsReportLoaderDialog(
        tmp_path / "automadin.db",
        tmp_path,
        parent,
        report_builder=fast_builder,
    )
    loader._reveal_timer.setInterval(1000)
    close_results: list[tuple[bool, bool]] = []

    def attempt_parent_close() -> None:
        result = parent.close()
        close_results.append((result, parent.isVisible()))

    try:
        QTimer.singleShot(20, attempt_parent_close)
        QTimer.singleShot(60, release_worker.set)
        result = loader.exec()

        assert result == loader.DialogCode.Accepted
        assert loader.report is report
        assert not loader._loader_revealed
        assert not loader.isVisible()
        assert len(close_results) == 1
        assert close_results[0][1]
        assert parent.isVisible()
        assert button.hasFocus()
        assert application.activeWindow() is parent
    finally:
        release_worker.set()
        loader.close()
        loader.deleteLater()
        parent.close()
        parent.deleteLater()
        application.processEvents()


def test_loader_failure_is_generic_and_has_no_dead_log_actions(
    application: QApplication,
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "automadin.log").write_text("private log", encoding="utf-8")
    def fail_builder(_db_path: str | Path, _data_dir: str | Path):
        raise RuntimeError("token=secret user@example.com")

    loader = DiagnosticsReportLoaderDialog(
        tmp_path / "automadin.db",
        tmp_path,
        report_builder=fail_builder,
    )
    try:
        loader.show()
        _wait_until(application, lambda: loader.collection_failed)

        visible_text = loader.status_label.text()
        assert loader.title_label.text() == "Support report unavailable"
        assert "token=secret" not in visible_text
        assert "user@example.com" not in visible_text
        assert "No information was copied" in visible_text
        assert "try Diagnostics again" in visible_text
        assert not hasattr(loader, "open_logs_button")
        assert not hasattr(loader, "copy_logs_path_button")
        assert loader.height() >= 260
    finally:
        loader.close()
        loader.deleteLater()
        application.processEvents()


def test_deferred_exec_shows_fast_failure_until_user_closes(
    application: QApplication,
    tmp_path: Path,
) -> None:
    def fail_builder(_db_path: str | Path, _data_dir: str | Path):
        raise RuntimeError("private failure")

    loader = DiagnosticsReportLoaderDialog(
        tmp_path / "automadin.db",
        tmp_path,
        report_builder=fail_builder,
    )
    observed: list[tuple[bool, bool, str]] = []

    def observe_and_close() -> None:
        observed.append(
            (
                loader.collection_failed,
                loader.isVisible(),
                loader.title_label.text(),
            )
        )
        loader.reject()

    try:
        QTimer.singleShot(loader._REVEAL_DELAY_MS + 100, observe_and_close)
        result = loader.exec()

        assert result == loader.DialogCode.Rejected
        assert observed == [
            (True, True, "Support report unavailable")
        ]
    finally:
        loader.close()
        loader.deleteLater()
        application.processEvents()


def test_slow_loader_becomes_a_native_visible_window(
    application: QApplication,
    tmp_path: Path,
) -> None:
    release_worker = threading.Event()
    report = build_support_report(_Database(), tmp_path)

    def slow_builder(_db_path: str | Path, _data_dir: str | Path):
        release_worker.wait(timeout=2)
        return report

    loader = DiagnosticsReportLoaderDialog(
        tmp_path / "automadin.db",
        tmp_path,
        report_builder=slow_builder,
    )
    exposed_states: list[bool] = []

    def observe_and_release() -> None:
        window = loader.windowHandle()
        exposed_states.append(
            window is not None
            and window.isVisible()
            and window.isExposed()
        )
        release_worker.set()

    try:
        QTimer.singleShot(
            loader._REVEAL_DELAY_MS + 100,
            observe_and_release,
        )
        result = loader.exec()

        assert result == loader.DialogCode.Accepted
        assert exposed_states == [True]
        assert loader._loader_revealed
    finally:
        release_worker.set()
        if loader._thread.isRunning():
            _wait_until(application, lambda: not loader._thread.isRunning())
        loader.close()
        loader.deleteLater()
        application.processEvents()


def test_loader_keeps_ui_responsive_and_defers_close_while_worker_runs(
    application: QApplication,
    tmp_path: Path,
) -> None:
    release_worker = threading.Event()
    base_report = build_support_report(_Database(), tmp_path)

    def slow_builder(_db_path: str | Path, _data_dir: str | Path):
        release_worker.wait(timeout=2)
        return base_report

    loader = DiagnosticsReportLoaderDialog(
        tmp_path / "automadin.db",
        tmp_path,
        report_builder=slow_builder,
    )
    timer_fired: list[bool] = []
    try:
        loader.show()
        QTimer.singleShot(0, lambda: timer_fired.append(True))
        _wait_until(application, lambda: bool(timer_fired))
        assert loader._thread.isRunning()

        loader.close()
        application.processEvents()
        assert loader.isVisible()
        assert not loader.close_button.isEnabled()
        assert "current local diagnostics check" in loader.status_label.text()

        release_worker.set()
        _wait_until(application, lambda: not loader.isVisible())
        assert not loader._thread.isRunning()
        assert loader.report is None
    finally:
        release_worker.set()
        if loader._thread.isRunning():
            _wait_until(application, lambda: not loader._thread.isRunning())
        loader.close()
        loader.deleteLater()
        application.processEvents()


def test_loader_passes_cancellation_callback_to_new_builder_shape(
    application: QApplication,
    tmp_path: Path,
) -> None:
    callback_received = threading.Event()
    cancellation_observed = threading.Event()

    def cancellable_builder(
        _db_path: str | Path,
        _data_dir: str | Path,
        cancelled,
    ):
        callback_received.set()
        while not cancelled():
            time.sleep(0.005)
        cancellation_observed.set()
        raise RuntimeError("support report collection cancelled")

    loader = DiagnosticsReportLoaderDialog(
        tmp_path / "automadin.db",
        tmp_path,
        report_builder=cancellable_builder,
    )
    cancel_states: list[tuple[bool, bool]] = []

    def cancel_after_reveal() -> None:
        cancel_states.append(
            (callback_received.is_set(), loader.isVisible())
        )
        loader.reject()

    try:
        QTimer.singleShot(
            loader._REVEAL_DELAY_MS + 100,
            cancel_after_reveal,
        )
        result = loader.exec()

        assert result == loader.DialogCode.Rejected
        assert cancel_states == [(True, True)]
        assert cancellation_observed.is_set()
        assert not loader._thread.isRunning()
        assert not loader.isVisible()
        assert not loader.collection_failed
        assert loader.report is None
    finally:
        if loader._thread.isRunning():
            loader._thread.requestInterruption()
            _wait_until(application, lambda: not loader._thread.isRunning())
        loader.close()
        loader.deleteLater()
        application.processEvents()


def test_help_menu_exposes_and_wires_diagnostics_action(
    application: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[object] = []
    monkeypatch.setattr(
        main_window.MainWindow,
        "_show_diagnostics",
        lambda self: opened.append(self),
    )
    monkeypatch.setenv("AUTOMADIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTOMADIN_PROFILES_DIR", str(tmp_path / "profiles"))
    database = DatabaseService(str(tmp_path / "automadin.db"))
    window = main_window.MainWindow(database)
    try:
        help_actions = [
            action for action in window.menuBar().actions() if action.text() == "&Help"
        ]
        assert len(help_actions) == 1
        help_menu = help_actions[0].menu()
        assert help_menu is not None
        diagnostics_actions = [
            action
            for action in help_menu.actions()
            if action.objectName() == "diagnosticsAction"
        ]
        assert len(diagnostics_actions) == 1
        assert diagnostics_actions[0].text() == "Diagnostics..."

        diagnostics_actions[0].trigger()
        application.processEvents()

        assert opened == [window]
    finally:
        window.close()
        window.deleteLater()
        application.processEvents()
        database.close()


def test_main_window_opens_report_returned_by_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = build_support_report(_Database(), tmp_path)
    calls: list[tuple[object, ...]] = []

    class _Loader:
        def __init__(self, db_path, data_dir, parent) -> None:
            self.report = report
            calls.append(("loader", db_path, data_dir, parent))

        def exec(self) -> int:
            calls.append(("loader-exec",))
            return 1

    class _Dialog:
        def __init__(self, loaded_report, parent) -> None:
            calls.append(("dialog", loaded_report, parent))

        def exec(self) -> int:
            calls.append(("dialog-exec",))
            return 1

    fake_window = SimpleNamespace(
        _db=SimpleNamespace(db_path=str(tmp_path / "automadin.db"))
    )
    monkeypatch.setattr(main_window, "DiagnosticsReportLoaderDialog", _Loader)
    monkeypatch.setattr(main_window, "DiagnosticsDialog", _Dialog)

    main_window.MainWindow._show_diagnostics(fake_window)

    assert calls == [
        ("loader", str(tmp_path / "automadin.db"), tmp_path, fake_window),
        ("loader-exec",),
        ("dialog", report, fake_window),
        ("dialog-exec",),
    ]
