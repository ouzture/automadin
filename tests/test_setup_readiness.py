from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from automadin.runtime import RuntimeComponentStatus
from automadin.services.browser_health import BrowserHealthResult, BrowserHealthState
from automadin.services import setup_readiness
from automadin.services.setup_readiness import (
    SetupReadinessCancelled,
    collect_setup_readiness,
)


class _Database:
    schema_version = 2

    def list_flows(self) -> list[str]:
        return ["first", "second"]

    def list_run_configs(self) -> list[str]:
        return ["desktop"]

    def list_profile_records(self) -> list[object]:
        return [object(), object(), object()]

    def profile_engine_specs(self) -> list[object]:
        return [object(), object()]


def _status(name: str) -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        source=name,
        path=Path(f"{name}.exe"),
        usable=True,
        version=f"{name}-version",
    )


@pytest.fixture(autouse=True)
def _stable_runtime_assessments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        setup_readiness,
        "browser_runtime_status",
        lambda _data_dir: _status("playwright"),
    )
    monkeypatch.setattr(
        setup_readiness,
        "system_browser_channel_status",
        lambda channel: _status(channel),
    )
    monkeypatch.setattr(
        setup_readiness,
        "adb_runtime_status",
        lambda _data_dir: _status("automatic-adb"),
    )


def test_snapshot_reports_counts_fallback_version_and_cleans_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        setup_readiness,
        "packaged_app_version",
        lambda development_label: development_label,
    )

    snapshot = collect_setup_readiness(_Database(), tmp_path)

    assert snapshot.app_version == "Development build"
    assert snapshot.data_dir == tmp_path.resolve()
    assert snapshot.data_writable is True
    assert snapshot.data_error is None
    assert snapshot.schema_version == 2
    assert snapshot.schema_error is None
    assert snapshot.flow_count == 2
    assert snapshot.run_config_count == 1
    assert snapshot.workspace_error is None
    assert snapshot.profile_total == 3
    assert snapshot.profile_ready == 2
    assert snapshot.profile_error is None
    assert snapshot.checked_at.tzinfo is not None
    assert list(tmp_path.glob(".automadin-write-probe-*.tmp")) == []

    with pytest.raises(FrozenInstanceError):
        snapshot.flow_count = 10  # type: ignore[misc]


def test_setup_readiness_uses_shared_packaged_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        setup_readiness,
        "packaged_app_version",
        lambda development_label: "0.2.0",
    )

    snapshot = collect_setup_readiness(_Database(), tmp_path)

    assert snapshot.app_version == "0.2.0"


def test_browser_launch_health_uses_detected_statuses_and_remains_point_in_time(
    tmp_path: Path,
) -> None:
    calls: list[tuple[dict[str, RuntimeComponentStatus], Path]] = []

    class HealthCache:
        @staticmethod
        def check_many(statuses, data_dir, cancel):
            assert not cancel()
            calls.append((dict(statuses), Path(data_dir)))
            return {
                target: BrowserHealthResult(
                    target,
                    BrowserHealthState.HEALTHY,
                    "complete",
                    datetime.now(UTC),
                    0.01,
                    "The isolated headless launch check completed.",
                )
                for target in ("chromium", "firefox", "webkit", "chrome", "msedge")
            }

    snapshot = collect_setup_readiness(
        _Database(),
        tmp_path,
        browser_health_cache=HealthCache(),
    )

    assert len(calls) == 1
    statuses, data_dir = calls[0]
    assert statuses["playwright"].source == "playwright"
    assert statuses["chrome"].source == "chrome"
    assert statuses["msedge"].source == "msedge"
    assert data_dir == tmp_path.resolve()
    assert tuple(target for target, _result in snapshot.browser_health) == (
        "chromium",
        "firefox",
        "webkit",
        "chrome",
        "msedge",
    )
    assert all(
        result.state is BrowserHealthState.HEALTHY
        for _target, result in snapshot.browser_health
    )


def test_browser_health_service_failure_does_not_erase_other_setup_facts(
    tmp_path: Path,
) -> None:
    class BrokenHealthCache:
        @staticmethod
        def check_many(*_args, **_kwargs):
            raise RuntimeError("health coordinator failed")

    snapshot = collect_setup_readiness(
        _Database(),
        tmp_path,
        browser_health_cache=BrokenHealthCache(),
    )

    assert snapshot.browser_health == ()
    assert snapshot.browser_health_error == "health coordinator failed"
    assert snapshot.adb.usable
    assert snapshot.schema_version == 2


