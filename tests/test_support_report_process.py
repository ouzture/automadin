from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from automadin.services import support_report_process
from automadin.services.database import DatabaseService
from automadin.services.support_report_process import (
    SupportReportCleanupError,
    SupportReportCollectionCancelled,
    SupportReportCollectionTimedOut,
    SupportReportProtocolError,
    collect_support_report,
)
from automadin.support_report_worker import REQUEST_VERSION, _decode_request

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def _source_worker(request_path: Path) -> list[str]:
    return [
        sys.executable,
        str(MAIN),
        "--support-report-worker",
        str(request_path),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        (
            b'{"request_version":true,"database_path":"C:\\\\database.db",'
            b'"data_directory":"C:\\\\data"}'
        ),
        (
            b'{"request_version":1,"request_version":1,'
            b'"database_path":"C:\\\\database.db","data_directory":"C:\\\\data"}'
        ),
    ],
)
def test_worker_rejects_ambiguous_request_versions(payload: bytes) -> None:
    with pytest.raises(ValueError, match="request was invalid"):
        _decode_request(payload)


def test_source_subprocess_collects_report_with_exact_absolute_request(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    scratch = tmp_path / "scratch"
    data_dir.mkdir()
    scratch.mkdir()
    database = DatabaseService(str(data_dir / "automadin.db"), enable_wal=False)
    requests: list[dict[str, object]] = []

    def command_builder(request_path: Path) -> list[str]:
        request = json.loads(request_path.read_bytes())
        assert set(request) == {
            "request_version",
            "database_path",
            "data_directory",
        }
        assert request["request_version"] == REQUEST_VERSION
        assert Path(str(request["database_path"])).is_absolute()
        assert Path(str(request["data_directory"])).is_absolute()
        requests.append(request)
        return _source_worker(request_path)

    try:
        database.save_image("private-name", b"private pixels", 1, 1)

        report = collect_support_report(
            database.db_path,
            data_dir,
            command_builder=command_builder,
            safe_temp_parent=scratch,
        )
    finally:
        database.close()

    assert len(requests) == 1
    assert "Images: 1" in report.text
    assert "private-name" not in report.text
    assert list(scratch.iterdir()) == []


def test_default_source_command_uses_main_worker_route(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    scratch = tmp_path / "scratch"
    data_dir.mkdir()
    scratch.mkdir()
    database = DatabaseService(str(data_dir / "automadin.db"), enable_wal=False)
    try:
        report = collect_support_report(
            database.db_path,
            data_dir,
            safe_temp_parent=scratch,
        )
    finally:
        database.close()

    assert "AUTOMADIN SUPPORT REPORT" in report.text
    assert list(scratch.iterdir()) == []


def test_timeout_kills_worker_and_removes_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(SupportReportCollectionTimedOut):
        collect_support_report(
            tmp_path / "automadin.db",
            tmp_path,
            timeout_seconds=0.1,
            command_builder=lambda request_path: [
                sys.executable,
                "-c",
                "import time; time.sleep(10)",
                str(request_path),
            ],
            safe_temp_parent=scratch,
        )

    assert list(scratch.iterdir()) == []


def test_timeout_covers_prelaunch_command_preparation(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def slow_command_builder(_request_path: Path) -> list[str]:
        time.sleep(0.08)
        return [sys.executable, "-c", "raise SystemExit(0)"]

    with pytest.raises(SupportReportCollectionTimedOut):
        collect_support_report(
            tmp_path / "automadin.db",
            tmp_path,
            timeout_seconds=0.02,
            command_builder=slow_command_builder,
            safe_temp_parent=scratch,
        )

    assert list(scratch.iterdir()) == []


def test_target_path_preparation_is_lexical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_resolve(_path: Path, *args, **kwargs):
        raise AssertionError("target path resolution touched the filesystem")

    monkeypatch.setattr(Path, "resolve", reject_resolve)

    prepared = support_report_process._lexical_absolute_path(
        tmp_path / "unavailable-target" / "automadin.db"
    )

    assert prepared.is_absolute()
    assert prepared.name == "automadin.db"


def test_cancellation_kills_worker_and_removes_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cancellation_checks = 0

    def cancel_requested() -> bool:
        nonlocal cancellation_checks
        cancellation_checks += 1
        return cancellation_checks >= 2

    with pytest.raises(SupportReportCollectionCancelled):
        collect_support_report(
            tmp_path / "automadin.db",
            tmp_path,
            cancel_requested=cancel_requested,
            command_builder=lambda request_path: [
                sys.executable,
                "-c",
                "import time; time.sleep(10)",
                str(request_path),
            ],
            safe_temp_parent=scratch,
        )

    assert list(scratch.iterdir()) == []


def test_protocol_tampering_is_rejected_and_scratch_is_removed(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    tamper = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text('{}', encoding='utf-8'); "
        "Path('result.json').write_text('{}', encoding='utf-8')"
    )

    with pytest.raises(SupportReportProtocolError):
        collect_support_report(
            tmp_path / "automadin.db",
            tmp_path,
            command_builder=lambda request_path: [
                sys.executable,
                "-c",
                tamper,
                str(request_path),
            ],
            safe_temp_parent=scratch,
        )

    assert list(scratch.iterdir()) == []


def test_cleanup_failure_overrides_a_valid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_remove_workspace = support_report_process._remove_workspace

    def report_cleanup_failure(workspace: Path) -> bool:
        assert real_remove_workspace(workspace)
        return False

    monkeypatch.setattr(
        support_report_process,
        "_remove_workspace",
        report_cleanup_failure,
    )

    with pytest.raises(SupportReportCleanupError):
        collect_support_report(
            tmp_path / "automadin.db",
            tmp_path,
            command_builder=_source_worker,
            safe_temp_parent=scratch,
        )

    assert list(scratch.iterdir()) == []
