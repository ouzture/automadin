"""Authenticated local IPC used to bring an existing Automadin window forward.

The data-directory :class:`~automadin.instance_lock.DataDirectoryLock` is the sole
authority for instance ownership.  This module deliberately neither acquires nor
steals that lock; its small descriptor only tells a rejected process how to contact
the PID reported by the lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ACTIVATION_DESCRIPTOR_NAME = ".automadin.activation.json"
ACTIVATION_PROTOCOL = 1
MAX_DESCRIPTOR_BYTES = 2048
TOKEN_HEX_BYTES = 32
TOKEN_LENGTH = TOKEN_HEX_BYTES * 2
MAX_FRAME_BYTES = 128
MAX_ACTIVE_CLIENTS = 32
CLIENT_DEFAULT_TIMEOUT_MILLISECONDS = 5000
CLIENT_MAX_TIMEOUT_MILLISECONDS = 10_000
CLIENT_ATTEMPT_MILLISECONDS = 200
CLIENT_RETRY_SECONDS = 0.04
SERVER_IDLE_MILLISECONDS = 3000
ACK = b"OK\n"

logger = logging.getLogger(__name__)


def _descriptor_path(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser().resolve() / ACTIVATION_DESCRIPTOR_NAME


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate descriptor field: {key}")
        result[key] = value
    return result


def _read_descriptor(data_dir: str | Path, lock_owner_pid: int) -> dict[str, Any] | None:
    """Read and strictly validate the bounded, untrusted endpoint descriptor."""
    path = _descriptor_path(data_dir)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_DESCRIPTOR_BYTES + 1)
    except (OSError, ValueError):
        return None
    if not raw or len(raw) > MAX_DESCRIPTOR_BYTES:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"protocol", "pid", "server", "token"}:
        return None
    protocol = value["protocol"]
    pid = value["pid"]
    server = value["server"]
    token = value["token"]
    if type(protocol) is not int or protocol != ACTIVATION_PROTOCOL:
        return None
    if type(pid) is not int or pid <= 0 or pid != int(lock_owner_pid):
        return None
    if not isinstance(server, str) or re.fullmatch(
        rf"automadin-{pid}-[0-9a-f]{{32}}", server
    ) is None:
        return None
    if (
        not isinstance(token, str)
        or len(token) != TOKEN_LENGTH
        or any(character not in "0123456789abcdef" for character in token)
    ):
        return None
    return value


def _atomic_write_descriptor(path: Path, descriptor: dict[str, Any]) -> None:
    encoded = json.dumps(descriptor, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("activation descriptor exceeds its size limit")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor_fd: int | None = None
    try:
        descriptor_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor_fd, "wb") as stream:
            descriptor_fd = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor_fd is not None:
            os.close(descriptor_fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _activation_frame(token: str) -> bytes:
    frame = f"ACTIVATE {ACTIVATION_PROTOCOL} {token}\n".encode("ascii")
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("activation frame exceeds its size limit")
    return frame


def _allow_set_foreground_window(pid: int) -> None:
    """Best-effort Windows foreground permission transfer."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(int(pid))
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def request_existing_instance_activation(
    data_dir: str | Path,
    *,
    expected_pid: int,
    timeout_ms: int = CLIENT_DEFAULT_TIMEOUT_MILLISECONDS,
) -> bool:
    """Ask the lock owner to activate, retrying within one bounded total deadline.

    Success means the lock owner's activation server authenticated and accepted the
    request.  The acknowledgement may be delayed while a newly starting primary
    process completes initialization and enters Qt's event loop.
    """
    from PySide6.QtCore import QIODevice
    from PySide6.QtNetwork import QLocalSocket

    try:
        owner_pid = int(expected_pid)
        bounded_timeout_ms = max(
            0,
            min(int(timeout_ms), CLIENT_MAX_TIMEOUT_MILLISECONDS),
        )
    except (OverflowError, TypeError, ValueError):
        return False
    if owner_pid <= 0:
        return False
    deadline = time.monotonic() + (bounded_timeout_ms / 1000)
    _allow_set_foreground_window(owner_pid)
    while True:
        descriptor = _read_descriptor(data_dir, owner_pid)
        if descriptor is not None:
            frame = _activation_frame(descriptor["token"])
            socket = QLocalSocket()
            socket.connectToServer(descriptor["server"], QIODevice.OpenModeFlag.ReadWrite)
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            attempt_ms = min(CLIENT_ATTEMPT_MILLISECONDS, remaining_ms)
            if attempt_ms > 0 and socket.waitForConnected(attempt_ms):
                accepted = socket.write(frame)
                if accepted == len(frame):
                    socket.flush()
                    remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                    written = socket.bytesToWrite() == 0
                    if not written and remaining_ms > 0:
                        written = socket.waitForBytesWritten(remaining_ms)
                    if written or socket.bytesToWrite() == 0:
                        reply = bytearray()
                        while len(reply) < len(ACK):
                            available = socket.bytesAvailable()
                            if available:
                                remaining_reply = len(ACK) - len(reply)
                                reply.extend(
                                    bytes(socket.read(min(available, remaining_reply + 1)))
                                )
                                continue
                            remaining_ms = max(
                                0,
                                int((deadline - time.monotonic()) * 1000),
                            )
                            if remaining_ms <= 0 or not socket.waitForReadyRead(
                                remaining_ms
                            ):
                                break
                        if bytes(reply) == ACK and socket.bytesAvailable() == 0:
                            socket.disconnectFromServer()
                            return True
            socket.abort()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(CLIENT_RETRY_SECONDS, remaining))


