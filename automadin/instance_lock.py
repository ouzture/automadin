"""Single-instance protection scoped to one canonical Automadin data directory.

This is deliberately the data-integrity ownership authority, not an IPC service.
Existing-window activation is delegated to ``automadin.instance_activation``; when
that best-effort channel is unavailable, the lock still provides the safe in-use
diagnostic fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

LOCK_FILE_NAME = ".automadin.lock"


def canonical_data_directory(data_dir: str | Path) -> Path:
    """Return one stable filesystem identity for a data-directory path."""
    resolved = Path(data_dir).expanduser().resolve()
    # normcase is significant on Windows, where path spelling/case aliases must
    # still contend for the same lock.  It is intentionally a no-op on POSIX.
    return Path(os.path.normcase(os.path.normpath(str(resolved))))


@dataclass(frozen=True)
class LockOwner:
    """Best-effort ownership details recorded by QLockFile."""

    pid: int
    hostname: str
    application: str


class DataDirectoryLockError(RuntimeError):
    """Base class for failures to protect a data directory."""


class DataDirectoryInUseError(DataDirectoryLockError):
    """Raised when a live process already owns the data-directory lock."""

    def __init__(self, data_dir: Path, lock_path: Path, owner: LockOwner) -> None:
        self.data_dir = data_dir
        self.lock_path = lock_path
        self.owner = owner
        owner_note = f" (PID {owner.pid})" if owner.pid > 0 else ""
        super().__init__(f"The Automadin data directory is already in use{owner_note}: {data_dir}")


class DataDirectoryLock:
    """Own a QLockFile for the lifetime of one Automadin process."""

    def __init__(self, data_dir: str | Path) -> None:
        # Import lazily so a Qt import failure itself remains inside app.main's
        # startup exception boundary and can be reported through diagnostics.
        from PySide6.QtCore import QLockFile

        self.data_dir = canonical_data_directory(data_dir)
        self.lock_path = self.data_dir / LOCK_FILE_NAME
        self._lock = QLockFile(str(self.lock_path))
        # Disable age-only expiry.  Qt still detects a confirmed dead owner and
        # can safely clear that crashed-process lock, but never steals from a
        # healthy long-running Automadin instance merely because it is old.
        self._lock.setStaleLockTime(0)
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> None:
        """Acquire immediately or raise a specific, actionable failure."""
        if self._acquired:
            return
        if self._lock.tryLock(0):
            self._acquired = True
            return

        from PySide6.QtCore import QLockFile

        error = self._lock.error()
        pid, hostname, application = self._lock.getLockInfo()
        owner = LockOwner(pid=pid, hostname=hostname, application=application)
        if error == QLockFile.LockError.LockFailedError:
            raise DataDirectoryInUseError(self.data_dir, self.lock_path, owner)
        if error == QLockFile.LockError.PermissionError:
            raise DataDirectoryLockError(
                f"Automadin cannot create its instance lock in {self.data_dir}. "
                "Check that the data directory is writable."
            )
        raise DataDirectoryLockError(
            f"Automadin could not protect the data directory {self.data_dir} "
            f"(lock error: {error.name})."
        )

    def release(self) -> None:
        """Release the lock if held; safe to call during partial startup."""
        if not self._acquired:
            return
        self._lock.unlock()
        self._acquired = False

    def __enter__(self) -> DataDirectoryLock:
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()
