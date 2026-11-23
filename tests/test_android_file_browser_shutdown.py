from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from automadin.models.device import AndroidDevice, DeviceSession
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import (
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    device_operation_key,
)
from automadin.ui.android_file_browser import (
    AndroidFileBrowserView,
    _AndroidListThread,
    _AndroidPreviewThread,
    _AndroidRootThread,
)
from automadin.ui.main_window import MainWindow


class _FakeThread:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.interruption_requests = 0
        self.wait_calls: list[int] = []

    def isRunning(self) -> bool:
        return self.running

    def requestInterruption(self) -> None:
        self.interruption_requests += 1

    def wait(self, wait_ms: int) -> bool:
        self.wait_calls.append(wait_ms)
        return not self.running


class _Selector:
    def __init__(self, session: DeviceSession) -> None:
        self.session = session

    def get_selected_session(self) -> DeviceSession:
        return self.session


@pytest.fixture
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def android_session() -> DeviceSession:
    return DeviceSession(
        session_id="android-files-session",
        device=AndroidDevice("emulator-5554"),
        data={"adb_data": {"device_id": "emulator-5554", "adb_path": "adb"}},
    )


@pytest.fixture
def browser_view(
    application: QApplication,
    tmp_path,
    android_session: DeviceSession,
) -> AndroidFileBrowserView:
    database = DatabaseService(str(tmp_path / "android-files.db"))
    view = AndroidFileBrowserView(
        database,
        SimpleNamespace(),
        _Selector(android_session),
        device_operation_registry=DeviceOperationLeaseRegistry(),
    )
    yield view
    view.shutdown()
    view.deleteLater()
    database.close()
    application.processEvents()


def _browser_with_threads(*threads: _FakeThread | None) -> SimpleNamespace:
    controls: list[bool] = []
    return SimpleNamespace(
        _shutting_down=False,
        _list_thread=threads[0],
        _read_thread=threads[1],
        _root_thread=threads[2],
        _operation_leases={},
        _pending_operation_leases=[],
        _set_controls_enabled=controls.append,
        _release_operation_lease=lambda _thread: None,
        _retry_operation_lease_cleanup=lambda: None,
        controls=controls,
    )


def test_shutdown_requests_all_workers_without_waiting_by_default() -> None:
    threads = (_FakeThread(), _FakeThread(), _FakeThread())
    view = _browser_with_threads(*threads)

    assert AndroidFileBrowserView.shutdown(view) is False  # type: ignore[arg-type]

    assert view._shutting_down
    assert view.controls == [False]
    assert [thread.interruption_requests for thread in threads] == [1, 1, 1]
    assert [thread.wait_calls for thread in threads] == [[], [], []]
    assert (view._list_thread, view._read_thread, view._root_thread) == threads


def test_android_file_worker_mapping_retains_lease_after_release_exception(
    browser_view: AndroidFileBrowserView,
) -> None:
    registry = browser_view._device_operation_registry
    lease = registry.acquire(
        "android:lease-retry-device",
        DeviceOperationUse("android-file-list", "File listing", "lease-retry"),
    )
    thread = _FakeThread(running=False)
    browser_view._operation_leases[thread] = lease  # type: ignore[index]

    with patch.object(
        registry,
        "_release",
        side_effect=RuntimeError("transient registry failure"),
    ):
        browser_view._release_operation_lease(thread)  # type: ignore[arg-type]

    assert browser_view._operation_leases[thread] is lease  # type: ignore[index]
    assert registry.active_count == 1
    browser_view._release_operation_lease(thread)  # type: ignore[arg-type]
    assert thread not in browser_view._operation_leases
    assert registry.active_count == 0


