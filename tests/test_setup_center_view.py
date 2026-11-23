from __future__ import annotations

import os
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea, QWidget

from automadin.models.device import AndroidDevice
from automadin.runtime import RuntimeComponentStatus
from automadin.services.browser_health import BrowserHealthResult, BrowserHealthState
from automadin.services.setup_center import (
    CURRENT_SETUP_VERSION,
    SETUP_ACKNOWLEDGED_VERSION_SETTING,
    SETUP_SELECTED_TRACK_SETTING,
    SetupTrack,
)
from automadin.services.setup_readiness import SetupReadinessSnapshot
from automadin.ui import setup_center_view
from automadin.ui.setup_center_view import (
    SetupCenterView,
    SetupRowStatus,
    build_setup_rows,
)


@pytest.fixture
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


class _SettingsDatabase:
    schema_version = 2

    def __init__(self, settings: dict[str, str] | None = None) -> None:
        self.settings = dict(settings or {})

    def get_app_setting(self, key: str, default: str | None = None) -> str | None:
        return self.settings.get(key, default)

    def set_app_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def delete_app_setting(self, key: str) -> None:
        self.settings.pop(key, None)

    def list_flows(self) -> list[str]:
        return ["Welcome flow"]

    def list_run_configs(self) -> list[str]:
        return []

    def list_profile_records(self) -> list[object]:
        return []

    def profile_engine_specs(self) -> list[object]:
        return []


def _runtime(
    *,
    source: str = "unavailable",
    usable: bool = False,
    name: str = "component",
    error: str | None = None,
) -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        source=source,
        path=Path(f"{name}.exe") if usable else None,
        usable=usable,
        version=f"{name} 1.0" if usable else None,
        error=error,
    )


def _snapshot(data_dir: Path, **changes: object) -> SetupReadinessSnapshot:
    snapshot = SetupReadinessSnapshot(
        checked_at=datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
        app_version="1.2.3",
        data_dir=data_dir,
        data_writable=True,
        data_error=None,
        schema_version=2,
        schema_error=None,
        flow_count=0,
        run_config_count=0,
        workspace_error=None,
        profile_total=0,
        profile_ready=0,
        profile_error=None,
        playwright=_runtime(name="playwright"),
        chrome=_runtime(name="chrome"),
        edge=_runtime(name="edge"),
        adb=_runtime(name="adb"),
    )
    return replace(snapshot, **changes)


def _health(
    target: str,
    state: BrowserHealthState,
    stage: str = "complete",
) -> BrowserHealthResult:
    return BrowserHealthResult(
        target,
        state,
        stage,
        datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
        0.2,
        "Isolated headless launch result.",
    )


def _rows_by_id(
    snapshot: SetupReadinessSnapshot | None,
    selected: SetupTrack | None,
    connected: SetupTrack | None = None,
    connected_label: str | None = None,
):
    return {
        row.item_id: row
        for row in build_setup_rows(
            snapshot,
            selected,
            connected,
            connected_label,
        )
    }


@pytest.mark.parametrize(
    ("track", "required_tool", "target_action", "profile_status"),
    (
        (
            SetupTrack.BROWSER,
            "browser_tools",
            "target_browser",
            SetupRowStatus.NEEDS_ACTION,
        ),
        (
            SetupTrack.ANDROID,
            "android_tools",
            "target_android",
            SetupRowStatus.OPTIONAL,
        ),
        (
            SetupTrack.WINDOWS,
            "windows_tools",
            "target_windows",
            SetupRowStatus.OPTIONAL,
        ),
    ),
)
def test_rows_scope_required_checks_to_the_selected_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    track: SetupTrack,
    required_tool: str,
    target_action: str,
    profile_status: SetupRowStatus,
) -> None:
    monkeypatch.setattr(setup_center_view.sys, "platform", "win32")

    rows = _rows_by_id(_snapshot(tmp_path), track)

    assert tuple(rows) == (
        "foundation",
        "browser_tools",
        "android_tools",
        "windows_tools",
        "screen_text",
        "browser_profile",
        "target",
        "flow",
    )
    assert rows[required_tool].required
    assert rows["browser_tools"].required is (track is SetupTrack.BROWSER)
    assert rows["android_tools"].required is (track is SetupTrack.ANDROID)
    assert rows["windows_tools"].required is (track is SetupTrack.WINDOWS)
    assert rows["browser_profile"].status is profile_status
    assert rows["target"].status is SetupRowStatus.NEEDS_ACTION
    assert rows["target"].action_id == target_action
    assert rows["flow"].status is SetupRowStatus.NEEDS_ACTION