class ActivationRequestRouter:
    """Route activation requests, coalescing all requests received before binding."""

    def __init__(self, handler: Callable[[], None] | None = None) -> None:
        self._handler = handler
        self._pending = False
        self._pending_completions: list[Callable[[bool], None]] = []

    @property
    def pending(self) -> bool:
        return self._pending

    def request(self, completion: Callable[[bool], None] | None = None) -> bool:
        handler = self._handler
        if handler is None:
            self._pending = True
            if completion is not None:
                self._pending_completions.append(completion)
            return False
        try:
            handler()
        except BaseException:
            if completion is not None:
                completion(False)
            raise
        if completion is not None:
            completion(True)
        return True

    def request_activation(
        self,
        completion: Callable[[bool], None] | None = None,
    ) -> bool:
        return self.request(completion)

    def set_handler(self, handler: Callable[[], None]) -> None:
        self._handler = handler
        if not self._pending:
            return
        self._pending = False
        completions, self._pending_completions = self._pending_completions, []
        try:
            handler()
        except BaseException:
            for completion in completions:
                completion(False)
            raise
        for completion in completions:
            completion(True)

    def attach_handler(self, handler: Callable[[], None]) -> None:
        self.set_handler(handler)

    def clear_handler(self) -> None:
        self._handler = None

    def cancel_pending(self) -> None:
        self._pending = False
        completions, self._pending_completions = self._pending_completions, []
        for completion in completions:
            completion(False)