def test_shutdown_uses_one_shared_wait_deadline(monkeypatch) -> None:
    clock = [100.0]

    class _AdvancingThread(_FakeThread):
        def wait(self, wait_ms: int) -> bool:
            super().wait(wait_ms)
            clock[0] += 0.060
            return False

    first = _AdvancingThread()
    second = _FakeThread()
    view = _browser_with_threads(first, second, None)
    monkeypatch.setattr(
        "automadin.ui.android_file_browser.time.monotonic",
        lambda: clock[0],
    )

    assert (
        AndroidFileBrowserView.shutdown(view, wait_ms=100) is False  # type: ignore[arg-type]
    )

    assert 0 < first.wait_calls[0] <= 100
    assert 0 < second.wait_calls[0] <= 40
    assert second.wait_calls[0] < first.wait_calls[0]


def test_shutdown_returns_true_after_retained_workers_finish() -> None:
    thread = _FakeThread()
    view = _browser_with_threads(thread, None, None)

    assert AndroidFileBrowserView.shutdown(view) is False  # type: ignore[arg-type]
    thread.running = False

    assert AndroidFileBrowserView.shutdown(view) is True  # type: ignore[arg-type]
    assert view._list_thread is thread


def test_main_window_refuses_close_while_android_file_worker_is_live() -> None:
    messages: list[str] = []
    session_close_calls: list[bool] = []
    window = SimpleNamespace(
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
        _android_files=SimpleNamespace(shutdown=lambda: False),
        _session_service=SimpleNamespace(
            close_all=lambda: session_close_calls.append(True)
        ),
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda message, _duration: messages.append(message)
        ),
    )
    event = QCloseEvent()

    MainWindow.closeEvent(window, event)  # type: ignore[arg-type]

    assert not event.isAccepted()
    assert "Stopping an Android file operation" in messages[-1]
    assert session_close_calls == []


@pytest.mark.parametrize(
    ("kind", "action"),
    [
        ("flow-run", "list"),
        ("run-step", "preview"),
    ],
)
def test_android_file_actions_conflict_with_same_target_from_different_session(
    browser_view: AndroidFileBrowserView,
    android_session: DeviceSession,
    kind: str,
    action: str,
) -> None:
    registry = browser_view._device_operation_registry
    same_target_other_session = DeviceSession(
        session_id="different-session",
        device=AndroidDevice(android_session.device.id),
    )
    held = registry.acquire(
        device_operation_key(same_target_other_session.device),
        DeviceOperationUse(kind, f"{kind} operation", f"{kind}-1"),
    )

    if action == "list":
        browser_view._open_path("/sdcard")
        assert browser_view._list_thread is None
    else:
        browser_view._preview_file("/sdcard/test.txt")
        assert browser_view._read_thread is None

    assert "Device busy" in browser_view._status_label.text()
    assert "Stop that operation" in browser_view._status_label.text()
    assert registry.active_count == 1
    held.release()


def test_adb_root_conflict_is_immediate_and_does_not_start_worker(
    browser_view: AndroidFileBrowserView,
    android_session: DeviceSession,
) -> None:
    registry = browser_view._device_operation_registry
    held = registry.acquire(
        device_operation_key(android_session.device),
        DeviceOperationUse("flow-run", "Running flow", "flow-1"),
    )

    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        browser_view._on_root_clicked()

    assert browser_view._root_thread is None
    assert "Device busy" in browser_view._status_label.text()
    assert registry.active_count == 1
    held.release()


@pytest.mark.parametrize(
    ("method_name", "thread_path", "thread_attr"),
    [
        ("_open_path", "_AndroidListThread.start", "_list_thread"),
        ("_preview_file", "_AndroidPreviewThread.start", "_read_thread"),
    ],
)
def test_worker_start_failure_releases_exact_lease_and_restores_local_state(
    browser_view: AndroidFileBrowserView,
    method_name: str,
    thread_path: str,
    thread_attr: str,
) -> None:
    with patch(
        f"automadin.ui.android_file_browser.{thread_path}",
        side_effect=OSError("private start failure"),
    ):
        getattr(browser_view, method_name)("/sdcard")

    assert getattr(browser_view, thread_attr) is None
    assert browser_view._device_operation_registry.active_count == 0
    assert browser_view._operation_leases == {}
    assert "could not be started" in browser_view._status_label.text()


