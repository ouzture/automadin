"""Chrome/Edge native-messaging bridge to the live Automadin broker."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
from pathlib import Path

from automadin.runtime import default_installed_data_dir
from automadin.services.browser_companion_broker import DESCRIPTOR_NAME


def _parent_window() -> int:
    for argument in sys.argv[1:]:
        if argument.startswith("--parent-window="):
            try:
                return int(argument.partition("=")[2])
            except ValueError:
                return 0
    return 0


def _read_native() -> dict[str, object] | None:
    size_bytes = sys.stdin.buffer.read(4)
    if not size_bytes:
        return None
    size = struct.unpack("=I", size_bytes)[0]
    if size > 1024 * 1024:
        raise ValueError("Native message exceeds 1 MiB")
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size:
        raise EOFError("Truncated native message")
    return json.loads(payload.decode("utf-8"))


def _write_native(message: dict[str, object]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(payload)))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _connect_to_broker(details: dict[str, object]) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", int(details["port"])), timeout=5)
    # The timeout bounds only connection establishment. Native hosts are long-lived;
    # leaving it on the socket silently kills the broker-to-browser reader after five
    # idle seconds while the opposite direction remains connected.
    connection.settimeout(None)
    return connection


def run_native_host() -> int:
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    descriptor = Path(default_installed_data_dir()) / DESCRIPTOR_NAME
    try:
        details = json.loads(descriptor.read_text(encoding="utf-8"))
        connection = _connect_to_broker(details)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return 2
    reader = connection.makefile("r", encoding="utf-8", newline="\n")
    writer = connection.makefile("w", encoding="utf-8", newline="\n")
    writer.write(json.dumps({"type": "auth", "token": details["token"]}) + "\n")
    writer.write(json.dumps({"type": "host_hello", "parent_window": _parent_window()}) + "\n")
    writer.flush()

    def broker_to_browser() -> None:
        try:
            for line in reader:
                _write_native(json.loads(line))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            # stdin belongs to the browser and can remain blocked indefinitely.
            # This executable is a dedicated helper, so broker loss must end the
            # whole helper immediately rather than leave Automadin.exe behind.
            os._exit(0)

    threading.Thread(target=broker_to_browser, daemon=True).start()
    try:
        while True:
            message = _read_native()
            if message is None:
                break
            writer.write(json.dumps(message, separators=(",", ":")) + "\n")
            writer.flush()
    except (OSError, ValueError, EOFError, json.JSONDecodeError):
        return 3
    return 0


__all__ = ["run_native_host"]
