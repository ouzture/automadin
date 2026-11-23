from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QIODevice
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication

from automadin.instance_activation import (
    ACK,
    ACTIVATION_DESCRIPTOR_NAME,
    ACTIVATION_PROTOCOL,
    MAX_DESCRIPTOR_BYTES,
    ActivationRequestRouter,
    InstanceActivationServer,
    _atomic_write_descriptor,
    _read_descriptor,
    activate_application_window,
    request_existing_instance_activation,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _spawn_activation_request(
    data_dir: Path,
    owner_pid: int,
    *,
    timeout_ms: int,
) -> subprocess.Popen:
    script = (
        "import sys\n"
        "from automadin.instance_activation import request_existing_instance_activation\n"
        "ok = request_existing_instance_activation("
        "sys.argv[1], expected_pid=int(sys.argv[2]), timeout_ms=int(sys.argv[3]))\n"
        "raise SystemExit(0 if ok else 3)\n"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(data_dir),
            str(owner_pid),
            str(timeout_ms),
        ]
    )


def _reap_worker(worker: subprocess.Popen) -> None:
    if worker.poll() is not None:
        worker.wait()
        return
    worker.terminate()
    try:
        worker.wait(2)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(2)


def test_real_local_server_accepts_request(tmp_path: Path) -> None:
    app = _app()
    calls: list[str] = []
    server = InstanceActivationServer(tmp_path, lambda: calls.append("activate"))
    worker: subprocess.Popen | None = None
    server.start()
    try:
        descriptor = json.loads((tmp_path / ACTIVATION_DESCRIPTOR_NAME).read_text())
        assert descriptor["protocol"] == ACTIVATION_PROTOCOL
        assert descriptor["pid"] == os.getpid()
        worker = _spawn_activation_request(tmp_path, os.getpid(), timeout_ms=5000)
        assert _wait_until(
            lambda: calls == ["activate"] and worker.poll() is not None,
            timeout=8,
        )
        assert worker.wait(2) == 0
        app.processEvents()
    finally:
        if worker is not None:
            _reap_worker(worker)
        server.close()


def test_descriptor_pid_must_match_lock_owner(tmp_path: Path) -> None:
    server = InstanceActivationServer(tmp_path)
    server.start()
    try:
        assert not request_existing_instance_activation(
            tmp_path, expected_pid=os.getpid() + 1, timeout_ms=80
        )
    finally:
        server.close()


def test_client_requires_server_acknowledgement(tmp_path: Path) -> None:
    calls: list[int] = []
    server = InstanceActivationServer(
        tmp_path,
        lambda: calls.append(1),
        send_ack=False,
    )
    worker: subprocess.Popen | None = None
    server.start()
    try:
        worker = _spawn_activation_request(tmp_path, os.getpid(), timeout_ms=400)
        assert _wait_until(lambda: worker.poll() is not None, timeout=3)
        assert worker.wait(1) == 3
        assert calls
    finally:
        if worker is not None:
            _reap_worker(worker)
        server.close()


def test_server_defers_acknowledgement_until_window_handler_is_attached(
    tmp_path: Path,
) -> None:
    server = InstanceActivationServer(tmp_path)
    worker: subprocess.Popen | None = None
    activations: list[int] = []
    server.start()
    try:
        worker = _spawn_activation_request(tmp_path, os.getpid(), timeout_ms=3000)
        assert _wait_until(lambda: server.router.pending and bool(server._clients))
        assert worker.poll() is None

        server.router.set_handler(lambda: activations.append(1))

        assert _wait_until(
            lambda: activations == [1] and worker.poll() is not None,
            timeout=4,
        )
        assert worker.wait(1) == 0
    finally:
        if worker is not None:
            _reap_worker(worker)
        server.close()


def test_malformed_and_oversized_descriptors_are_bounded(tmp_path: Path) -> None:
    descriptor = tmp_path / ACTIVATION_DESCRIPTOR_NAME
    descriptor.write_text("{bad", encoding="utf-8")
    assert not request_existing_instance_activation(tmp_path, expected_pid=os.getpid(), timeout_ms=20)
    descriptor.write_bytes(b"x" * (MAX_DESCRIPTOR_BYTES + 1))
    assert not request_existing_instance_activation(tmp_path, expected_pid=os.getpid(), timeout_ms=20)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: {**value, "unexpected": True},
        lambda value: {key: item for key, item in value.items() if key != "token"},
        lambda value: {**value, "protocol": True},
        lambda value: {**value, "pid": True},
        lambda value: {**value, "server": "automadin-untrusted"},
        lambda value: {**value, "token": "A" * 64},
        lambda value: {**value, "token": "a" * 63},
    ],
)
def test_descriptor_validation_rejects_ambiguous_fields(
    tmp_path: Path,
    mutate,
) -> None:
    owner_pid = os.getpid()
    descriptor = {
        "protocol": ACTIVATION_PROTOCOL,
        "pid": owner_pid,
        "server": f"automadin-{owner_pid}-{'b' * 32}",
        "token": "a" * 64,
    }
    (tmp_path / ACTIVATION_DESCRIPTOR_NAME).write_text(
        json.dumps(mutate(descriptor)),
        encoding="utf-8",
    )

    assert _read_descriptor(tmp_path, owner_pid) is None