def test_root_start_failure_releases_lease_and_reenables_button(
    browser_view: AndroidFileBrowserView,
) -> None:
    with (
        patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ),
        patch(
            "automadin.ui.android_file_browser._AndroidRootThread.start",
            side_effect=OSError("private start failure"),
        ),
    ):
        browser_view._on_root_clicked()

    assert browser_view._root_thread is None
    assert browser_view._root_btn.isEnabled()
    assert browser_view._device_operation_registry.active_count == 0
    assert browser_view._operation_leases == {}


@pytest.mark.parametrize(
    ("method_name", "thread_class_name", "thread_attr", "args"),
    [
        ("_open_path", "_AndroidListThread", "_list_thread", ("/sdcard",)),
        (
            "_preview_file",
            "_AndroidPreviewThread",
            "_read_thread",
            ("/sdcard/test.txt",),
        ),
        ("_on_root_clicked", "_AndroidRootThread", "_root_thread", ()),
    ],
)
def test_worker_constructor_failure_releases_lease_and_rolls_back_state(
    browser_view: AndroidFileBrowserView,
    method_name: str,
    thread_class_name: str,
    thread_attr: str,
    args: tuple[str, ...],
) -> None:
    question = (
        patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        )
        if method_name == "_on_root_clicked"
        else patch.object(QMessageBox, "question")
    )
    with (
        question,
        patch(
            f"automadin.ui.android_file_browser.{thread_class_name}",
            side_effect=OSError("private constructor failure"),
        ),
    ):
        getattr(browser_view, method_name)(*args)

    assert getattr(browser_view, thread_attr) is None
    assert browser_view._device_operation_registry.active_count == 0
    assert browser_view._operation_leases == {}
    if method_name == "_on_root_clicked":
        assert browser_view._root_btn.isEnabled()
    assert "could not be started" in browser_view._status_label.text()