def test_ocr_is_optional_but_has_a_dedicated_setup_action(tmp_path: Path) -> None:
    row = _rows_by_id(_snapshot(tmp_path), SetupTrack.WINDOWS)["screen_text"]

    assert not row.required
    assert row.status is SetupRowStatus.OPTIONAL
    assert row.action_id == "ocr"


def test_rows_distinguish_presence_readiness_connection_and_sign_in_scope(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        chrome=_runtime(source="system", usable=True, name="chrome"),
        adb=_runtime(source="configured", usable=True, name="adb"),
        profile_total=3,
        profile_ready=2,
        flow_count=4,
        run_config_count=1,
    )

    rows = _rows_by_id(
        snapshot,
        SetupTrack.BROWSER,
        SetupTrack.ANDROID,
        "Pixel test device",
    )

    browser = rows["browser_tools"]
    assert browser.status is SetupRowStatus.NEEDS_ACTION
    assert "Detected — not launch tested: Google Chrome" in browser.detail
    assert "does not prove the browser can launch" in browser.detail
    assert "automation is ready" not in browser.detail

    android = rows["android_tools"]
    assert android.status is SetupRowStatus.READY
    assert "ADB responds, not that a device is connected" in android.detail

    profile = rows["browser_profile"]
    assert profile.status is SetupRowStatus.READY
    assert "does not guarantee the account is still signed in" in profile.detail
    assert "1 additional profile needs attention" in profile.detail

    target = rows["target"]
    assert target.status is SetupRowStatus.NEEDS_ACTION
    assert "current connection is Pixel test device" in target.detail
    assert "Disconnect it in the target bar" in target.detail
    assert target.action_id is None

    flow = rows["flow"]
    assert flow.status is SetupRowStatus.READY
    assert "does not infer target compatibility" in flow.detail
    assert "validates flows before save and execution" in flow.detail


def test_playwright_payload_detection_does_not_claim_launch_health(
    tmp_path: Path,
) -> None:
    rows = _rows_by_id(
        _snapshot(
            tmp_path,
            playwright=_runtime(
                source="managed",
                usable=True,
                name="playwright",
            ),
        ),
        SetupTrack.BROWSER,
    )

    detail = rows["browser_tools"].detail
    assert "Detected — not launch tested: Chromium, Firefox, WebKit" in detail
    assert "does not prove the browser can launch" in detail
    assert "automation is ready" not in detail


def test_browser_launch_health_is_literal_and_mixed_state_is_ready_with_limits(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        playwright=_runtime(source="managed", usable=True, name="playwright"),
        chrome=_runtime(source="system", usable=True, name="chrome"),
        browser_health=(
            ("chromium", _health("chromium", BrowserHealthState.HEALTHY)),
            (
                "firefox",
                _health("firefox", BrowserHealthState.FAILED, "launch"),
            ),
            (
                "webkit",
                _health("webkit", BrowserHealthState.TIMED_OUT, "timeout"),
            ),
            ("chrome", _health("chrome", BrowserHealthState.HEALTHY)),
            (
                "msedge",
                _health("msedge", BrowserHealthState.UNAVAILABLE, "runtime"),
            ),
        ),
    )

    browser = _rows_by_id(snapshot, SetupTrack.BROWSER)["browser_tools"]

    assert browser.status is SetupRowStatus.READY_WITH_LIMITS
    assert "Launch tested: Chromium, Google Chrome" in browser.detail
    assert "Needs attention: Firefox" in browser.detail
    assert "Could not check: WebKit" in browser.detail
    assert "Unavailable: Microsoft Edge" in browser.detail
    assert "do not verify headed mode" in browser.detail
    assert "account sign-in" in browser.detail


