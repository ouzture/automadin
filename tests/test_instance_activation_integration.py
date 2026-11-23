from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import PySide6.QtCore as qt_core
import PySide6.QtWidgets as qt_widgets

from automadin import app
from automadin.instance_activation import ACTIVATION_DESCRIPTOR_NAME
from automadin.services import database as database_module
from automadin.ui import main_window, runtime_manager, theme


class _FakeApplication:
    events: list[str] = []

    def __init__(self, _argv) -> None:
        self.events.append("application-created")

    def setApplicationName(self, _name: str) -> None:
        self.events.append("application-named")

    def setStyleSheet(self, _stylesheet: str) -> None:
        self.events.append("stylesheet")

    def exec(self) -> int:
        self.events.append("event-loop")
        return 17


class _FakeTimer:
    events: list[str] = []

    @staticmethod
    def singleShot(_delay: int, callback) -> None:
        _FakeTimer.events.append(callback.__name__)
        _FakeApplication.events.append("guidance-scheduled")


class _FakeDatabase:
    def __init__(self, _path: str) -> None:
        _FakeApplication.events.append("database-opened")

    def get_app_setting(self, _key: str, default: str) -> str:
        return default

    def set_app_setting(self, _key: str, _value: str) -> None:
        pass


class _FakeMainWindow:
    schedules_enabled = False

    def __init__(self, *, db) -> None:
        assert isinstance(db, _FakeDatabase)
        _FakeApplication.events.append("window-created")

    def show(self) -> None:
        _FakeApplication.events.append("window-shown")

    def activate_from_external_launch(self) -> None:
        _FakeApplication.events.append("window-activated")

    def show_startup_guidance(self) -> None:
        pass

    def show_profile_setup_notice(self) -> None:
        pass

    def can_start_hidden(self) -> bool:
        return True

    def has_enabled_schedules(self) -> bool:
        return self.schedules_enabled

    def start_hidden(self) -> None:
        _FakeApplication.events.append("window-hidden")

    def request_exit(self) -> None:
        _FakeApplication.events.append("exit-requested")


class _FakeRouter:
    def __init__(self) -> None:
        _FakeApplication.events.append("router-created")
        self.handler = None

    def request_activation(self) -> None:
        if self.handler is not None:
            self.handler()

    def attach_handler(self, handler) -> None:
        self.handler = handler
        _FakeApplication.events.append("handler-attached")


class _FakeServer:
    fail_start = False

    def __init__(self, _data_dir: str, on_activation=None) -> None:
        _FakeApplication.events.append("server-created")
        self.on_activation = on_activation
        self.router = _FakeRouter()

    def start(self) -> None:
        _FakeApplication.events.append("server-started")
        if self.fail_start:
            raise RuntimeError("activation unavailable")

    def close(self) -> None:
        _FakeApplication.events.append("server-closed")


def _patch_application_dependencies(monkeypatch, tmp_path: Path) -> None:
    _FakeApplication.events = []
    _FakeTimer.events = []
    _FakeServer.fail_start = False
    _FakeMainWindow.schedules_enabled = False
    (tmp_path / "automadin.db").touch()

    monkeypatch.setattr(qt_widgets, "QApplication", _FakeApplication)
    monkeypatch.setattr(qt_core, "QTimer", _FakeTimer)
    monkeypatch.setattr(app, "install_exception_hook", lambda *_args: None)
    monkeypatch.setattr(
        app,
        "configure_runtime",
        lambda *_args: _FakeApplication.events.append("runtime-configured"),
    )
    monkeypatch.setattr(
        app,
        "_apply_pending_startup_imports",
        lambda *_args: (None, None),
    )
    monkeypatch.setattr(
        app,
        "_show_startup_import_result",
        lambda *_args: _FakeApplication.events.append("import-result-shown"),
    )
    monkeypatch.setattr(database_module, "DatabaseService", _FakeDatabase)
    monkeypatch.setattr(main_window, "MainWindow", _FakeMainWindow)
    monkeypatch.setattr(theme, "set_active_theme", lambda *_args: None)
    monkeypatch.setattr(theme, "get_stylesheet", lambda: "")

    import automadin.instance_activation as instance_activation

    monkeypatch.setattr(instance_activation, "ActivationRequestRouter", _FakeRouter)
    monkeypatch.setattr(instance_activation, "InstanceActivationServer", _FakeServer)


def test_scheduler_host_starts_hidden_when_schedules_are_enabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_application_dependencies(monkeypatch, tmp_path)
    _FakeMainWindow.schedules_enabled = True

    result = app._run_application(
        str(tmp_path),
        tmp_path / "automadin.log",
        runtime_manager=False,
        scheduler_host=True,
    )

    assert result == 17
    assert "window-hidden" in _FakeApplication.events
    assert "window-shown" not in _FakeApplication.events
    assert _FakeTimer.events == []


def test_scheduler_host_exits_when_no_schedules_are_enabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_application_dependencies(monkeypatch, tmp_path)

    result = app._run_application(
        str(tmp_path),
        tmp_path / "automadin.log",
        runtime_manager=False,
        scheduler_host=True,
    )

    assert result == 17
    assert "window-shown" not in _FakeApplication.events
    assert _FakeTimer.events == ["request_exit"]


def test_primary_starts_activation_before_runtime_and_closes_after_event_loop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_application_dependencies(monkeypatch, tmp_path)

    result = app._run_application(
        str(tmp_path),
        tmp_path / "automadin.log",
        runtime_manager=False,
    )

    events = _FakeApplication.events
    assert result == 17
    assert events.index("server-started") < events.index("runtime-configured")
    assert events.index("runtime-configured") < events.index("database-opened")
    assert events.index("window-shown") < events.index("handler-attached")
    assert events.index("import-result-shown") < events.index("guidance-scheduled")
    assert events.index("event-loop") < events.index("server-closed")
    assert _FakeTimer.events == ["show_startup_guidance"]