@pytest.mark.parametrize(
    ("method_name", "thread_class", "thread_attr", "engine_method", "args"),
    [
        (
            "_open_path",
            _AndroidListThread,
            "_list_thread",
            "list_directory",
            ("/sdcard",),
        ),
        (
            "_preview_file",
            _AndroidPreviewThread,
            "_read_thread",
            "read_file_text",
            ("/sdcard/test.txt", True, False),
        ),
    ],
)
def test_file_worker_post_native_start_raise_retains_worker_and_lease(
    application: QApplication,
    browser_view: AndroidFileBrowserView,
    method_name: str,
    thread_class,
    thread_attr: str,
    engine_method: str,
    args: tuple[object, ...],
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original_start = thread_class.start

    def blocking_operation(*_args):
        entered.set()
        release.wait(2)
        return [] if engine_method == "list_directory" else ("preview", False)

    def start_then_raise(worker) -> None:
        original_start(worker)
        assert entered.wait(1)
        raise OSError("private post-native file-worker start detail")

    browser_view._session_service._adb_engine = SimpleNamespace(
        **{engine_method: blocking_operation}
    )
    worker = None
    try:
        with patch.object(thread_class, "start", start_then_raise):
            getattr(browser_view, method_name)(*args)

        worker = getattr(browser_view, thread_attr)
        assert worker is not None
        assert worker.isRunning()
        assert worker.isInterruptionRequested()
        assert browser_view._device_operation_registry.active_count == 1
        assert worker in browser_view._operation_leases
    finally:
        release.set()
        if worker is not None:
            worker.wait(2000)
        application.processEvents()

    assert getattr(browser_view, thread_attr) is None
    assert browser_view._device_operation_registry.active_count == 0
    assert browser_view._operation_leases == {}


def test_root_post_native_start_raise_retains_lease_and_refreshes_after_release(
    application: QApplication,
    browser_view: AndroidFileBrowserView,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    refresh_lease_counts: list[int] = []
    original_start = _AndroidRootThread.start

    def restart_root(_adb_data):
        entered.set()
        release.wait(2)
        return "restarting"

    def start_then_raise(worker) -> None:
        original_start(worker)
        assert entered.wait(1)
        raise OSError("private post-native root start detail")

    browser_view._session_service._adb_engine = SimpleNamespace(
        restart_adbd_as_root=restart_root,
        root_state=lambda _adb_data: SimpleNamespace(label="root"),
    )
    browser_view._refresh_current_path = lambda: refresh_lease_counts.append(
        browser_view._device_operation_registry.active_count
    )
    worker = None
    try:
        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(_AndroidRootThread, "start", start_then_raise),
        ):
            browser_view._on_root_clicked()

        worker = browser_view._root_thread
        assert worker is not None
        assert worker.isRunning()
        assert worker.isInterruptionRequested()
        assert browser_view._device_operation_registry.active_count == 1
    finally:
        release.set()
        if worker is not None:
            worker.wait(2000)
        application.processEvents()

    assert browser_view._root_thread is None
    assert browser_view._device_operation_registry.active_count == 0
    assert refresh_lease_counts == [0]


@pytest.mark.parametrize(
    ("method_name", "thread_path", "thread_attr", "clear_method"),
    [
        (
            "_open_path",
            "_AndroidListThread.start",
            "_list_thread",
            "_clear_list_thread",
        ),
        (
            "_preview_file",
            "_AndroidPreviewThread.start",
            "_read_thread",
            "_clear_read_thread",
        ),
    ],
)
def test_file_worker_lease_is_held_until_exact_finished_cleanup(
    browser_view: AndroidFileBrowserView,
    method_name: str,
    thread_path: str,
    thread_attr: str,
    clear_method: str,
) -> None:
    with patch(
        f"automadin.ui.android_file_browser.{thread_path}",
        return_value=None,
    ):
        getattr(browser_view, method_name)("/sdcard")

    thread = getattr(browser_view, thread_attr)
    assert thread is not None
    assert browser_view._device_operation_registry.active_count == 1
    assert thread in browser_view._operation_leases

    getattr(browser_view, clear_method)(thread)
    getattr(browser_view, clear_method)(thread)

    assert browser_view._device_operation_registry.active_count == 0
    assert browser_view._operation_leases == {}


def test_root_lease_is_held_until_exact_finished_cleanup(
    browser_view: AndroidFileBrowserView,
) -> None:
    with (
        patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ),
        patch(
            "automadin.ui.android_file_browser._AndroidRootThread.start",
            return_value=None,
        ),
    ):
        browser_view._on_root_clicked()

    thread = browser_view._root_thread
    assert thread is not None
    assert browser_view._device_operation_registry.active_count == 1
    assert thread in browser_view._operation_leases

    browser_view._clear_root_thread(thread)
    browser_view._clear_root_thread(thread)

    assert browser_view._device_operation_registry.active_count == 0
    assert browser_view._operation_leases == {}


def test_refused_shutdown_restores_controls_when_last_worker_finishes(
    browser_view: AndroidFileBrowserView,
) -> None:
    thread = _FakeThread()
    browser_view._list_thread = thread  # type: ignore[assignment]

    assert browser_view.shutdown() is False
    assert browser_view._shutting_down
    assert not browser_view._refresh_btn.isEnabled()

    thread.running = False
    browser_view._clear_list_thread(thread)  # type: ignore[arg-type]

    assert not browser_view._shutting_down
    assert browser_view._list_thread is None
    assert browser_view._refresh_btn.isEnabled()
    assert browser_view._root_btn.isEnabled()
    assert "ready" in browser_view._status_label.text().lower()
