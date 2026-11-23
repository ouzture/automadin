from __future__ import annotations

import os
import threading
import logging
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from automadin.ui import runtime_manager


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _ControllableInstallWorker(QThread):
    progress = Signal(int, str)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, component: str, data_dir: str, parent=None) -> None:
        super().__init__(parent)
        self.component = component
        self.data_dir = data_dir
        self.cancel_requested = False
        self.wait_calls: list[int] = []
        self._release = threading.Event()

    def run(self) -> None:
        self._release.wait()

    def cancel(self) -> None:
        # Deliberately do not release the worker. A real installer may need an
        # arbitrary amount of time to leave subprocess and filesystem code safely.
        self.cancel_requested = True

    def wait(self, msecs: int = -1) -> bool:
        self.wait_calls.append(msecs)
        raise AssertionError("RuntimeManagerDialog must never wait on the GUI thread")

    def finish_from_test(self) -> None:
        self._release.set()
        assert QThread.wait(self, 1000)


@pytest.fixture
def dialog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app = _app()
    monkeypatch.setattr(runtime_manager, "_InstallWorker", _ControllableInstallWorker)
    monkeypatch.setattr(
        runtime_manager.RuntimeManagerDialog,
        "_refresh_status",
        lambda self: self._update_adb_button(),
    )
    value = runtime_manager.RuntimeManagerDialog(str(tmp_path))
    value.show()
    app.processEvents()
    yield value

    worker = value._worker
    if isinstance(worker, _ControllableInstallWorker) and worker.isRunning():
        worker.finish_from_test()
        app.processEvents()
    value.close()
    value.deleteLater()
    app.processEvents()


def _start_worker(dialog: runtime_manager.RuntimeManagerDialog) -> _ControllableInstallWorker:
    dialog._start_install("browsers")
    worker = dialog._worker
    assert isinstance(worker, _ControllableInstallWorker)
    assert worker.isRunning()
    return worker