def test_descriptor_validation_rejects_duplicate_fields(tmp_path: Path) -> None:
    owner_pid = os.getpid()
    raw = (
        f'{{"protocol":1,"protocol":1,"pid":{owner_pid},'
        f'"server":"automadin-{owner_pid}-{"b" * 32}",'
        f'"token":"{"a" * 64}"}}'
    )
    (tmp_path / ACTIVATION_DESCRIPTOR_NAME).write_text(raw, encoding="utf-8")

    assert _read_descriptor(tmp_path, owner_pid) is None


def test_atomic_publication_failure_preserves_previous_descriptor(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / ACTIVATION_DESCRIPTOR_NAME
    path.write_bytes(b"previous")
    monkeypatch.setattr(
        os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        _atomic_write_descriptor(
            path,
            {
                "protocol": ACTIVATION_PROTOCOL,
                "pid": os.getpid(),
                "server": f"automadin-{os.getpid()}-{'b' * 32}",
                "token": "a" * 64,
            },
        )

    assert path.read_bytes() == b"previous"
    assert list(tmp_path.glob(f".{ACTIVATION_DESCRIPTOR_NAME}.*.tmp")) == []


def test_server_closes_listener_when_descriptor_publication_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "automadin.instance_activation._atomic_write_descriptor",
        lambda *_args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    server = InstanceActivationServer(tmp_path)

    with pytest.raises(OSError, match="disk unavailable"):
        server.start()

    assert not server.listening
    assert not server.descriptor_path.exists()
    server.close()


def test_server_supports_partial_frames_and_rejects_wrong_token(tmp_path: Path) -> None:
    calls: list[int] = []
    server = InstanceActivationServer(tmp_path, lambda: calls.append(1))
    server.start()
    descriptor = json.loads(server.descriptor_path.read_text())
    try:
        bad = QLocalSocket()
        bad.connectToServer(descriptor["server"], QIODevice.OpenModeFlag.WriteOnly)
        assert bad.waitForConnected(1000)
        bad.write(f"ACTIVATE 1 {'0' * 64}\n".encode())
        bad.flush()

        good = QLocalSocket()
        good.connectToServer(descriptor["server"], QIODevice.OpenModeFlag.ReadWrite)
        assert good.waitForConnected(1000)
        frame = f"ACTIVATE 1 {descriptor['token']}\n".encode()
        good.write(frame[:7])
        good.flush()
        _app().processEvents()
        assert calls == []
        good.write(frame[7:])
        good.flush()
        assert _wait_until(lambda: calls == [1])
        assert _wait_until(lambda: good.bytesAvailable() > 0)
        assert bytes(good.readAll()) == ACK
    finally:
        server.close()


def test_raising_handler_is_rejected_and_server_remains_usable(
    tmp_path: Path,
) -> None:
    failures: list[int] = []

    def fail_activation() -> None:
        failures.append(1)
        raise RuntimeError("activation failed")

    server = InstanceActivationServer(tmp_path, fail_activation)
    server.start()
    descriptor = json.loads(server.descriptor_path.read_text())
    rejected = QLocalSocket()
    worker: subprocess.Popen | None = None
    successful: list[int] = []
    try:
        rejected.connectToServer(
            descriptor["server"],
            QIODevice.OpenModeFlag.ReadWrite,
        )
        assert rejected.waitForConnected(1000)
        rejected.write(f"ACTIVATE 1 {descriptor['token']}\n".encode())
        rejected.flush()
        assert _wait_until(lambda: failures == [1] and not server._clients)
        assert bytes(rejected.readAll()) == b""

        server.router.set_handler(lambda: successful.append(1))
        worker = _spawn_activation_request(tmp_path, os.getpid(), timeout_ms=2000)
        assert _wait_until(
            lambda: successful == [1] and worker.poll() is not None,
            timeout=4,
        )
        assert worker.wait(1) == 0
    finally:
        rejected.abort()
        if worker is not None:
            _reap_worker(worker)
        server.close()


def test_server_rejects_oversized_frame_and_expires_idle_client(tmp_path: Path) -> None:
    calls: list[int] = []
    server = InstanceActivationServer(
        tmp_path, lambda: calls.append(1), idle_milliseconds=30
    )
    server.start()
    descriptor = json.loads(server.descriptor_path.read_text())
    oversized = QLocalSocket()
    idle = QLocalSocket()
    try:
        oversized.connectToServer(descriptor["server"], QIODevice.OpenModeFlag.WriteOnly)
        idle.connectToServer(descriptor["server"], QIODevice.OpenModeFlag.WriteOnly)
        assert oversized.waitForConnected(1000)
        assert idle.waitForConnected(1000)
        oversized.write(b"x" * 129)
        oversized.flush()
        assert _wait_until(lambda: not server._clients)
        assert calls == []
    finally:
        server.close()


def test_server_caps_burst_of_active_clients(tmp_path: Path) -> None:
    server = InstanceActivationServer(tmp_path, max_active_clients=2, idle_milliseconds=500)
    server.start()
    descriptor = json.loads(server.descriptor_path.read_text())
    sockets = [QLocalSocket() for _ in range(8)]
    try:
        for socket in sockets:
            socket.connectToServer(descriptor["server"], QIODevice.OpenModeFlag.WriteOnly)
            assert socket.waitForConnected(1000)
        assert _wait_until(lambda: len(server._clients) == 2)
    finally:
        for socket in sockets:
            socket.abort()
        server.close()


def test_close_only_removes_own_token_descriptor(tmp_path: Path) -> None:
    server = InstanceActivationServer(tmp_path)
    server.start()
    replacement = {
        "protocol": ACTIVATION_PROTOCOL,
        "pid": os.getpid(),
        "server": f"automadin-{os.getpid()}-{'b' * 32}",
        "token": "a" * 64,
    }
    server.descriptor_path.write_text(json.dumps(replacement), encoding="utf-8")
    server.close()
    assert json.loads(server.descriptor_path.read_text()) == replacement


def test_descriptor_cleanup_error_does_not_escape_shutdown(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    server = InstanceActivationServer(tmp_path)
    server.start()
    original_unlink = Path.unlink

    def fail_descriptor_unlink(path: Path, *args, **kwargs) -> None:
        if path == server.descriptor_path:
            raise PermissionError("descriptor is temporarily busy")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_descriptor_unlink)

    server.close()

    assert not server.listening
    assert "Could not remove the stale activation descriptor" in caplog.text


def test_router_collapses_requests_before_handler() -> None:
    calls: list[int] = []
    router = ActivationRequestRouter()
    router.request()
    router.request()
    assert router.pending
    router.set_handler(lambda: calls.append(1))
    assert calls == [1]
    router.request()
    assert calls == [1, 1]


def test_router_defers_completions_and_reports_handler_outcome() -> None:
    completions: list[bool] = []
    calls: list[int] = []
    router = ActivationRequestRouter()

    assert not router.request(completions.append)
    assert completions == []
    router.set_handler(lambda: calls.append(1))

    assert calls == [1]
    assert completions == [True]

    failing = ActivationRequestRouter()
    assert not failing.request(completions.append)
    with pytest.raises(RuntimeError, match="cannot activate"):
        failing.set_handler(
            lambda: (_ for _ in ()).throw(RuntimeError("cannot activate"))
        )
    assert completions == [True, False]


class _FakeWindow:
    def __init__(
        self,
        *,
        fullscreen: bool = False,
        maximized: bool = False,
        minimized: bool = False,
        visible: bool = True,
        active: bool = True,
    ) -> None:
        self.fullscreen = fullscreen
        self.maximized = maximized
        self.minimized = minimized
        self.visible = visible
        self.active = active
        self.calls: list[str] = []

    def isFullScreen(self) -> bool:
        return self.fullscreen

    def isMaximized(self) -> bool:
        return self.maximized

    def isMinimized(self) -> bool:
        return self.minimized

    def isVisible(self) -> bool:
        return self.visible

    def showFullScreen(self) -> None:
        self.calls.append("fullscreen")

    def showMaximized(self) -> None:
        self.calls.append("maximized")

    def showNormal(self) -> None:
        self.calls.append("normal")

    def show(self) -> None:
        self.calls.append("show")

    def raise_(self) -> None:
        self.calls.append("raise")

    def activateWindow(self) -> None:
        self.calls.append("activate")

    def isActiveWindow(self) -> bool:
        return self.active


class _FakeApp:
    def __init__(self, modal=None, active=None) -> None:
        self.modal = modal
        self.active = active
        self.alerts: list[tuple[object, int]] = []

    def activeModalWidget(self):
        return self.modal

    def activeWindow(self):
        return self.active

    def alert(self, target, duration: int) -> None:
        self.alerts.append((target, duration))


def test_window_activation_targets_modal_and_preserves_maximized_state(monkeypatch) -> None:
    main = _FakeWindow(maximized=True, minimized=True)
    modal = _FakeWindow(maximized=True, minimized=True)
    app = _FakeApp(modal=modal)

    class FakeQApplication:
        @staticmethod
        def instance():
            return app

    monkeypatch.setattr("PySide6.QtWidgets.QApplication", FakeQApplication)
    assert activate_application_window(main)
    assert main.calls == ["maximized"]
    assert modal.calls == ["maximized", "raise", "activate"]


def test_window_activation_uses_qt_alert_fallback(monkeypatch) -> None:
    window = _FakeWindow(active=False)
    app = _FakeApp(active=window)

    class FakeQApplication:
        @staticmethod
        def instance():
            return app

    monkeypatch.setattr("PySide6.QtWidgets.QApplication", FakeQApplication)
    assert activate_application_window(window)
    assert window.calls == ["show", "raise", "activate"]
    assert app.alerts == [(window, 0)]
