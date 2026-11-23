"""Qt-free packaged release gate for the browser-health supervisor."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from automadin.runtime import application_dir, is_frozen
from automadin.services.browser_health import BrowserHealthState, check_browser_launch

RELEASE_SELF_TEST_OK = 0
RELEASE_SELF_TEST_INVALID_PARENT = 64
RELEASE_SELF_TEST_SETUP_FAILED = 65
RELEASE_SELF_TEST_CHECK_FAILED = 66
RELEASE_SELF_TEST_UNEXPECTED_RESULT = 67
RELEASE_SELF_TEST_RESIDUE_DETECTED = 68
RELEASE_SELF_TEST_CLEANUP_FAILED = 69

_SELF_TEST_PREFIX = "automadin-release-browser-health-"


def browser_health_worker_command(target: str) -> list[str]:
    """Build the worker recursion without importing the Qt-backed runtime manager."""
    arguments = ["--browser-health-worker", target]
    candidates = (
        sys.argv[0] if sys.argv else "",
        str(application_dir() / "Automadin.exe"),
        sys.executable,
    )
    for value in candidates:
        if not value:
            continue
        candidate = Path(value).resolve()
        if candidate.name.casefold() == "automadin.exe" and candidate.is_file():
            return [str(candidate), *arguments]
    if is_frozen():
        raise RuntimeError(
            "Automadin could not locate its installed executable. "
            "Please repair or reinstall the application."
        )
    return [
        str(Path(sys.executable).resolve()),
        str(application_dir() / "main.py"),
        *arguments,
    ]


def _validated_existing_parent(raw_parent: str | Path) -> Path:
    parent = Path(raw_parent).expanduser().resolve(strict=True)
    if not parent.is_dir():
        raise NotADirectoryError(f"Release self-test parent is not a directory: {parent}")
    return parent


def run_release_browser_health_self_test(safe_temp_parent: str | Path) -> int:
    """Exercise the real supervisor/worker recursion with a missing Chromium payload.

    The caller supplies an existing directory that is safe for temporary children.
    The deliberately empty Playwright browser directory makes ``unavailable`` at
    the worker's ``launch`` stage the only passing health result.
    """

    try:
        parent = _validated_existing_parent(safe_temp_parent)
    except (OSError, ValueError):
        return RELEASE_SELF_TEST_INVALID_PARENT

    root: Path | None = None
    outcome = RELEASE_SELF_TEST_SETUP_FAILED
    try:
        root = Path(tempfile.mkdtemp(prefix=_SELF_TEST_PREFIX, dir=parent))
        supervisor_parent = root / "supervisor"
        empty_browsers = root / "empty-playwright-browsers"
        supervisor_parent.mkdir()
        empty_browsers.mkdir()

        try:
            result = check_browser_launch(
                "chromium",
                safe_temp_parent=supervisor_parent,
                timeout=15.0,
                command_builder=browser_health_worker_command,
                playwright_browsers_path=empty_browsers,
            )
        except Exception:
            outcome = RELEASE_SELF_TEST_CHECK_FAILED
        else:
            expected_result = (
                result.state is BrowserHealthState.UNAVAILABLE
                and result.stage == "launch"
            )
            supervisor_clean = not any(supervisor_parent.iterdir())
            browsers_clean = not any(empty_browsers.iterdir())
            if not supervisor_clean or not browsers_clean:
                outcome = RELEASE_SELF_TEST_RESIDUE_DETECTED
            elif not expected_result:
                outcome = RELEASE_SELF_TEST_UNEXPECTED_RESULT
            else:
                outcome = RELEASE_SELF_TEST_OK
    except (OSError, ValueError):
        outcome = RELEASE_SELF_TEST_SETUP_FAILED
    finally:
        if root is not None:
            try:
                shutil.rmtree(root)
            except OSError:
                outcome = RELEASE_SELF_TEST_CLEANUP_FAILED
            else:
                if root.exists():
                    outcome = RELEASE_SELF_TEST_CLEANUP_FAILED

    return outcome