def test_checking_rows_apply_the_selected_target_requirements() -> None:
    rows = _rows_by_id(None, SetupTrack.BROWSER)

    assert rows["browser_tools"].required
    assert rows["browser_profile"].required
    assert not rows["android_tools"].required
    assert not rows["windows_tools"].required


def test_rows_use_blocked_and_could_not_check_instead_of_false_zeroes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(setup_center_view.sys, "platform", "linux")
    snapshot = _snapshot(
        tmp_path,
        data_writable=False,
        data_error="data directory denied writes",
        schema_version=None,
        schema_error="schema unavailable",
        workspace_error="flow query failed",
        flow_count=None,
        profile_error="profile scan failed",
        playwright=_runtime(source="error", error="browser scan failed"),
        adb=_runtime(source="error", error="ADB probe crashed"),
    )

    rows = _rows_by_id(snapshot, SetupTrack.WINDOWS)

    assert rows["foundation"].status is SetupRowStatus.BLOCKED
    assert "data directory denied writes" in rows["foundation"].detail
    assert rows["browser_tools"].status is SetupRowStatus.COULD_NOT_CHECK
    assert rows["android_tools"].status is SetupRowStatus.COULD_NOT_CHECK
    assert rows["windows_tools"].status is SetupRowStatus.BLOCKED
    assert rows["browser_profile"].status is SetupRowStatus.COULD_NOT_CHECK
    assert rows["flow"].status is SetupRowStatus.COULD_NOT_CHECK
    assert "0 saved flow" not in rows["flow"].detail


def _button_with_text(view: SetupCenterView, text: str) -> QPushButton:
    return next(
        button
        for button in view.findChildren(QPushButton)
        if button.text() == text
    )


def _row_action(view: SetupCenterView, title: str) -> QPushButton:
    row = next(
        row
        for row in view._row_widgets
        if row.accessibleName().startswith(f"{title}.")
    )
    return row.findChild(QPushButton)


def test_view_persists_track_and_separates_continue_from_acknowledgement(
    application: QApplication,
    tmp_path: Path,
) -> None:
    database = _SettingsDatabase(
        {SETUP_SELECTED_TRACK_SETTING: SetupTrack.ANDROID.value}
    )
    view = SetupCenterView(database, tmp_path)
    emitted: list[str] = []
    view.action_requested.connect(emitted.append)
    try:
        assert view.selected_track is SetupTrack.ANDROID
        assert view.should_auto_open()
        assert view._track_buttons[SetupTrack.ANDROID].isChecked()

        view._track_buttons[SetupTrack.BROWSER].click()
        assert view.selected_track is SetupTrack.BROWSER
        assert database.settings[SETUP_SELECTED_TRACK_SETTING] == "browser"

        view._snapshot = _snapshot(
            tmp_path,
            playwright=_runtime(source="managed", usable=True, name="playwright"),
        )
        view._render()
        _row_action(view, "Browser tools").click()
        _row_action(view, "Target connection").click()
        _row_action(view, "Automation workspace").click()
        _button_with_text(view, "Restore portable backup").click()
        _button_with_text(view, "Import database only").click()
        _button_with_text(view, "Continue to Flows").click()

        assert emitted == [
            "runtime",
            "target_browser",
            "flows",
            "restore_portable",
            "import_database",
            "continue_flows",
        ]
        assert SETUP_ACKNOWLEDGED_VERSION_SETTING not in database.settings
        assert view.should_auto_open()

        view.acknowledge_current()
        assert database.settings[SETUP_ACKNOWLEDGED_VERSION_SETTING] == str(
            CURRENT_SETUP_VERSION
        )
        assert not view.should_auto_open()
    finally:
        view.close()
        application.processEvents()


