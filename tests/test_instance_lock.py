from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from automadin.instance_lock import DataDirectoryInUseError, DataDirectoryLock


def test_same_canonical_data_directory_rejects_second_owner(tmp_path: Path) -> None:
    data_dir = tmp_path / "Shared Data"
    alias_child = data_dir / "path-alias"
    alias_child.mkdir(parents=True)
    first = DataDirectoryLock(data_dir)
    second = DataDirectoryLock(alias_child / "..")

    try:
        first.acquire()

        with pytest.raises(DataDirectoryInUseError) as result:
            second.acquire()

        assert result.value.data_dir == first.data_dir
        assert result.value.lock_path == first.lock_path
        assert first.acquired is True
        assert first.lock_path.is_file()
    finally:
        second.release()
        first.release()


def test_different_data_directories_can_run_at_the_same_time(tmp_path: Path) -> None:
    first = DataDirectoryLock(tmp_path / "first")
    second = DataDirectoryLock(tmp_path / "second")
    first.data_dir.mkdir(parents=True)
    second.data_dir.mkdir(parents=True)

    try:
        first.acquire()
        second.acquire()

        assert first.acquired is True
        assert second.acquired is True
        assert first.lock_path != second.lock_path
    finally:
        second.release()
        first.release()


def test_lock_is_held_for_owner_lifetime_and_released_explicitly(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    first = DataDirectoryLock(data_dir)
    successor = DataDirectoryLock(data_dir)

    first.acquire()
    with pytest.raises(DataDirectoryInUseError):
        successor.acquire()

    first.release()
    successor.acquire()
    try:
        assert successor.acquired is True
    finally:
        successor.release()

    assert first.acquired is False
    assert successor.acquired is False
    assert not first.lock_path.exists()


def test_lock_left_by_crashed_process_is_recovered(tmp_path: Path) -> None:
    data_dir = tmp_path / "crash-recovery"
    data_dir.mkdir()
    lock_path = data_dir / ".automadin.lock"
    script = (
        "import os, sys\n"
        "from PySide6.QtCore import QLockFile\n"
        "lock = QLockFile(sys.argv[1])\n"
        "lock.setStaleLockTime(0)\n"
        "if not lock.tryLock(0): raise SystemExit(3)\n"
        "os._exit(0)\n"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(lock_path)],
        check=True,
        timeout=15,
    )
    assert lock_path.is_file()

    recovered = DataDirectoryLock(data_dir)
    recovered.acquire()
    try:
        assert recovered.acquired is True
    finally:
        recovered.release()
