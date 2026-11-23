from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from automadin import diagnostics_self_test
from automadin.services import support_report
from automadin.services import support_report_process


def test_diagnostics_release_self_test_exercises_ui_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    before = set(tmp_path.iterdir())

    exit_code = diagnostics_self_test.run_release_diagnostics_self_test(tmp_path)

    assert exit_code == diagnostics_self_test.DIAGNOSTICS_SELF_TEST_OK
    assert set(tmp_path.iterdir()) == before


def test_diagnostics_release_self_test_rejects_missing_parent(
    tmp_path: Path,
) -> None:
    exit_code = diagnostics_self_test.run_release_diagnostics_self_test(
        tmp_path / "missing"
    )

    assert exit_code == diagnostics_self_test.DIAGNOSTICS_SELF_TEST_INVALID_PARENT
    assert list(tmp_path.iterdir()) == []


def test_diagnostics_release_self_test_fails_when_metadata_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    collect_report = support_report_process.collect_support_report

    def collect_without_metadata(*args, **kwargs):
        report = collect_report(*args, **kwargs)
        snapshot = replace(report.snapshot, pyside_version="Could not check")
        return replace(
            report,
            snapshot=snapshot,
            text=support_report.format_support_report(snapshot),
        )

    monkeypatch.setattr(
        support_report_process,
        "collect_support_report",
        collect_without_metadata,
    )

    exit_code = diagnostics_self_test.run_release_diagnostics_self_test(tmp_path)

    assert exit_code == diagnostics_self_test.DIAGNOSTICS_SELF_TEST_REPORT_FAILED
    assert list(tmp_path.iterdir()) == []


def test_diagnostics_release_self_test_writes_nonce_bound_success_marker(
    tmp_path: Path,
) -> None:
    nonce = "0123456789abcdef0123456789abcdef"

    exit_code = diagnostics_self_test.run_release_diagnostics_self_test(
        tmp_path,
        nonce=nonce,
    )

    marker = diagnostics_self_test.diagnostics_self_test_marker(tmp_path, nonce)
    assert exit_code == diagnostics_self_test.DIAGNOSTICS_SELF_TEST_OK
    assert marker.read_text(encoding="utf-8") == (
        diagnostics_self_test.diagnostics_self_test_marker_text(nonce)
    )
    assert set(tmp_path.iterdir()) == {marker}


def test_diagnostics_release_self_test_rejects_invalid_nonce(
    tmp_path: Path,
) -> None:
    exit_code = diagnostics_self_test.run_release_diagnostics_self_test(
        tmp_path,
        nonce="../not-a-nonce",
    )

    assert exit_code == diagnostics_self_test.DIAGNOSTICS_SELF_TEST_INVALID_NONCE
    assert list(tmp_path.iterdir()) == []