def test_connected_device_updates_only_the_target_connection_row(
    application: QApplication,
    tmp_path: Path,
) -> None:
    view = SetupCenterView(_SettingsDatabase(), tmp_path)
    try:
        view._select_track(SetupTrack.ANDROID)
        view._snapshot = _snapshot(tmp_path)
        view.set_connected_device(AndroidDevice("emulator-5554", model="Pixel 9"))

        rows = _rows_by_id(
            view.snapshot,
            view.selected_track,
            view._connected_track,
            view._connected_label,
        )
        assert rows["target"].status is SetupRowStatus.READY
        assert "Pixel 9" in rows["target"].detail
        assert "Disconnect in the target bar" in rows["target"].detail
        assert rows["target"].action_id is None
        assert rows["android_tools"].status is SetupRowStatus.NEEDS_ACTION
        assert "connected" not in rows["android_tools"].detail.casefold()
    finally:
        view.close()
        application.processEvents()


class _ControllableReadinessThread(QObject):
    completed = Signal(int, object)
    failed = Signal(int, str)
    finished = Signal()
    instances: list[_ControllableReadinessThread] = []

    def __init__(
        self,
        generation: int,
        _database: object,
        _data_dir: Path,
        _configured_adb_path: str | Path | None,
        parent: QObject | None = None,
        *,
        browser_health_cache: object | None = None,
    ) -> None:
        super().__init__(parent)
        self.browser_health_cache = browser_health_cache
        self.generation = generation
        self.running = False
        self.interruption_requested = False
        self.deleted = False
        type(self).instances.append(self)

    def start(self) -> None:
        self.running = True

    def isRunning(self) -> bool:
        return self.running

    def requestInterruption(self) -> None:
        self.interruption_requested = True

    def wait(self, _milliseconds: int) -> bool:
        raise AssertionError("SetupCenterView must never wait on the GUI thread")

    def deleteLater(self) -> None:
        self.deleted = True

    def complete(self, snapshot: SetupReadinessSnapshot) -> None:
        self.running = False
        self.completed.emit(self.generation, snapshot)
        self.finished.emit()

    def fail(self, error: str) -> None:
        self.running = False
        self.failed.emit(self.generation, error)
        self.finished.emit()


@pytest.mark.parametrize("collector_fails", (False, True))
def test_readiness_worker_releases_its_thread_database_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    collector_fails: bool,
) -> None:
    class _ReleaseTrackingDatabase(_SettingsDatabase):
        def __init__(self) -> None:
            super().__init__()
            self.release_calls = 0

        def release_current_thread_connection(self) -> bool:
            self.release_calls += 1
            return True

    database = _ReleaseTrackingDatabase()

    def collect(*_args: object, **_kwargs: object) -> SetupReadinessSnapshot:
        if collector_fails:
            raise RuntimeError("assessment failed")
        return _snapshot(tmp_path)

    monkeypatch.setattr(setup_center_view, "collect_setup_readiness", collect)
    worker = setup_center_view._SetupReadinessThread(
        1,
        database,  # type: ignore[arg-type]
        tmp_path,
        None,
    )

    worker.run()

    assert database.release_calls == 1


def test_readiness_worker_logs_traceback_and_emits_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    database = _SettingsDatabase()
    monkeypatch.setattr(
        setup_center_view,
        "collect_setup_readiness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private readiness detail")
        ),
    )
    worker = setup_center_view._SetupReadinessThread(
        1,
        database,  # type: ignore[arg-type]
        tmp_path,
        None,
    )
    failures: list[tuple[int, str]] = []
    worker.failed.connect(lambda generation, message: failures.append((generation, message)))

    with caplog.at_level(logging.ERROR, logger=setup_center_view.__name__):
        worker.run()

    assert failures == [(1, setup_center_view._READINESS_FAILURE_MESSAGE)]
    assert "private readiness detail" not in failures[0][1]
    assert "private readiness detail" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_readiness_worker_logs_database_release_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    class _ReleaseFailureDatabase(_SettingsDatabase):
        def release_current_thread_connection(self) -> bool:
            raise RuntimeError("private release detail")

    monkeypatch.setattr(
        setup_center_view,
        "collect_setup_readiness",
        lambda *_args, **_kwargs: _snapshot(tmp_path),
    )
    worker = setup_center_view._SetupReadinessThread(
        1,
        _ReleaseFailureDatabase(),  # type: ignore[arg-type]
        tmp_path,
        None,
    )

    with caplog.at_level(logging.ERROR, logger=setup_center_view.__name__):
        worker.run()

    assert "private release detail" in caplog.text
    assert any(
        "database connection" in record.message and record.exc_info
        for record in caplog.records
    )