def test_activation_listen_failure_degrades_without_blocking_primary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_application_dependencies(monkeypatch, tmp_path)
    _FakeServer.fail_start = True

    result = app._run_application(
        str(tmp_path),
        tmp_path / "automadin.log",
        runtime_manager=False,
    )

    assert result == 17
    assert "runtime-configured" in _FakeApplication.events
    assert "window-shown" in _FakeApplication.events
    assert "server-closed" not in _FakeApplication.events


def test_real_descriptor_publication_failure_degrades_without_leaks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    real_qt_app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])

    import automadin.instance_activation as instance_activation

    real_router = instance_activation.ActivationRequestRouter
    real_server = instance_activation.InstanceActivationServer
    _patch_application_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(instance_activation, "ActivationRequestRouter", real_router)
    monkeypatch.setattr(instance_activation, "InstanceActivationServer", real_server)
    monkeypatch.setattr(
        instance_activation,
        "_atomic_write_descriptor",
        lambda *_args: (_ for _ in ()).throw(OSError("descriptor unavailable")),
    )

    result = app._run_application(
        str(tmp_path),
        tmp_path / "automadin.log",
        runtime_manager=False,
    )

    real_qt_app.processEvents()
    assert result == 17
    assert "window-shown" in _FakeApplication.events
    assert not (tmp_path / ACTIVATION_DESCRIPTOR_NAME).exists()
    assert list(tmp_path.glob(f".{ACTIVATION_DESCRIPTOR_NAME}.*.tmp")) == []


def test_runtime_manager_neither_listens_nor_opens_the_main_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_application_dependencies(monkeypatch, tmp_path)

    class _FakeRuntimeManager:
        def __init__(self, _data_dir: str) -> None:
            _FakeApplication.events.append("runtime-manager-created")

        def show(self) -> None:
            _FakeApplication.events.append("runtime-manager-shown")

    monkeypatch.setattr(runtime_manager, "RuntimeManagerDialog", _FakeRuntimeManager)

    result = app._run_application(
        str(tmp_path),
        tmp_path / "runtime-manager.log",
        runtime_manager=True,
    )

    assert result == 17
    assert "server-created" not in _FakeApplication.events
    assert "database-opened" not in _FakeApplication.events
    assert "runtime-manager-shown" in _FakeApplication.events


def test_application_boundary_uses_real_activation_server_and_cleans_descriptor(
    monkeypatch,
    tmp_path: Path,
) -> None:
    real_qt_app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])

    import automadin.instance_activation as instance_activation

    real_router = instance_activation.ActivationRequestRouter
    real_server = instance_activation.InstanceActivationServer
    real_timer = qt_core.QTimer
    _patch_application_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(qt_core, "QTimer", real_timer)
    monkeypatch.setattr(instance_activation, "ActivationRequestRouter", real_router)
    monkeypatch.setattr(instance_activation, "InstanceActivationServer", real_server)

    class ApplicationWithActivation(_FakeApplication):
        def exec(self) -> int:
            descriptor = tmp_path / ACTIVATION_DESCRIPTOR_NAME
            assert descriptor.is_file()
            script = (
                "import sys\n"
                "from automadin.instance_activation import "
                "request_existing_instance_activation\n"
                "ok = request_existing_instance_activation("
                "sys.argv[1], expected_pid=int(sys.argv[2]), timeout_ms=5000)\n"
                "raise SystemExit(0 if ok else 3)\n"
            )
            worker = subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path), str(os.getpid())]
            )
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    real_qt_app.processEvents()
                    if (
                        "window-activated" in self.events
                        and worker.poll() is not None
                    ):
                        break
                    time.sleep(0.005)
                assert worker.poll() == 0
                assert "window-activated" in self.events
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    try:
                        worker.wait(2)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait(2)
            self.events.append("event-loop")
            return 23

    monkeypatch.setattr(qt_widgets, "QApplication", ApplicationWithActivation)

    result = app._run_application(
        str(tmp_path),
        tmp_path / "automadin.log",
        runtime_manager=False,
    )

    assert result == 23
    assert not (tmp_path / ACTIVATION_DESCRIPTOR_NAME).exists()


def test_real_activation_server_is_cleaned_when_event_loop_raises(
    monkeypatch,
    tmp_path: Path,
) -> None:
    qt_widgets.QApplication.instance() or qt_widgets.QApplication([])

    import automadin.instance_activation as instance_activation

    real_router = instance_activation.ActivationRequestRouter
    real_server = instance_activation.InstanceActivationServer
    real_timer = qt_core.QTimer
    _patch_application_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(qt_core, "QTimer", real_timer)
    monkeypatch.setattr(instance_activation, "ActivationRequestRouter", real_router)
    monkeypatch.setattr(instance_activation, "InstanceActivationServer", real_server)

    class RaisingApplication(_FakeApplication):
        def exec(self) -> int:
            assert (tmp_path / ACTIVATION_DESCRIPTOR_NAME).is_file()
            raise RuntimeError("event loop failed")

    monkeypatch.setattr(qt_widgets, "QApplication", RaisingApplication)

    with pytest.raises(RuntimeError, match="event loop failed"):
        app._run_application(
            str(tmp_path),
            tmp_path / "automadin.log",
            runtime_manager=False,
        )

    assert not (tmp_path / ACTIVATION_DESCRIPTOR_NAME).exists()