def test_configured_adb_is_probed_first_and_labeled_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = tmp_path / "selected-adb.exe"
    configured.touch()
    automatic_calls = 0
    probe_calls: list[Path] = []

    def automatic(_data_dir: Path) -> RuntimeComponentStatus:
        nonlocal automatic_calls
        automatic_calls += 1
        return _status("automatic-adb")

    def probe(path: Path) -> RuntimeComponentStatus:
        probe_calls.append(path)
        return RuntimeComponentStatus(
            source="unavailable",
            path=path,
            usable=True,
            version="Android Debug Bridge version 1.0.41",
        )

    monkeypatch.setattr(setup_readiness, "adb_runtime_status", automatic)
    monkeypatch.setattr(setup_readiness, "probe_adb", probe)

    snapshot = collect_setup_readiness(
        _Database(),
        tmp_path,
        configured_adb_path=f"  {configured}  ",
    )

    assert probe_calls == [configured]
    assert automatic_calls == 0
    assert snapshot.adb.source == "configured"
    assert snapshot.adb.path == configured
    assert snapshot.adb.usable
    assert snapshot.adb.version == "Android Debug Bridge version 1.0.41"


def test_collection_never_configures_runtime_or_discovers_devices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from automadin import runtime
    from automadin.engine.session import DeviceDiscoveryService

    def forbidden(*_args, **_kwargs):
        raise AssertionError("mutating runtime/device API was called")

    monkeypatch.setattr(runtime, "configure_runtime", forbidden)
    monkeypatch.setattr(DeviceDiscoveryService, "list_devices", forbidden)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "keep-browser-path")
    monkeypatch.setenv("ADB_PATH", "keep-adb-path")

    collect_setup_readiness(_Database(), tmp_path)

    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "keep-browser-path"
    assert os.environ["ADB_PATH"] == "keep-adb-path"


def test_assessment_failures_remain_independent_and_counts_stay_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class PartialDatabase:
        @property
        def schema_version(self) -> int:
            raise RuntimeError("schema unavailable")

        def list_flows(self) -> list[str]:
            raise RuntimeError("flow query failed")

        def list_run_configs(self) -> list[str]:
            return ["one", "two"]

        def list_profile_records(self) -> list[object]:
            return [object()] * 4

        def profile_engine_specs(self) -> list[object]:
            raise RuntimeError("profile inspection failed")

    def browser_failure(_data_dir: Path) -> RuntimeComponentStatus:
        raise RuntimeError("browser assessment failed")

    def browser_channels(channel: str) -> RuntimeComponentStatus:
        if channel == "msedge":
            raise RuntimeError("edge assessment failed")
        return _status(channel)

    monkeypatch.setattr(setup_readiness, "browser_runtime_status", browser_failure)
    monkeypatch.setattr(
        setup_readiness,
        "system_browser_channel_status",
        browser_channels,
    )

    snapshot = collect_setup_readiness(PartialDatabase(), tmp_path)

    assert snapshot.data_writable is True
    assert snapshot.schema_version is None
    assert snapshot.schema_error == "schema unavailable"
    assert snapshot.flow_count is None
    assert snapshot.run_config_count == 2
    assert snapshot.workspace_error is not None
    assert "flow query failed" in snapshot.workspace_error
    assert snapshot.profile_total == 4
    assert snapshot.profile_ready is None
    assert snapshot.profile_error is not None
    assert "profile inspection failed" in snapshot.profile_error
    assert snapshot.playwright.source == "error"
    assert snapshot.playwright.error == "browser assessment failed"
    assert snapshot.chrome.usable
    assert snapshot.edge.source == "error"
    assert snapshot.edge.error == "edge assessment failed"
    assert snapshot.adb.usable


def test_cancellation_is_checked_between_assessments_and_probe_is_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    def runtime_must_not_run(_data_dir: Path) -> RuntimeComponentStatus:
        raise AssertionError("runtime assessment ran after cancellation")

    monkeypatch.setattr(
        setup_readiness,
        "browser_runtime_status",
        runtime_must_not_run,
    )

    with pytest.raises(SetupReadinessCancelled):
        collect_setup_readiness(_Database(), tmp_path, cancel=cancel)

    assert calls == 3
    assert list(tmp_path.glob(".automadin-write-probe-*.tmp")) == []


def test_failed_fsync_is_reported_and_temporary_probe_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_fsync(_fd: int) -> None:
        raise OSError("disk sync failed")

    monkeypatch.setattr(setup_readiness.os, "fsync", fail_fsync)

    snapshot = collect_setup_readiness(_Database(), tmp_path)

    assert snapshot.data_writable is False
    assert snapshot.data_error is not None
    assert "disk sync failed" in snapshot.data_error
    assert list(tmp_path.glob(".automadin-write-probe-*.tmp")) == []
