from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from automadin.services import support_report
from automadin.services.database import CURRENT_SCHEMA_VERSION, DatabaseService


class _Database:
    @staticmethod
    def get_workspace_statistics() -> dict[str, int]:
        return {
            "schema_version": 2,
            "flows": 2,
            "images": 1,
            "run_setups": 3,
            "browser_profiles": 4,
        }


def _make_report(
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    database: object | None = None,
) -> support_report.SupportReport:
    monkeypatch.setattr(
        support_report,
        "packaged_app_version",
        lambda _development_label: "9.8.7",
    )
    monkeypatch.setattr(support_report, "_application_mode", lambda: "Installed")
    monkeypatch.setattr(support_report.platform, "system", lambda: "Windows")
    monkeypatch.setattr(support_report.platform, "release", lambda: "11")
    monkeypatch.setattr(support_report.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(support_report.platform, "python_version", lambda: "3.11.4")
    monkeypatch.setattr(
        support_report,
        "_installed_distribution_version",
        lambda _name: "6.10.2",
    )
    return support_report.build_support_report(
        database or _Database(),
        data_dir,
        generated_utc=datetime(2026, 7, 19, 12, 34, 56, tzinfo=timezone.utc),
    )


def test_support_report_is_allowlisted_bounded_and_deterministically_ordered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    main_log = logs / "automadin.log"
    main_log.write_text(
        "email=user@example.com token=secret-token url=https://private.example\n"
        f"path={tmp_path}\n",
        encoding="utf-8",
    )
    runtime_log = logs / "runtime-manager.log.2"
    runtime_log.write_text("adb=C:\\Users\\Private\\platform-tools\\adb.exe\n")
    unknown = logs / "account-user@example.com.txt"
    unknown.write_text("must never be inventoried", encoding="utf-8")
    fixed_timestamp = 1_750_000_000
    os.utime(main_log, (fixed_timestamp, fixed_timestamp))
    os.utime(runtime_log, (fixed_timestamp, fixed_timestamp))

    report = _make_report(monkeypatch, tmp_path)
    text = report.text

    assert text.startswith("AUTOMADIN SUPPORT REPORT\n")
    assert text.index("APPLICATION") < text.index("PLATFORM")
    assert text.index("PLATFORM") < text.index("WORKSPACE")
    assert text.index("WORKSPACE") < text.index("DIAGNOSTIC FILES")
    assert "Version: 9.8.7" in text
    assert "Database schema: 2" in text
    assert "Flows: 2" in text
    assert "Images: 1" in text
    assert "Run setups: 3" in text
    assert "Browser profiles: 4" in text
    assert f"automadin.log | {main_log.stat().st_size:,} bytes" in text
    assert f"runtime-manager.log.2 | {runtime_log.stat().st_size:,} bytes" in text
    assert report.logs_directory == logs.resolve()
    assert len(text) < support_report.MAX_REPORT_CHARACTERS

    for secret in (
        "user@example.com",
        "secret-token",
        "https://private.example",
        str(tmp_path),
        "C:\\Users\\Private",
        "account-user@example.com.txt",
        "must never be inventoried",
    ):
        assert secret not in text


def test_support_report_maps_database_failures_to_literal_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenDatabase:
        @staticmethod
        def get_workspace_statistics() -> dict[str, int]:
            raise RuntimeError(
                "user@example.com https://private.example account-name-secret"
            )

    report = _make_report(monkeypatch, tmp_path, _BrokenDatabase())

    assert report.text.count("Could not check") >= 5
    for secret in (
        "user@example.com",
        "private.example",
        "account-name-secret",
    ):
        assert secret not in report.text


def test_support_report_uses_only_aggregate_database_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AggregateOnlyDatabase(_Database):
        @staticmethod
        def list_flows() -> list[str]:
            raise AssertionError("row-returning flow API must not be called")

        @staticmethod
        def list_images() -> list[dict[str, object]]:
            raise AssertionError("row-returning image API must not be called")

        @staticmethod
        def list_run_configs() -> list[str]:
            raise AssertionError("row-returning run API must not be called")

        @staticmethod
        def list_profile_records() -> list[object]:
            raise AssertionError("sensitive profile API must not be called")

    report = _make_report(monkeypatch, tmp_path, _AggregateOnlyDatabase())

    assert "Flows: 2" in report.text
    assert "Browser profiles: 4" in report.text


def test_metadata_values_fail_closed_instead_of_partially_redacting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "AlicePrivate"
    monkeypatch.setattr(
        support_report,
        "packaged_app_version",
        lambda _development_label: f"https://private.example/{secret}",
    )
    monkeypatch.setattr(
        support_report,
        "_application_mode",
        lambda: f"C:\\Users\\{secret}",
    )
    monkeypatch.setattr(
        support_report.platform,
        "system",
        lambda: f"user@example.com-{secret}",
    )
    monkeypatch.setattr(
        support_report.platform,
        "release",
        lambda: f"6.8.0-{secret}BuildHost",
    )
    monkeypatch.setattr(
        support_report.platform,
        "machine",
        lambda: f"AMD64-{secret}",
    )
    monkeypatch.setattr(
        support_report.platform,
        "python_version",
        lambda: f"3.11.4+{secret}",
    )
    monkeypatch.setattr(
        support_report,
        "_installed_distribution_version",
        lambda _name: f"6.10.2/{secret}",
    )

    report = support_report.build_support_report(
        _Database(),
        tmp_path,
        generated_utc=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )

    assert secret not in report.text
    assert "private.example" not in report.text
    assert "user@example.com" not in report.text
    assert "BuildHost" not in report.text
    assert report.text.count("Could not check") >= 7


def test_formatter_revalidates_counts_status_and_fixed_log_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _make_report(monkeypatch, tmp_path)
    secret = "user@example.com/Private"
    adversarial_snapshot = replace(
        report.snapshot,
        workspace=support_report.WorkspaceSummary(
            schema_version=secret,  # type: ignore[arg-type]
            flows=-1,
            images=1_000_000_001,
            run_setups=True,
            browser_profiles=4,
        ),
        diagnostics=support_report.DiagnosticFileInventory(
            status=secret,
            directory=None,
            files=(
                support_report.DiagnosticFileSummary(
                    name=secret,
                    size_bytes=50,
                    modified_utc=datetime(2026, 7, 19, tzinfo=timezone.utc),
                ),
            ),
        ),
    )

    text = support_report.format_support_report(adversarial_snapshot)

    assert secret not in text
    assert "Database schema: Could not check" in text
    assert "Flows: Could not check" in text
    assert "Images: Could not check" in text
    assert "Run setups: Could not check" in text
    assert "Browser profiles: 4" in text
    assert "Directory: Could not check" in text
    assert "- No known diagnostic file metadata is available." in text


def test_real_database_report_is_aggregate_only_and_non_mutating(
    tmp_path: Path,
) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"), enable_wal=False)
    try:
        database.save_image("private/image", b"private pixels", 1, 1)
        database.create_profile(
            "Private Profile",
            account_name="Alice",
            account_email="alice@example.com",
        )
        before_changes = database._conn.total_changes
        before_rows = {
            table: database._conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("flows", "images", "run_configs", "profiles")
        }
        before_files = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in tmp_path.iterdir()
            if path.is_file()
        }

        report = support_report.build_support_report_from_database_path(
            database.db_path,
            tmp_path,
            generated_utc=datetime(2026, 7, 19, tzinfo=timezone.utc),
        )

        after_rows = {
            table: database._conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("flows", "images", "run_configs", "profiles")
        }
        after_files = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in tmp_path.iterdir()
            if path.is_file()
        }
        assert database._conn.total_changes == before_changes
        assert after_rows == before_rows
        assert after_files == before_files
        assert "Images: 1" in report.text
        assert "Browser profiles: 1" in report.text
        assert "Private Profile" not in report.text
        assert "alice@example.com" not in report.text
    finally:
        database.close()


