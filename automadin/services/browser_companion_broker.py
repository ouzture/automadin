"""Token-authenticated loopback broker for browser companion native hosts."""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automadin.runtime import default_installed_data_dir

DESCRIPTOR_NAME = "browser-companion-broker.json"
PROTOCOL_VERSION = 4
_MIN_PROTOCOL_BY_OPERATION = {
    "evaluate": 2,
    "click": 3,
    "click_position": 3,
    "fill": 3,
    "type": 3,
    "press": 4,
}


@dataclass(frozen=True, slots=True)
class CompanionTarget:
    client_id: str
    target_id: str
    title: str
    url: str
    parent_window: int
    engine: str = "chrome"


@dataclass
class _Client:
    client_id: str
    connection: socket.socket
    writer: Any
    parent_window: int = 0
    target: CompanionTarget | None = None
    engine: str = "chrome"
    protocol: int = 0
    pending: dict[str, queue.Queue[dict[str, Any]]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class BrowserCompanionBroker:
    def __init__(self, descriptor_root: str | Path | None = None) -> None:
        self._root = Path(descriptor_root or default_installed_data_dir()).resolve()
        self._token = secrets.token_urlsafe(32)
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self._clients: dict[str, _Client] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)

    @property
    def descriptor_path(self) -> Path:
        return self._root / DESCRIPTOR_NAME

    def start(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "port": self._server.getsockname()[1],
            "token": self._token,
        }
        temporary = self.descriptor_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, self.descriptor_path)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        try:
            self._server.close()
        except OSError:
            pass
        with self._lock:
            connections = [client.connection for client in self._clients.values()]
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        try:
            self.descriptor_path.unlink(missing_ok=True)
        except OSError:
            pass

    def targets(self) -> list[CompanionTarget]:
        with self._lock:
            return [client.target for client in self._clients.values() if client.target]

    def request(
        self,
        client_id: str,
        operation: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        with self._lock:
            client = self._clients.get(client_id)
        if client is None:
            raise RuntimeError("The browser companion disconnected")
        minimum_protocol = _MIN_PROTOCOL_BY_OPERATION.get(operation, 1)
        if client.protocol < minimum_protocol:
            action = (
                "click Reload, enable Allow User Scripts, then reconnect the webpage tab."
                if operation == "evaluate"
                else "click Reload, then reconnect the webpage tab."
            )
            raise RuntimeError(
                "The Browser Companion extension is outdated. Open its details "
                f"in edge://extensions or chrome://extensions, {action}"
            )
        request_id = uuid.uuid4().hex
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        client.pending[request_id] = response_queue
        try:
            with client.lock:
                client.writer.write(
                    json.dumps(
                        {
                            "type": "request",
                            "id": request_id,
                            "op": operation,
                            "params": params or {},
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                client.writer.flush()
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"Browser companion operation timed out: {operation}") from exc
        finally:
            client.pending.pop(request_id, None)
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response.get("result")

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        client: _Client | None = None
        reader = connection.makefile("r", encoding="utf-8", newline="\n")
        writer = connection.makefile("w", encoding="utf-8", newline="\n")
        try:
            auth = json.loads(reader.readline())
            if auth != {"type": "auth", "token": self._token}:
                return
            client = _Client(uuid.uuid4().hex, connection, writer)
            with self._lock:
                self._clients[client.client_id] = client
            for line in reader:
                message = json.loads(line)
                kind = message.get("type")
                if kind == "host_hello":
                    client.parent_window = int(message.get("parent_window") or 0)
                elif kind == "hello":
                    client.protocol = int(message.get("protocol") or 0)
                    user_agent = str(message.get("user_agent") or "")
                    client.engine = "msedge" if "Edg/" in user_agent else "chrome"
                elif kind == "granted":
                    client.target = CompanionTarget(
                        client.client_id,
                        str(message["target_id"]),
                        str(message.get("title") or "Browser tab"),
                        str(message.get("url") or ""),
                        client.parent_window,
                        client.engine,
                    )
                elif kind == "revoked":
                    client.target = None
                elif kind == "response":
                    pending = client.pending.get(str(message.get("id")))
                    if pending is not None:
                        pending.put(message)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
        finally:
            if client is not None:
                with self._lock:
                    self._clients.pop(client.client_id, None)
            try:
                connection.close()
            except OSError:
                pass


_broker: BrowserCompanionBroker | None = None


def start_browser_companion_broker() -> BrowserCompanionBroker:
    global _broker
    if _broker is None:
        _broker = BrowserCompanionBroker()
        _broker.start()
    return _broker


def browser_companion_broker() -> BrowserCompanionBroker | None:
    return _broker


__all__ = [
    "BrowserCompanionBroker",
    "CompanionTarget",
    "browser_companion_broker",
    "start_browser_companion_broker",
]
