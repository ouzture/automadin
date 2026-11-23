from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


ADB_SUBPROCESS_FILES = (
    "automadin/engine/adb_engine.py",
    "automadin/engine/device_controller.py",
    "automadin/execution/coordinates.py",
    "automadin/execution/handlers/actions.py",
    "automadin/execution/handlers/click.py",
    "automadin/execution/handlers/type_text.py",
    "automadin/ui/adb_path_dialog.py",
)


def test_hidden_process_flag_matches_platform() -> None:
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    assert HIDDEN_PROCESS_CREATION_FLAGS == expected


def test_every_adb_subprocess_run_hides_its_window() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    unhidden: list[str] = []
    call_count = 0
    for relative_path in ADB_SUBPROCESS_FILES:
        path = repo_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr == "run"
            ):
                continue
            call_count += 1
            if not any(keyword.arg == "creationflags" for keyword in node.keywords):
                unhidden.append(f"{relative_path}:{node.lineno}")

    # Input operations now share AndroidDeviceController._shell, so moving an
    # operation behind the controller reduces duplicate subprocess.run sites.
    assert call_count == 13
    assert unhidden == []