def test_visible_close_button_uses_guarded_close_behavior(
    dialog: runtime_manager.RuntimeManagerDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _start_worker(dialog)
    questions: list[str] = []

    def refuse_close(_parent, title, _message, _buttons):
        questions.append(title)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(runtime_manager.QMessageBox, "question", refuse_close)

    QTest.mouseClick(dialog._close_button, Qt.MouseButton.LeftButton)

    assert questions == ["Cancel Installation?"]
    assert dialog.isVisible()
    assert not worker.cancel_requested
    assert not dialog._close_requested


def test_window_close_is_refused_when_user_declines_cancellation(
    dialog: runtime_manager.RuntimeManagerDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _start_worker(dialog)
    monkeypatch.setattr(
        runtime_manager.QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.No,
    )

    assert not dialog.close()

    assert dialog.isVisible()
    assert not worker.cancel_requested
    assert not dialog._close_requested


def test_escape_uses_the_same_guarded_close_behavior(
    dialog: runtime_manager.RuntimeManagerDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _start_worker(dialog)
    monkeypatch.setattr(
        runtime_manager.QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.No,
    )

    QTest.keyClick(dialog, Qt.Key.Key_Escape)

    assert dialog.isVisible()
    assert not worker.cancel_requested
    assert not dialog._close_requested


def test_confirmed_close_never_waits_for_a_slow_worker_and_closes_when_it_finishes(
    dialog: runtime_manager.RuntimeManagerDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _start_worker(dialog)
    question_count = 0

    def confirm_close(*_args):
        nonlocal question_count
        question_count += 1
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(runtime_manager.QMessageBox, "question", confirm_close)

    # The controllable worker stays alive indefinitely after cancel(), simulating
    # an installer still running beyond the old five-second timeout without a sleep.
    assert not dialog.close()

    assert worker.isRunning()
    assert worker.cancel_requested
    assert worker.wait_calls == []
    assert dialog.isVisible()
    assert dialog._close_requested
    assert not dialog._close_button.isEnabled()
    assert not dialog._cancel_button.isEnabled()
    assert "close automatically" in dialog._operation_status.text()

    # Repeated native close requests remain refused without another prompt.
    assert not dialog.close()
    assert question_count == 1

    worker.finish_from_test()
    _app().processEvents()

    assert dialog._worker is None
    assert not dialog.isVisible()
    assert worker.wait_calls == []


@pytest.mark.parametrize(
    ("outcome", "message", "message_box_name", "expected_title"),
    [
        ("success", "component ready", "information", "Runtime Ready"),
        ("failure", "component failed", "warning", "Installation Failed"),
        (
            "cancelled",
            runtime_manager._INSTALL_CANCELLED_MESSAGE,
            "information",
            "Installation Cancelled",
        ),
    ],
)
def test_result_ui_is_shown_only_after_worker_finished(
    dialog: runtime_manager.RuntimeManagerDialog,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    message: str,
    message_box_name: str,
    expected_title: str,
) -> None:
    worker = _start_worker(dialog)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runtime_manager.QMessageBox,
        message_box_name,
        lambda _parent, title, message: shown.append((title, message)),
    )

    if outcome == "success":
        dialog._on_succeeded(message)
    else:
        dialog._on_failed(message)

    assert shown == []
    assert dialog._worker is worker

    worker.finish_from_test()
    _app().processEvents()

    assert dialog._worker is None
    assert shown == [(expected_title, message)]
    assert not dialog._progress.isVisible()
    assert not dialog._cancel_button.isVisible()
    assert dialog._close_button.isEnabled()


def test_install_worker_logs_traceback_and_emits_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _app()
    monkeypatch.setattr(
        runtime_manager,
        "install_adb",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private installer detail")
        ),
    )
    worker = runtime_manager._InstallWorker("adb", str(tmp_path))
    failures: list[str] = []
    worker.failed.connect(failures.append)

    with caplog.at_level(logging.ERROR, logger=runtime_manager.__name__):
        worker.run()

    assert failures == [runtime_manager._INSTALL_FAILURE_MESSAGE]
    assert "private installer detail" not in failures[0]
    assert any(record.exc_info for record in caplog.records)
    assert "private installer detail" in caplog.text


def test_worker_start_failure_rolls_back_runtime_manager_ui(
    dialog: runtime_manager.RuntimeManagerDialog,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _StartFailureWorker(_ControllableInstallWorker):
        instance: "_StartFailureWorker | None" = None

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.deleted = False
            type(self).instance = self

        def start(self) -> None:
            raise RuntimeError("private start detail")

        def deleteLater(self) -> None:
            self.deleted = True

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(runtime_manager, "_InstallWorker", _StartFailureWorker)
    monkeypatch.setattr(
        runtime_manager.QMessageBox,
        "warning",
        lambda _parent, title, message: shown.append((title, message)),
    )

    with caplog.at_level(logging.ERROR, logger=runtime_manager.__name__):
        dialog._start_install("browsers")

    worker = _StartFailureWorker.instance
    assert worker is not None
    assert worker.deleted
    assert dialog._worker is None
    assert not dialog._progress.isVisible()
    assert not dialog._cancel_button.isVisible()
    assert dialog._browser_button.isEnabled()
    assert dialog._close_button.isEnabled()
    assert shown == [
        ("Installation Could Not Start", runtime_manager._INSTALL_START_FAILURE_MESSAGE)
    ]
    assert "private start detail" not in shown[0][1]
    assert any(record.exc_info for record in caplog.records)


def test_worker_release_failure_does_not_strand_runtime_manager_ui(
    dialog: runtime_manager.RuntimeManagerDialog,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _start_worker(dialog)
    monkeypatch.setattr(
        runtime_manager.QMessageBox,
        "information",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        worker,
        "deleteLater",
        lambda: (_ for _ in ()).throw(RuntimeError("release failed")),
    )
    dialog._on_succeeded("component ready")

    with caplog.at_level(logging.ERROR, logger=runtime_manager.__name__):
        worker.finish_from_test()
        _app().processEvents()

    assert dialog._worker is None
    assert not dialog._progress.isVisible()
    assert dialog._close_button.isEnabled()
    assert any(
        "Could not release runtime installation worker" in record.message
        and record.exc_info
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("outcome", "message", "outcome_title"),
    [
        ("success", "component ready", "Runtime Ready"),
        ("failure", "component failed", "Installation Failed"),
    ],
)
def test_status_refresh_failure_preserves_install_outcome_after_cleanup(
    dialog: runtime_manager.RuntimeManagerDialog,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    message: str,
    outcome_title: str,
) -> None:
    worker = _start_worker(dialog)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dialog,
        "_refresh_status",
        lambda: (_ for _ in ()).throw(RuntimeError("private refresh detail")),
    )
    monkeypatch.setattr(
        runtime_manager.QMessageBox,
        "warning",
        lambda _parent, title, message: shown.append((title, message)),
    )
    monkeypatch.setattr(
        runtime_manager.QMessageBox,
        "information",
        lambda _parent, title, message: shown.append((title, message)),
    )
    if outcome == "success":
        dialog._on_succeeded(message)
    else:
        dialog._on_failed(message)

    with caplog.at_level(logging.ERROR, logger=runtime_manager.__name__):
        worker.finish_from_test()
        _app().processEvents()

    assert dialog._worker is None
    assert not dialog._progress.isVisible()
    assert shown == [
        (outcome_title, message),
        (
            "Runtime Status Unavailable",
            "Runtime status could not be refreshed after the operation. "
            "Check the Automadin log and reopen Runtime Manager.",
        ),
    ]
    assert all("private refresh detail" not in shown_message for _, shown_message in shown)
    assert "private refresh detail" in caplog.text
    assert any(record.exc_info for record in caplog.records)