@pytest.fixture
def controllable_threads(monkeypatch: pytest.MonkeyPatch):
    _ControllableReadinessThread.instances = []
    monkeypatch.setattr(
        setup_center_view,
        "_SetupReadinessThread",
        _ControllableReadinessThread,
    )
    return _ControllableReadinessThread.instances


def test_refresh_coalesces_rejects_stale_results_and_runs_latest_generation(
    application: QApplication,
    controllable_threads: list[_ControllableReadinessThread],
    tmp_path: Path,
) -> None:
    view = SetupCenterView(_SettingsDatabase(), tmp_path)
    first_snapshot = _snapshot(tmp_path, app_version="first")
    latest_snapshot = _snapshot(tmp_path, app_version="latest")
    try:
        view.refresh()
        first = controllable_threads[0]
        assert first.generation == 1
        assert first.isRunning()
        assert view._refresh_button.text() == "Checking…"

        view.refresh()
        assert len(controllable_threads) == 1
        assert view._refresh_pending
        assert view._refresh_button.text() == "Refresh queued"

        first.complete(first_snapshot)
        assert view.snapshot is None
        assert first.deleted
        assert len(controllable_threads) == 2

        second = controllable_threads[1]
        assert second.generation == 2
        view._readiness_completed(1, first_snapshot)
        assert view.snapshot is None

        second.complete(latest_snapshot)
        assert view.snapshot is latest_snapshot
        assert view._thread is None
        assert view._refresh_button.isEnabled()
        assert view._refresh_button.text() == "Refresh status"
    finally:
        view.close()
        application.processEvents()


def test_explicit_refresh_invalidates_health_only_when_each_scan_starts(
    application: QApplication,
    controllable_threads: list[_ControllableReadinessThread],
    tmp_path: Path,
) -> None:
    class HealthCache:
        def __init__(self) -> None:
            self.invalidations = 0

        def invalidate(self) -> None:
            self.invalidations += 1

    cache = HealthCache()
    view = SetupCenterView(  # type: ignore[arg-type]
        _SettingsDatabase(),
        tmp_path,
        browser_health_cache=cache,
    )
    try:
        view.refresh(fresh_browser_health=True)
        assert cache.invalidations == 1
        first = controllable_threads[0]
        assert first.browser_health_cache is cache

        view.refresh(fresh_browser_health=True)
        assert cache.invalidations == 1

        first.complete(_snapshot(tmp_path))
        assert cache.invalidations == 2
        assert len(controllable_threads) == 2
    finally:
        view.close()
        application.processEvents()


def test_shutdown_retains_worker_ignores_late_result_and_recovers_after_finish(
    application: QApplication,
    controllable_threads: list[_ControllableReadinessThread],
    tmp_path: Path,
) -> None:
    view = SetupCenterView(_SettingsDatabase(), tmp_path)
    try:
        view.refresh()
        worker = controllable_threads[0]

        assert not view.shutdown(wait_ms=0)
        assert worker.interruption_requested
        assert view._shutdown_wait_pending
        assert not view.isEnabled()

        view.refresh()
        assert len(controllable_threads) == 1

        worker.complete(_snapshot(tmp_path, app_version="late"))
        assert view.snapshot is None
        assert view._thread is None
        assert view.isEnabled()
        assert not view._shutting_down
        assert "stopped" in view._summary_label.text().casefold()
    finally:
        view.close()
        application.processEvents()


def test_shutdown_without_a_running_check_leaves_setup_usable(
    application: QApplication,
    tmp_path: Path,
) -> None:
    view = SetupCenterView(_SettingsDatabase(), tmp_path)
    try:
        assert view.shutdown()
        assert view.isEnabled()
        assert not view._shutting_down
    finally:
        view.close()
        application.processEvents()


