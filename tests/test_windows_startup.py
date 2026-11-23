from __future__ import annotations

import subprocess
from pathlib import Path

from automadin.services.windows_startup import scheduler_startup_command


def test_packaged_scheduler_startup_command_quotes_paths(tmp_path: Path) -> None:
    executable = tmp_path / "Program Files" / "Automadin.exe"
    data_dir = tmp_path / "User Data"

    command = scheduler_startup_command(
        data_dir,
        executable=executable,
        frozen=True,
    )

    assert command == subprocess.list2cmdline(
        [
            str(executable),
            "--scheduler-host",
            "--data-dir",
            str(data_dir.resolve()),
        ]
    )

def test_source_scheduler_startup_command_uses_module_entrypoint(
    tmp_path: Path,
) -> None:
    command = scheduler_startup_command(
        tmp_path / "data",
        executable="python.exe",
        frozen=False,
    )

    assert command == subprocess.list2cmdline(
        [
            "python.exe",
            "-m",
            "automadin.app",
            "--scheduler-host",
            "--data-dir",
            str((tmp_path / "data").resolve()),
        ]
    )
