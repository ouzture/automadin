"""In-process exclusive operation leases for connected device sessions.

The registry is deliberately non-blocking: callers either acquire the selected
session immediately or receive metadata describing the operation that currently
owns it.  Coordination is process-local because device sessions themselves are
owned by the current Automadin process.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from automadin.models.device import AndroidDevice, Device, WindowsDevice


def _normalized_text(value: object, description: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{description} must be text.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{description} must be non-empty text.")
    return normalized


def _normalized_session_id(session_id: object) -> str:
    return _normalized_text(session_id, "Device session ID")


def device_operation_key(device: Device) -> str:
    """Return a stable process-wide key for one physical automation target.

    A session UUID is not stable enough because saved runs may create or reuse
    a different ``DeviceSession`` for the same device. Android provider
    identity survives ADB reconnects; a normalized window title keeps a saved
    Windows target coordinated when its HWND is refreshed.
    """
    if isinstance(device, AndroidDevice):
        return f"android:{_normalized_text(device.id, 'Android device identity')}"
    if isinstance(device, WindowsDevice):
        executable = (
            str(device.executable_path)
            .strip()
            .replace("/", "\\")
            .casefold()
        )
        if executable:
            return f"windows-app:{executable}"
        title = " ".join(str(device.title).split()).casefold()
        if title:
            return f"windows-title:{title}"
        return f"windows-hwnd:{int(device.hwnd)}"
    raise TypeError("Only Android and Windows devices use device-operation leases.")


@dataclass(frozen=True, slots=True)
class DeviceOperationUse:
    """Human-readable identity of one operation using a device session."""

    kind: str
    label: str
    operation_id: str

    def __post_init__(self) -> None:
        for field_name in ("kind", "label", "operation_id"):
            normalized = _normalized_text(
                getattr(self, field_name),
                f"Device operation {field_name}",
            )
            object.__setattr__(self, field_name, normalized)


class DeviceOperationConflictError(RuntimeError):
    """Raised when a session is already owned by another operation."""

    def __init__(
        self,
        session_id: str,
        current: DeviceOperationUse,
        requested: DeviceOperationUse,
    ) -> None:
        self.session_id = session_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"Device session {session_id} is already in use by {current.label}; "
            f"cannot start {requested.label}."
        )


@dataclass(slots=True)
class _LeaseEntry:
    generation: int
    token: str
    use: DeviceOperationUse


class DeviceOperationLeaseRegistry:
    """Thread-safe exclusive ownership keyed by normalized device session ID."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, _LeaseEntry] = {}
        self._generations: dict[str, int] = {}

    def acquire(
        self,
        session_id: str,
        use: DeviceOperationUse,
    ) -> DeviceOperationLease:
        canonical_id = _normalized_session_id(session_id)
        if not isinstance(use, DeviceOperationUse):
            raise TypeError("Device operation use must be a DeviceOperationUse.")
        with self._lock:
            current = self._entries.get(canonical_id)
            if current is not None:
                raise DeviceOperationConflictError(
                    canonical_id,
                    current.use,
                    use,
                )
            generation = self._generations.get(canonical_id, 0) + 1
            self._generations[canonical_id] = generation
            token = str(uuid4())
            self._entries[canonical_id] = _LeaseEntry(generation, token, use)
        return DeviceOperationLease(
            self,
            canonical_id,
            generation,
            token,
            use,
        )

    def snapshot(self, session_id: str) -> DeviceOperationUse | None:
        canonical_id = _normalized_session_id(session_id)
        with self._lock:
            entry = self._entries.get(canonical_id)
            return entry.use if entry is not None else None

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def _release(self, session_id: str, generation: int, token: str) -> bool:
        """Release only the exact handle generation and unguessable token."""
        with self._lock:
            current = self._entries.get(session_id)
            if (
                current is None
                or current.generation != generation
                or current.token != token
            ):
                return False
            del self._entries[session_id]
            return True


class DeviceOperationLease:
    """Idempotent handle owning one exact registry entry."""

    def __init__(
        self,
        registry: DeviceOperationLeaseRegistry,
        session_id: str,
        generation: int,
        token: str,
        use: DeviceOperationUse,
    ) -> None:
        self._registry = registry
        self.session_id = session_id
        self.generation = generation
        self._token = token
        self.use = use
        self._released = False
        self._state_lock = RLock()

    @property
    def released(self) -> bool:
        with self._state_lock:
            return self._released

    def release(self) -> bool:
        with self._state_lock:
            if self._released:
                return False
            released = self._registry._release(
                self.session_id,
                self.generation,
                self._token,
            )
            self._released = True
            return released

    close = release

    def __enter__(self) -> DeviceOperationLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


_DEFAULT_DEVICE_OPERATION_REGISTRY = DeviceOperationLeaseRegistry()


def default_device_operation_registry() -> DeviceOperationLeaseRegistry:
    """Return the process-wide production registry."""

    return _DEFAULT_DEVICE_OPERATION_REGISTRY