def test_refresh_start_failure_restores_controls_and_shows_safe_error(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    class _StartFailureThread(_ControllableReadinessThread):
        instance: "_StartFailureThread | None" = None

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            type(self).instance = self

        def start(self) -> None:
            raise RuntimeError("private readiness start detail")

    monkeypatch.setattr(
        setup_center_view,
        "_SetupReadinessThread",
        _StartFailureThread,
    )
    view = SetupCenterView(_SettingsDatabase(), tmp_path)
    try:
        with caplog.at_level(logging.ERROR, logger=setup_center_view.__name__):
            view.refresh()

        worker = _StartFailureThread.instance
        assert worker is not None
        assert worker.deleted
        assert view._thread is None
        assert view._refresh_button.isEnabled()
        assert view._refresh_button.text() == "Refresh status"
        assert view._refresh_error == setup_center_view._READINESS_FAILURE_MESSAGE
        assert "private readiness start detail" not in view._summary_label.text()
        assert any(record.exc_info for record in caplog.records)
    finally:
        view.close()
        application.processEvents()


def test_readiness_release_failure_does_not_strand_refresh_ui(
    application: QApplication,
    controllable_threads: list[_ControllableReadinessThread],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    view = SetupCenterView(_SettingsDatabase(), tmp_path)
    try:
        view.refresh()
        worker = controllable_threads[0]
        worker.deleteLater = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("release failed")
        )

        with caplog.at_level(logging.ERROR, logger=setup_center_view.__name__):
            worker.complete(_snapshot(tmp_path))

        assert view._thread is None
        assert view._refresh_button.isEnabled()
        assert view._refresh_button.text() == "Refresh status"
        assert any(
            "Could not release Setup Center readiness worker" in record.message
            and record.exc_info
            for record in caplog.records
        )
    finally:
        view.close()
        application.processEvents()


def test_refresh_failure_replaces_checking_rows_with_retryable_error_state(
    application: QApplication,
    controllable_threads: list[_ControllableReadinessThread],
    tmp_path: Path,
) -> None:
    view = SetupCenterView(_SettingsDatabase(), tmp_path)
    try:
        view._select_track(SetupTrack.BROWSER)
        view.refresh()
        controllable_threads[0].fail("readiness worker failed")

        assert view.snapshot is None
        assert view._refresh_error == "readiness worker failed"
        assert "Could not refresh setup status" in view._summary_label.text()
        assert "readiness worker failed" in view._summary_label.text()
        assert all(
            "Could not check" in row.accessibleName()
            for row in view._row_widgets
        )
        assert _row_action(view, "Data and version").text() == "Retry"

        view._select_track(SetupTrack.ANDROID)
        assert all(
            "Could not check" in row.accessibleName()
            for row in view._row_widgets
        )
    finally:
        view.close()
        application.processEvents()


def test_layout_is_readable_at_1920_without_horizontal_scrolling(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    view = SetupCenterView(_SettingsDatabase(), tmp_path)
    view._snapshot = _snapshot(
        tmp_path,
        playwright=_runtime(source="managed", usable=True, name="playwright"),
        adb=_runtime(source="configured", usable=True, name="adb"),
        profile_total=2,
        profile_ready=1,
        flow_count=3,
    )
    view._render()
    monkeypatch.setattr(view, "refresh", lambda: None)
    try:
        view.resize(1920, 1080)
        view.show()
        application.processEvents()

        scroll = view.findChild(QScrollArea)
        assert scroll is not None
        assert (
            scroll.horizontalScrollBarPolicy()
            is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        assert scroll.horizontalScrollBar().maximum() == 0

        content = next(
            child
            for child in view.findChildren(QWidget)
            if child.maximumWidth() == 1040
        )
        assert 900 <= content.width() <= 1040
        assert all(row.width() >= 900 for row in view._row_widgets)

        detail_labels = [
            next(
                label
                for label in row.findChildren(QLabel)
                if label.property("setupRowDetail")
            )
            for row in view._row_widgets
        ]
        assert len(detail_labels) == 8
        for label in detail_labels:
            label.ensurePolished()
        assert all(label.wordWrap() for label in detail_labels)
        assert all(label.width() >= 450 for label in detail_labels)
        assert all(label.font().pointSizeF() >= 10.5 for label in detail_labels)
        assert all(
            label.textInteractionFlags()
            & Qt.TextInteractionFlag.TextSelectableByKeyboard
            for label in detail_labels
        )
    finally:
        view.close()
        application.processEvents()
