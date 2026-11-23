"""Packaged release gate for the Diagnostics support-report workflow."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

DIAGNOSTICS_SELF_TEST_OK = 0
DIAGNOSTICS_SELF_TEST_INVALID_PARENT = 70
DIAGNOSTICS_SELF_TEST_SETUP_FAILED = 71
DIAGNOSTICS_SELF_TEST_REPORT_FAILED = 72
DIAGNOSTICS_SELF_TEST_UI_FAILED = 73
DIAGNOSTICS_SELF_TEST_CLEANUP_FAILED = 74
DIAGNOSTICS_SELF_TEST_INVALID_NONCE = 75

_SELF_TEST_PREFIX = "automadin-release-diagnostics-"
_RESULT_PROTOCOL = "automadin-diagnostics-self-test-v1"
_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}", re.ASCII)
_PRIVATE_PROFILE_NAME = "Private Diagnostics Profile"
_PRIVATE_ACCOUNT_EMAIL = "diagnostics-private@example.invalid"
_PRIVATE_LOG_MESSAGE = "private diagnostics log message"


class _MemoryClipboard:
    """Small clipboard double that keeps the release gate self-contained."""

    def __init__(self) -> None:
        self.value = ""

    def setText(self, text: str, _mode: object = None) -> None:
        self.value = text

    def text(self, _mode: object = None) -> str:
        return self.value


def _validated_existing_parent(raw_parent: str | Path) -> Path:
    candidate = Path(raw_parent).expanduser()
    information = candidate.lstat()
    is_reparse_point = bool(
        stat.S_ISLNK(information.st_mode)
        or getattr(information, "st_file_attributes", 0) & 0x0400
    )
    if is_reparse_point or not stat.S_ISDIR(information.st_mode):
        raise NotADirectoryError(
            f"Diagnostics self-test parent is not a plain directory: {candidate}"
        )
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(
            f"Diagnostics self-test parent is not a directory: {resolved}"
        )
    return resolved


def diagnostics_self_test_marker(
    safe_temp_parent: str | Path,
    nonce: str,
) -> Path:
    """Return the nonce-bound result path or reject an invalid nonce."""
    if not isinstance(nonce, str) or not _NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("Diagnostics self-test nonce is invalid.")
    return Path(safe_temp_parent) / f"diagnostics-{nonce}.ok"


def diagnostics_self_test_marker_text(nonce: str) -> str:
    """Return the exact result marker content expected by the build."""
    diagnostics_self_test_marker(Path("."), nonce)
    return f"{_RESULT_PROTOCOL}:{nonce}\n"


def run_release_diagnostics_self_test(
    safe_temp_parent: str | Path,
    *,
    nonce: str | None = None,
) -> int:
    """Exercise report collection, dialog preview/copy, and packaged metadata."""
    try:
        parent = _validated_existing_parent(safe_temp_parent)
    except (OSError, ValueError):
        return DIAGNOSTICS_SELF_TEST_INVALID_PARENT
    marker: Path | None = None
    if nonce is not None:
        try:
            marker = diagnostics_self_test_marker(parent, nonce)
        except ValueError:
            return DIAGNOSTICS_SELF_TEST_INVALID_NONCE
        if marker.exists():
            return DIAGNOSTICS_SELF_TEST_INVALID_NONCE

    root: Path | None = None
    database = None
    dialog = None
    outcome = DIAGNOSTICS_SELF_TEST_SETUP_FAILED
    try:
        root = Path(tempfile.mkdtemp(prefix=_SELF_TEST_PREFIX, dir=parent))
        data_dir = root / "data"
        logs_dir = data_dir / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "automadin.log").write_text(
            _PRIVATE_LOG_MESSAGE,
            encoding="utf-8",
        )

        # The offscreen platform opens the real widget hierarchy without flashing
        # a release-build window or changing the user's system clipboard.
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6 import __version__ as imported_pyside_version
        from PySide6.QtWidgets import QApplication

        from automadin.services.database import DatabaseService
        from automadin.services.support_report_process import collect_support_report
        from automadin.ui.diagnostics_dialog import DiagnosticsDialog
        from automadin.ui.theme import (
            DEFAULT_THEME_NAME,
            get_stylesheet,
            set_active_theme,
        )

        application = QApplication.instance() or QApplication(
            ["Automadin diagnostics release self-test"]
        )
        set_active_theme(DEFAULT_THEME_NAME)
        application.setStyleSheet(get_stylesheet())

        database_path = data_dir / "automadin.db"
        database = DatabaseService(
            str(database_path),
            enable_wal=False,
        )
        database.create_profile(
            _PRIVATE_PROFILE_NAME,
            account_email=_PRIVATE_ACCOUNT_EMAIL,
        )
        database.close()
        database = None
        try:
            report = collect_support_report(
                database_path,
                data_dir,
                timeout_seconds=10.0,
                safe_temp_parent=root,
            )
        except Exception:
            outcome = DIAGNOSTICS_SELF_TEST_REPORT_FAILED
            report = None
        if report is not None:
            report_text = report.text
            if (
                report.snapshot.pyside_version != imported_pyside_version
                or report.snapshot.pyside_version == "Could not check"
                or _PRIVATE_PROFILE_NAME in report_text
                or _PRIVATE_ACCOUNT_EMAIL in report_text
                or _PRIVATE_LOG_MESSAGE in report_text
                or str(root) in report_text
            ):
                outcome = DIAGNOSTICS_SELF_TEST_REPORT_FAILED
            else:
                clipboard = _MemoryClipboard()
                dialog = DiagnosticsDialog(report, clipboard=clipboard)
                dialog.show()
                application.processEvents()
                dialog.copy_button.click()
                application.processEvents()
                if (
                    not dialog.isVisible()
                    or dialog.preview.toPlainText() != report_text
                    or clipboard.text() != report_text
                    or "copied" not in dialog.status_label.text().casefold()
                ):
                    outcome = DIAGNOSTICS_SELF_TEST_UI_FAILED
                else:
                    outcome = DIAGNOSTICS_SELF_TEST_OK
    except Exception:
        outcome = DIAGNOSTICS_SELF_TEST_SETUP_FAILED
    finally:
        if dialog is not None:
            try:
                dialog.close()
                dialog.deleteLater()
            except Exception:
                if outcome == DIAGNOSTICS_SELF_TEST_OK:
                    outcome = DIAGNOSTICS_SELF_TEST_UI_FAILED
        if database is not None:
            try:
                database.close()
            except Exception:
                if outcome == DIAGNOSTICS_SELF_TEST_OK:
                    outcome = DIAGNOSTICS_SELF_TEST_SETUP_FAILED
        if root is not None:
            try:
                shutil.rmtree(root)
            except OSError:
                outcome = DIAGNOSTICS_SELF_TEST_CLEANUP_FAILED
            else:
                if root.exists():
                    outcome = DIAGNOSTICS_SELF_TEST_CLEANUP_FAILED

    if outcome == DIAGNOSTICS_SELF_TEST_OK and marker is not None:
        try:
            with marker.open("x", encoding="utf-8", newline="\n") as marker_file:
                marker_file.write(diagnostics_self_test_marker_text(nonce))
        except OSError:
            outcome = DIAGNOSTICS_SELF_TEST_SETUP_FAILED

    return outcome