class InstanceActivationServer:
    """One-process local activation listener.

    The caller must already own the external QLockFile and retain it until this
    server closes.  Each connection may carry exactly one fixed-format request.
    """

    def __init__(
        self,
        data_dir: str | Path,
        on_activation: Callable[[], None] | None = None,
        *,
        idle_milliseconds: int = SERVER_IDLE_MILLISECONDS,
        max_active_clients: int = MAX_ACTIVE_CLIENTS,
        send_ack: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.pid = os.getpid()
        self.router = ActivationRequestRouter(on_activation)
        self.idle_milliseconds = max(1, int(idle_milliseconds))
        self.max_active_clients = max(1, min(int(max_active_clients), MAX_ACTIVE_CLIENTS))
        self.send_ack = bool(send_ack)
        self.token = secrets.token_hex(TOKEN_HEX_BYTES)
        self.server_name = f"automadin-{self.pid}-{secrets.token_hex(16)}"
        self.descriptor_path = _descriptor_path(self.data_dir)
        self._server: Any | None = None
        self._clients: dict[Any, tuple[bytearray, Any]] = {}

    @property
    def listening(self) -> bool:
        return bool(self._server is not None and self._server.isListening())

    def start(self) -> None:
        from PySide6.QtNetwork import QLocalServer

        if self.listening:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(self._drain_pending_connections)
        if not server.listen(self.server_name):
            raise RuntimeError(f"could not start activation server: {server.errorString()}")
        self._server = server
        descriptor = {
            "protocol": ACTIVATION_PROTOCOL,
            "pid": self.pid,
            "server": self.server_name,
            "token": self.token,
        }
        try:
            _atomic_write_descriptor(self.descriptor_path, descriptor)
        except BaseException:
            server.close()
            self._server = None
            raise

    def _drain_pending_connections(self) -> None:
        from PySide6.QtCore import QTimer

        server = self._server
        if server is None:
            return
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            if socket is None:
                continue
            if len(self._clients) >= self.max_active_clients:
                socket.abort()
                socket.deleteLater()
                continue
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.setInterval(self.idle_milliseconds)
            buffer = bytearray()
            self._clients[socket] = (buffer, timer)
            socket.readyRead.connect(lambda current=socket: self._read_client(current))
            socket.disconnected.connect(lambda current=socket: self._drop_client(current))
            timer.timeout.connect(lambda current=socket: self._reject_client(current))
            timer.start()
            self._read_client(socket)

    def _read_client(self, socket: Any) -> None:
        state = self._clients.get(socket)
        if state is None:
            return
        buffer, timer = state
        incoming = bytes(socket.readAll())
        if incoming:
            buffer.extend(incoming)
            timer.start()
        expected = _activation_frame(self.token)
        if len(buffer) > len(expected) or len(buffer) > MAX_FRAME_BYTES:
            self._reject_client(socket)
            return
        if not expected.startswith(buffer):
            self._reject_client(socket)
            return
        if len(buffer) != len(expected):
            return
        timer.stop()
        try:
            self.router.request(
                lambda accepted, current=socket: self._complete_client(
                    current,
                    accepted,
                )
            )
        except Exception:
            logger.exception("Existing-window activation handler failed.")

    def _complete_client(self, socket: Any, accepted: bool) -> None:
        if socket not in self._clients:
            return
        if accepted and self.send_ack:
            socket.write(ACK)
            socket.flush()
        try:
            socket.disconnectFromServer()
        finally:
            self._drop_client(socket)

    def _reject_client(self, socket: Any) -> None:
        try:
            socket.abort()
        finally:
            self._drop_client(socket)

    def _drop_client(self, socket: Any) -> None:
        state = self._clients.pop(socket, None)
        if state is not None:
            state[1].stop()
        socket.deleteLater()

    def close(self) -> None:
        self.router.cancel_pending()
        for socket in list(self._clients):
            self._reject_client(socket)
        if self._server is not None:
            self._server.close()
            self._server = None
        descriptor = _read_descriptor(self.data_dir, self.pid)
        if descriptor is not None and secrets.compare_digest(descriptor["token"], self.token):
            try:
                self.descriptor_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "Could not remove the stale activation descriptor %s: %s",
                    self.descriptor_path,
                    exc,
                )

    def __enter__(self) -> InstanceActivationServer:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def activate_application_window(window: Any) -> bool:
    """Restore and foreground the best Automadin window without changing its mode."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return False
    if window is None:
        return False

    state = window.windowState() if hasattr(window, "windowState") else None
    fullscreen = (
        bool(state & Qt.WindowState.WindowFullScreen)
        if state is not None
        else bool(window.isFullScreen())
    )
    maximized = (
        bool(state & Qt.WindowState.WindowMaximized)
        if state is not None
        else bool(window.isMaximized())
    )
    if fullscreen:
        window.showFullScreen()
    elif maximized:
        window.showMaximized()
    elif window.isMinimized() or not window.isVisible():
        window.showNormal()
    else:
        window.show()

    target = app.activeModalWidget()
    if target is None:
        target = window
    if target is not window and (target.isMinimized() or not target.isVisible()):
        if target.isFullScreen():
            target.showFullScreen()
        elif target.isMaximized():
            target.showMaximized()
        else:
            target.showNormal()
    target.raise_()
    target.activateWindow()
    if not target.isActiveWindow():
        app.alert(target, 0)
    return True


# Compatibility aliases kept intentionally cheap for downstream callers.
ActivationServer = InstanceActivationServer
request_activation = request_existing_instance_activation
send_activation_request = request_existing_instance_activation