def test_database_path_report_reads_active_wal_database(tmp_path: Path) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"))
    try:
        database.save_image("target", b"pixels", 1, 1)

        report = support_report.build_support_report_from_database_path(
            database.db_path,
            tmp_path,
        )

        assert "Images: 1" in report.text
        assert f"Database schema: {CURRENT_SCHEMA_VERSION}" in report.text
    finally:
        database.close()


def test_database_path_report_bounds_lock_wait_and_fails_counts_closed(
    tmp_path: Path,
) -> None:
    database = DatabaseService(str(tmp_path / "automadin.db"), enable_wal=False)
    try:
        database._conn.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()

        report = support_report.build_support_report_from_database_path(
            database.db_path,
            tmp_path,
            database_timeout_seconds=0.05,
        )

        assert time.monotonic() - started < 1.0
        assert "Database schema: Could not check" in report.text
        assert "Flows: Could not check" in report.text
    finally:
        database._conn.rollback()
        database.close()


def test_diagnostic_inventory_ignores_unknown_and_reparse_entries(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unrelated.txt").write_text("unknown")
    external = tmp_path / "external.log"
    external.write_text("sensitive")
    linked_log = logs / "automadin.log"
    try:
        linked_log.symlink_to(external)
    except OSError:
        pytest.skip("File symlinks are unavailable in this Windows test environment")

    inventory = support_report.inspect_diagnostic_files(tmp_path)

    assert inventory.directory == logs.resolve()
    assert inventory.status == support_report.LOG_DIRECTORY_UNKNOWN
    assert inventory.files == ()


def test_diagnostic_inventory_rejects_linked_log_directory(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    try:
        (data_dir / "logs").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable in this Windows test environment")

    inventory = support_report.inspect_diagnostic_files(data_dir)

    assert inventory.status == support_report.LOG_DIRECTORY_UNSAFE
    assert inventory.directory is None
    assert inventory.files == ()


def test_validated_logs_directory_fails_closed_after_path_changes(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()

    assert support_report.validated_logs_directory(logs) == logs.resolve()
    assert support_report.validated_logs_directory(tmp_path) is None

    logs.rmdir()
    logs.write_text("not a directory", encoding="utf-8")

    assert support_report.validated_logs_directory(logs) is None


def test_support_report_record_round_trip_is_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "runtime-manager.log").write_text("runtime", encoding="utf-8")
    (logs / "automadin.log").write_text("main", encoding="utf-8")
    report = _make_report(monkeypatch, tmp_path)

    record = support_report.support_report_to_record(report)
    payload = json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    decoded = support_report.decode_support_report_record_json(payload)

    assert decoded.text == report.text
    assert decoded.logs_directory == report.logs_directory
    assert [item["name"] for item in record["diagnostics"]["files"]] == [
        "automadin.log",
        "runtime-manager.log",
    ]
    assert support_report.support_report_to_record(decoded) == record


def test_record_encoder_sanitizes_private_platform_fields_and_rejects_raw_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _make_report(monkeypatch, tmp_path)
    secret = "user@example.com/private-host"
    private_snapshot = replace(
        report.snapshot,
        application_version=secret,
        operating_system=secret,
    )
    private_report = replace(report, snapshot=private_snapshot)

    record = support_report.support_report_to_record(private_report)

    assert secret not in json.dumps(record)
    assert record["application"]["version"] == "Could not check"
    assert record["platform"]["operating_system"] == "Could not check"

    raw_file_snapshot = replace(
        private_snapshot,
        diagnostics=support_report.DiagnosticFileInventory(
            status=support_report.LOG_DIRECTORY_UNKNOWN,
            directory=None,
            files=(
                support_report.DiagnosticFileSummary(
                    name=secret,
                    size_bytes=1,
                    modified_utc=None,
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="diagnostic-file metadata"):
        support_report.support_report_to_record(
            replace(private_report, snapshot=raw_file_snapshot)
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda record: record.update(record_version=True),
            "record version",
        ),
        (
            lambda record: record["workspace"].update(flows="2"),
            "workspace count",
        ),
        (
            lambda record: record["workspace"].update(flows=2.9),
            "workspace count",
        ),
        (
            lambda record: record["application"].update(version=" 9.8.7 "),
            "not canonical",
        ),
        (
            lambda record: record["diagnostics"].update(
                status=support_report.LOG_DIRECTORY_AVAILABLE
            ),
            "inconsistent",
        ),
    ],
)
def test_record_decoder_rejects_noncanonical_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
    message: str,
) -> None:
    record = support_report.support_report_to_record(
        _make_report(monkeypatch, tmp_path)
    )
    malformed = copy.deepcopy(record)
    mutate(malformed)

    with pytest.raises(ValueError, match=message):
        support_report.support_report_from_record(malformed)


def test_record_decoder_rejects_noncanonical_file_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "automadin.log").write_text("main", encoding="utf-8")
    (logs / "runtime-manager.log").write_text("runtime", encoding="utf-8")
    record = support_report.support_report_to_record(
        _make_report(monkeypatch, tmp_path)
    )
    record["diagnostics"]["files"].reverse()

    with pytest.raises(ValueError, match="order was not canonical"):
        support_report.support_report_from_record(record)


def test_record_json_decoder_bounds_and_rejects_duplicate_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = support_report.support_report_to_record(
        _make_report(monkeypatch, tmp_path)
    )
    payload = json.dumps(record, separators=(",", ":")).encode("utf-8")
    duplicate_version = payload.replace(
        b'{"record_version":1,',
        b'{"record_version":1,"record_version":1,',
        1,
    )

    with pytest.raises(ValueError, match="JSON was invalid"):
        support_report.decode_support_report_record_json(duplicate_version)
    with pytest.raises(ValueError, match="payload was invalid"):
        support_report.decode_support_report_record_json(
            b" " * (support_report.MAX_REPORT_RECORD_BYTES + 1)
        )


def test_record_encoder_enforces_canonical_path_relationship(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _make_report(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="logs-directory state"):
        support_report.support_report_to_record(
            replace(report, logs_directory=tmp_path / "logs")
        )


def test_record_encoder_bounds_files_before_materializing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _make_report(monkeypatch, tmp_path)
    excessive_files = tuple(
        support_report.DiagnosticFileSummary(
            name="automadin.log",
            size_bytes=1,
            modified_utc=None,
        )
        for _ in range(len(support_report._known_log_names()) + 1)
    )
    snapshot = replace(
        report.snapshot,
        diagnostics=replace(
            report.snapshot.diagnostics,
            files=excessive_files,
        ),
    )

    with pytest.raises(ValueError, match="diagnostic-file list"):
        support_report.support_report_to_record(
            replace(report, snapshot=snapshot)
        )
