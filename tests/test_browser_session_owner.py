from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import cv2
import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from automadin.engine.browser_engine import (
    BrowserDeviceEngine,
    BrowserProfileLaunchError,
    BrowserSessionTeardownError,
)
from automadin.engine.browser_session import (
    BrowserSessionHandle,
    BrowserSessionOperationUncertainError,
)
from automadin.engine.browser_pages import (
    BrowserTabSnapshot,
    BrowserTabsSnapshot,
)
from automadin.engine.session import DeviceSessionService
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
    DeviceType,
    ScreenMode,
    WindowsDevice,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileBorrowActiveError,
    BrowserProfileInUseError,
    BrowserProfileLeaseRegistry,
    BrowserProfileLeaseStateError,
    ProfileUse,
)
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import DeviceOperationLeaseRegistry
from automadin.services.screen_capture import ScreenCaptureService
from automadin.ui.capture_controller import CaptureOperationController
from automadin.ui.device_selector import DeviceSelectorWidget
from automadin.ui.utility.utility_view import UtilityView
from automadin.ui.visual_rule_dialog import VisualRuleDialog

_PNG_OK, _PNG_BYTES = cv2.imencode(
    ".png",
    np.zeros((2, 2, 3), dtype=np.uint8),
)
assert _PNG_OK
_VALID_PNG = _PNG_BYTES.tobytes()


@pytest.fixture(scope="module", autouse=True)
def _application():
    return QApplication.instance() or QApplication([])


def _browser_device(
    *,
    profile_id: str | None = None,
    screen_mode: ScreenMode = ScreenMode.DESKTOP,
    headless: bool = False,
) -> BrowserDevice:
    return BrowserDevice(
        config=BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            headless=headless,
            screen_mode=screen_mode,
            profile_id=profile_id,
            profile_name="Managed" if profile_id else None,
        )
    )


class _FakeBrowserSessionData:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.screenshot_count = 0
        self.fail_next = False
        self.closed = False
        self.block_entered = threading.Event()
        self.block_next_until: threading.Event | None = None
        self.tab_thread_ids: list[int] = []
        self.selected_tab_ids: list[str] = []
        self.fail_select = False
        self.select_entered = threading.Event()
        self.select_release: threading.Event | None = None

    def screenshot(self) -> bytes:
        thread_id = threading.get_ident()
        with self._lock:
            self.thread_ids.append(thread_id)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.screenshot_count += 1
            count = self.screenshot_count
            fail = self.fail_next
            self.fail_next = False
            block_until = self.block_next_until
            self.block_next_until = None
        try:
            if block_until is not None:
                self.block_entered.set()
                block_until.wait(2)
            time.sleep(0.001)
            if fail:
                raise RuntimeError("simulated screenshot failure")
            return f"shot-{count}".encode()
        finally:
            with self._lock:
                self._active -= 1

    def tabs_snapshot(self) -> BrowserTabsSnapshot:
        self.tab_thread_ids.append(threading.get_ident())
        return BrowserTabsSnapshot(
            tabs=(
                BrowserTabSnapshot(
                    tab_id="tab-1",
                    index=0,
                    url="https://example.test/",
                    title="Example",
                    is_active=True,
                    opener_tab_id=None,
                ),
            ),
            active_tab_id="tab-1",
        )

    def select_tab(self, tab_id: str) -> None:
        self.tab_thread_ids.append(threading.get_ident())
        self.select_entered.set()
        if self.select_release is not None:
            self.select_release.wait(2)
        if self.fail_select:
            self.fail_select = False
            raise RuntimeError("simulated tab selection failure")
        self.selected_tab_ids.append(tab_id)


class _FakeBrowserEngine:
    def __init__(
        self,
        session_data: _FakeBrowserSessionData | None = None,
        *,
        startup_error: Exception | None = None,
        disconnect_error: Exception | None = None,
    ) -> None:
        self.session_data = session_data or _FakeBrowserSessionData()
        self.startup_error = startup_error
        self.disconnect_error = disconnect_error
        self.connect_thread_id: int | None = None
        self.disconnect_thread_id: int | None = None
        self.received_leases: list[object | None] = []

    def connect(
        self,
        device: BrowserDevice,
        *,
        profile_lease: object | None = None,
    ) -> _FakeBrowserSessionData:
        self.connect_thread_id = threading.get_ident()
        self.received_leases.append(profile_lease)
        if device.config.profile_id:
            assert profile_lease is not None
            profile_lease.ensure_live(device.config.profile_id)  # type: ignore[attr-defined]
        if self.startup_error is not None:
            raise self.startup_error
        return self.session_data

    def disconnect(self, session_data: _FakeBrowserSessionData) -> None:
        assert session_data is self.session_data
        self.disconnect_thread_id = threading.get_ident()
        if self.disconnect_error is not None:
            raise self.disconnect_error
        session_data.closed = True


def test_browser_session_handle_keeps_connect_capture_and_close_on_owner_thread() -> None:
    caller_thread_id = threading.get_ident()
    engine = _FakeBrowserEngine()
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]

    screenshots = [handle.screenshot() for _ in range(50)]
    handle.close()

    assert screenshots == [f"shot-{index}".encode() for index in range(1, 51)]
    assert engine.connect_thread_id is not None
    assert engine.connect_thread_id != caller_thread_id
    assert set(engine.session_data.thread_ids) == {engine.connect_thread_id}
    assert engine.disconnect_thread_id == engine.connect_thread_id
    assert engine.session_data.closed
    assert handle.is_closed


def test_browser_session_handle_serializes_concurrent_callers() -> None:
    engine = _FakeBrowserEngine()
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=8) as pool:
        screenshots = list(pool.map(lambda _index: handle.screenshot(), range(40)))

    handle.close()

    assert len(set(screenshots)) == 40
    assert engine.session_data.max_active == 1
    assert set(engine.session_data.thread_ids) == {engine.connect_thread_id}


def test_browser_session_handle_lists_and_selects_tabs_on_owner_thread() -> None:
    caller_thread_id = threading.get_ident()
    engine = _FakeBrowserEngine()
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]

    snapshot = handle.tabs_snapshot()
    handle.select_tab("tab-1")
    handle.close()

    assert snapshot.active_tab_id == "tab-1"
    assert engine.session_data.selected_tab_ids == ["tab-1"]
    assert engine.connect_thread_id is not None
    assert engine.connect_thread_id != caller_thread_id
    assert set(engine.session_data.tab_thread_ids) == {engine.connect_thread_id}


def test_browser_session_handle_rejects_empty_tab_id_before_queueing() -> None:
    engine = _FakeBrowserEngine()
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]
    try:
        with pytest.raises(ValueError, match="non-empty"):
            handle.select_tab(" ")
        assert engine.session_data.selected_tab_ids == []
    finally:
        handle.close()


def test_timed_out_queued_tab_selection_is_canceled_before_execution() -> None:
    engine = _FakeBrowserEngine()
    release_first = threading.Event()
    engine.session_data.block_next_until = release_first
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]
    first = threading.Thread(target=lambda: handle.screenshot(timeout=1.0))
    first.start()
    assert engine.session_data.block_entered.wait(1.0)

    with pytest.raises(FutureTimeoutError):
        handle.select_tab("tab-1", timeout=0.01)

    release_first.set()
    first.join(timeout=1.0)
    assert not first.is_alive()
    handle.close()
    assert engine.session_data.selected_tab_ids == []


def test_owner_tab_selection_failure_propagates_and_session_remains_usable() -> None:
    engine = _FakeBrowserEngine()
    engine.session_data.fail_select = True
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]
    try:
        with pytest.raises(RuntimeError, match="simulated tab selection failure"):
            handle.select_tab("tab-1")
        assert handle.tabs_snapshot().active_tab_id == "tab-1"
    finally:
        handle.close()


def test_inflight_tab_selection_timeout_reports_uncertain_outcome() -> None:
    engine = _FakeBrowserEngine()
    engine.session_data.select_release = threading.Event()
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]
    try:
        with pytest.raises(
            BrowserSessionOperationUncertainError,
            match="Refresh the tab list",
        ):
            handle.select_tab("tab-1", timeout=0.01)
        assert engine.session_data.select_entered.is_set()
        engine.session_data.select_release.set()
        for _ in range(100):
            if engine.session_data.selected_tab_ids:
                break
            time.sleep(0.01)
        assert engine.session_data.selected_tab_ids == ["tab-1"]
    finally:
        engine.session_data.select_release.set()
        handle.close()


def test_browser_session_handle_propagates_one_failure_and_remains_usable() -> None:
    engine = _FakeBrowserEngine()
    engine.session_data.fail_next = True
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="simulated screenshot failure"):
        handle.screenshot()

    assert handle.screenshot() == b"shot-2"
    handle.close()
    handle.close()

    with pytest.raises(RuntimeError, match="closing or already closed"):
        handle.screenshot()


def test_timed_out_queued_screenshot_is_skipped_before_close() -> None:
    engine = _FakeBrowserEngine()
    release_first = threading.Event()
    engine.session_data.block_next_until = release_first
    handle = BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]
    first_result: list[bytes] = []
    first = threading.Thread(
        target=lambda: first_result.append(handle.screenshot(timeout=1.0))
    )
    first.start()
    assert engine.session_data.block_entered.wait(1.0)

    with pytest.raises(FutureTimeoutError):
        handle.screenshot(timeout=0.01)

    close_errors: list[BaseException] = []

    def close_handle() -> None:
        try:
            handle.close(timeout=1.0)
        except BaseException as exc:
            close_errors.append(exc)

    closer = threading.Thread(target=close_handle)
    closer.start()
    release_first.set()
    first.join(timeout=1.0)
    closer.join(timeout=1.0)

    assert not first.is_alive()
    assert not closer.is_alive()
    assert close_errors == []
    assert first_result == [b"shot-1"]
    assert engine.session_data.screenshot_count == 1
    assert engine.session_data.closed


def test_browser_session_handle_propagates_startup_failure() -> None:
    engine = _FakeBrowserEngine(startup_error=RuntimeError("browser launch failed"))

    with pytest.raises(RuntimeError, match="browser launch failed"):
        BrowserSessionHandle(_browser_device(), engine)  # type: ignore[arg-type]

    assert engine.connect_thread_id is not None
    assert engine.disconnect_thread_id is None


def test_device_session_service_exposes_handle_not_raw_playwright_data() -> None:
    engine = _FakeBrowserEngine()
    service = DeviceSessionService(browser_engine=engine)  # type: ignore[arg-type]
    session = service.create_session(_browser_device())
    browser_port = session.get_data("browser_data")

    assert isinstance(browser_port, BrowserSessionHandle)
    assert browser_port is not engine.session_data
    assert service.take_screenshot(session) == b"shot-1"

    service.close_session(session.session_id)

    assert service.get_session(session.session_id) is None
    assert engine.disconnect_thread_id == engine.connect_thread_id


def test_failed_close_retains_live_session_for_retry() -> None:
    engine = _FakeBrowserEngine()
    service = DeviceSessionService(browser_engine=engine)  # type: ignore[arg-type]
    session = service.create_session(_browser_device())
    handle = session.get_data("browser_data")
    assert isinstance(handle, BrowserSessionHandle)

    with patch.object(handle, "close", side_effect=TimeoutError("still busy")):
        with pytest.raises(RuntimeError, match="still busy"):
            service.close_session(session.session_id)

    assert service.get_session(session.session_id) is session
    assert not handle.is_closed

    service.close_session(session.session_id)
    assert service.get_session(session.session_id) is None
    assert handle.is_closed


_PROFILE_A = "11111111-1111-4111-8111-111111111111"
_PROFILE_B = "22222222-2222-4222-8222-222222222222"


def _profile_use(label: str) -> ProfileUse:
    return ProfileUse(kind="test", label=label, operation_id=label)


def test_profile_owner_is_uuid_scoped_across_device_variants() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )
    first = service.create_session(_browser_device(profile_id=_PROFILE_A))

    with pytest.raises(BrowserProfileInUseError, match="already in use"):
        service.create_session(
            _browser_device(
                profile_id=_PROFILE_A,
                screen_mode=ScreenMode.MOBILE_ANDROID,
                headless=True,
            )
        )

    second = service.create_session(_browser_device(profile_id=_PROFILE_B))
    assert len(service.list_sessions()) == 2
    assert all(lease is not None for lease in engine.received_leases)

    service.close_session(second.session_id)
    service.close_session(first.session_id)
    assert registry.snapshot(_PROFILE_A) is None
    assert registry.snapshot(_PROFILE_B) is None


def test_temporary_browser_session_does_not_acquire_profile_owner() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )

    session = service.create_session(_browser_device())
    assert engine.received_leases == [None]
    service.close_session(session.session_id)


def test_profile_owner_is_released_when_browser_startup_fails() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine(startup_error=RuntimeError("launch failed"))
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )

    with pytest.raises(RuntimeError, match="launch failed"):
        service.create_session(_browser_device(profile_id=_PROFILE_A))

    assert registry.snapshot(_PROFILE_A) is None


def test_profile_owner_is_quarantined_when_startup_cleanup_is_uncertain() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine(
        startup_error=BrowserSessionTeardownError(
            [RuntimeError("partial browser still alive")]
        )
    )
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )

    with pytest.raises(BrowserSessionTeardownError, match="remain locked"):
        service.create_session(_browser_device(profile_id=_PROFILE_A))

    assert registry.snapshot(_PROFILE_A) is not None


def test_profile_owner_is_quarantined_when_teardown_is_unconfirmed() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine(
        disconnect_error=RuntimeError("browser process still alive"),
    )
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )
    session = service.create_session(_browser_device(profile_id=_PROFILE_A))

    with pytest.raises(RuntimeError, match="browser process still alive"):
        service.close_session(session.session_id)

    snapshot = registry.snapshot(_PROFILE_A)
    assert snapshot is not None
    assert snapshot.closing
    with pytest.raises(BrowserProfileInUseError):
        registry.acquire_maintenance(
            _PROFILE_A,
            _profile_use("unsafe maintenance"),
        )


def test_profile_owner_is_released_if_owner_thread_cannot_start() -> None:
    registry = BrowserProfileLeaseRegistry()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=_FakeBrowserEngine(),
        profile_lease_registry=registry,
    )

    with (
        patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")),
        pytest.raises(RuntimeError, match="no thread"),
    ):
        service.create_session(_browser_device(profile_id=_PROFILE_A))

    assert registry.snapshot(_PROFILE_A) is None


def test_startup_timeout_keeps_owner_until_owner_thread_finishes_teardown() -> None:
    registry = BrowserProfileLeaseRegistry()
    entered = threading.Event()
    release = threading.Event()

    class _BlockingStartupEngine(_FakeBrowserEngine):
        def connect(
            self,
            device: BrowserDevice,
            *,
            profile_lease: object | None = None,
        ) -> _FakeBrowserSessionData:
            entered.set()
            release.wait(2)
            return super().connect(device, profile_lease=profile_lease)

    engine = _BlockingStartupEngine()
    owner = registry.acquire_owner(_PROFILE_A, _profile_use("slow startup"))
    with pytest.raises(FutureTimeoutError):
        BrowserSessionHandle(
            _browser_device(profile_id=_PROFILE_A),
            engine,  # type: ignore[arg-type]
            profile_lease=owner,
            startup_timeout=0.01,
        )

    assert entered.is_set()
    assert registry.snapshot(_PROFILE_A) is not None
    release.set()
    for _ in range(100):
        if registry.snapshot(_PROFILE_A) is None:
            break
        time.sleep(0.01)
    assert registry.snapshot(_PROFILE_A) is None
    assert engine.disconnect_thread_id == engine.connect_thread_id


def test_browser_engine_rejects_missing_released_or_mismatched_owner_before_playwright() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = BrowserDeviceEngine()
    device = _browser_device(profile_id=_PROFILE_A)
    mismatched = registry.acquire_owner(_PROFILE_B, _profile_use("wrong owner"))

    with (
        patch.object(engine, "_profile_path"),
        patch(
            "automadin.engine.browser_engine.sync_playwright"
        ) as start_playwright,
    ):
        with pytest.raises(BrowserProfileLaunchError, match="not reserved"):
            engine.connect(device)
        with pytest.raises(
            BrowserProfileLeaseStateError,
            match="different profile",
        ):
            engine.connect(device, profile_lease=mismatched)
        assert not start_playwright.called

    mismatched.release()
    released = registry.acquire_owner(_PROFILE_A, _profile_use("released owner"))
    released.release()
    with (
        patch.object(engine, "_profile_path"),
        patch(
            "automadin.engine.browser_engine.sync_playwright"
        ) as start_playwright,
    ):
        with pytest.raises(BrowserProfileLeaseStateError, match="released"):
            engine.connect(device, profile_lease=released)
        assert not start_playwright.called

    temporary = _browser_device()
    live_owner = registry.acquire_owner(
        _PROFILE_A,
        _profile_use("temporary mismatch"),
    )
    with patch(
        "automadin.engine.browser_engine.sync_playwright"
    ) as start_playwright:
        with pytest.raises(BrowserProfileLaunchError, match="temporary"):
            engine.connect(temporary, profile_lease=live_owner)
        assert not start_playwright.called
    live_owner.release()


def test_close_refuses_active_borrow_without_disconnecting_or_releasing() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )
    session = service.create_session(_browser_device(profile_id=_PROFILE_A))
    handle = session.get_data("browser_data")
    assert isinstance(handle, BrowserSessionHandle)
    owner = handle.profile_lease
    assert owner is not None
    borrow = owner.borrow(_profile_use("flow run"))

    with pytest.raises(RuntimeError, match="flow run"):
        service.close_session(session.session_id)

    assert service.get_session(session.session_id) is session
    assert engine.disconnect_thread_id is None
    assert registry.snapshot(_PROFILE_A) is not None

    borrow.release()
    service.close_session(session.session_id)
    assert registry.snapshot(_PROFILE_A) is None


class _OwnerThreadExecutor:
    def __init__(
        self,
        *,
        fail: bool = False,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.fail = fail
        self.entered = entered
        self.release = release
        self.thread_ids: list[int] = []
        self.sessions: list[DeviceSession] = []
        self.executables: list[object] = []

    def _record(self, executable: object, ctx: object) -> None:
        self.thread_ids.append(threading.get_ident())
        self.sessions.append(ctx.session)
        self.executables.append(executable)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(2)
        if self.fail:
            raise RuntimeError("execution failed")

    def execute(self, flow: object, ctx: object) -> None:
        commands = getattr(flow, "commands", None)
        self._record(commands[0] if commands is not None else flow, ctx)

    def execute_command(
        self,
        command: object,
        ctx: object,
        *,
        flow_name: str,
    ) -> None:
        assert flow_name == "Interactive command"
        self._record(command, ctx)


def test_owner_thread_flow_and_command_restore_context_and_release_borrow() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )
    session = service.create_session(_browser_device(profile_id=_PROFILE_A))
    handle = session.get_data("browser_data")
    assert isinstance(handle, BrowserSessionHandle)
    original_session = DeviceSession(device=AndroidDevice(device_id="original"))
    ctx = SimpleNamespace(session=original_session)
    executor = _OwnerThreadExecutor()
    flow = object()
    command = object()

    handle.execute_flow(executor, flow, ctx, use=_profile_use("flow"))
    handle.execute_command(executor, command, ctx, use=_profile_use("command"))

    assert executor.executables == [flow, command]
    assert set(executor.thread_ids) == {engine.connect_thread_id}
    assert all(item.device == session.device for item in executor.sessions)
    assert all(
        item.get_data("browser_data") is engine.session_data
        for item in executor.sessions
    )
    assert ctx.session is original_session
    snapshot = registry.snapshot(_PROFILE_A)
    assert snapshot is not None
    assert snapshot.borrowed_by is None
    service.close_session(session.session_id)


def test_owner_thread_execution_failure_restores_context_and_releases_borrow() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )
    session = service.create_session(_browser_device(profile_id=_PROFILE_A))
    handle = session.get_data("browser_data")
    assert isinstance(handle, BrowserSessionHandle)
    original_session = DeviceSession(device=AndroidDevice(device_id="original"))
    ctx = SimpleNamespace(session=original_session)

    with pytest.raises(RuntimeError, match="execution failed"):
        handle.execute_flow(
            _OwnerThreadExecutor(fail=True),
            object(),
            ctx,
            use=_profile_use("failing flow"),
        )

    assert ctx.session is original_session
    snapshot = registry.snapshot(_PROFILE_A)
    assert snapshot is not None
    assert snapshot.borrowed_by is None
    service.close_session(session.session_id)


def test_timed_out_owner_execution_keeps_borrow_until_work_finishes() -> None:
    registry = BrowserProfileLeaseRegistry()
    engine = _FakeBrowserEngine()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )
    session = service.create_session(_browser_device(profile_id=_PROFILE_A))
    handle = session.get_data("browser_data")
    assert isinstance(handle, BrowserSessionHandle)
    entered = threading.Event()
    release = threading.Event()
    executor = _OwnerThreadExecutor(entered=entered, release=release)
    ctx = SimpleNamespace(session=session)

    with pytest.raises(FutureTimeoutError):
        handle.execute_flow(
            executor,
            object(),
            ctx,
            use=_profile_use("slow flow"),
            timeout=0.01,
        )

    assert entered.wait(1.0)
    snapshot = registry.snapshot(_PROFILE_A)
    assert snapshot is not None
    assert snapshot.borrowed_by == _profile_use("slow flow")
    with pytest.raises(BrowserProfileBorrowActiveError, match="slow flow"):
        handle.close()

    release.set()
    for _ in range(100):
        snapshot = registry.snapshot(_PROFILE_A)
        if snapshot is not None and snapshot.borrowed_by is None:
            break
        time.sleep(0.01)
    assert snapshot is not None
    assert snapshot.borrowed_by is None
    service.close_session(session.session_id)


class _FakeCaptureEngine:
    def __init__(self, screenshot: bytes) -> None:
        self.screenshot = screenshot
        self.received: object | None = None

    def take_screenshot(self, data: object) -> bytes:
        self.received = data
        return self.screenshot


def test_device_session_service_routes_android_and_windows_capture() -> None:
    adb = _FakeCaptureEngine(b"android")
    windows = _FakeCaptureEngine(b"windows")
    service = DeviceSessionService(  # type: ignore[arg-type]
        adb_engine=adb,
        windows_engine=windows,
    )
    android_data = object()
    windows_data = object()

    assert service.take_screenshot(
        DeviceSession(
            device=AndroidDevice(device_id="emulator-5554"),
            data={"adb_data": android_data},
        )
    ) == b"android"
    assert service.take_screenshot(
        DeviceSession(
            device=WindowsDevice(hwnd=123, title="Example"),
            data={"windows_data": windows_data},
        )
    ) == b"windows"
    assert adb.received is android_data
    assert windows.received is windows_data


class _SelectorDb:
    @staticmethod
    def profile_engine_specs() -> list[tuple[str, str]]:
        return []

    @staticmethod
    def profile_display_labels() -> dict[str, str]:
        return {}


class _DisconnectService:
    def __init__(self) -> None:
        self.fail = True
        self.calls: list[str] = []

    def close_session(self, session_id: str) -> None:
        self.calls.append(session_id)
        if self.fail:
            raise RuntimeError("owner still closing")


def test_device_selector_keeps_session_visible_after_disconnect_failure() -> None:
    service = _DisconnectService()
    selector = DeviceSelectorWidget(  # type: ignore[arg-type]
        service,
        _SelectorDb(),
        device_operation_registry=DeviceOperationLeaseRegistry(),
    )
    session = DeviceSession(device=AndroidDevice(device_id="emulator-5554"))
    selector._current_session = session
    selector._show_connected()
    disconnected: list[bool] = []
    selector.session_disconnected.connect(lambda: disconnected.append(True))

    with patch("automadin.ui.device_selector.QMessageBox.critical") as message:
        selector._disconnect()
        worker = selector._disconnect_thread
        assert worker is not None
        assert worker.wait(2000)
        QApplication.processEvents()

    assert message.call_count == 1
    assert selector.get_selected_session() is session
    assert selector._disconnect_btn.isVisibleTo(selector)
    assert "remains connected" in selector._status_label.text()
    assert disconnected == []

    service.fail = False
    selector._disconnect()
    worker = selector._disconnect_thread
    assert worker is not None
    assert worker.wait(2000)
    QApplication.processEvents()
    assert selector.get_selected_session() is None
    assert disconnected == [True]

    selector.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def test_device_selector_profile_conflict_omits_runtime_repair_advice() -> None:
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(_PROFILE_A, _profile_use("Active launcher run"))
    with pytest.raises(BrowserProfileInUseError) as caught:
        registry.acquire_owner(_PROFILE_A, _profile_use("New selector session"))

    selector = DeviceSelectorWidget(  # type: ignore[arg-type]
        _DisconnectService(),
        _SelectorDb(),
    )
    try:
        browser_index = selector._type_combo.findData(DeviceType.BROWSER)
        assert browser_index >= 0
        selector._type_combo.setCurrentIndex(browser_index)

        with patch(
            "automadin.ui.device_selector.QMessageBox.critical"
        ) as message:
            selector._on_connect_error(caught.value)

        title = message.call_args.args[1]
        body = message.call_args.args[2]
        assert title == "Browser Profile Already In Use"
        assert "Active launcher run" in body
        assert "Runtime Components" not in body
    finally:
        owner.release()
        selector.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        QApplication.processEvents()


class _SlowCaptureService:
    def __init__(self, session: DeviceSession) -> None:
        self.session = session
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_session(self, session_id: str) -> DeviceSession | None:
        if session_id == self.session.session_id:
            return self.session
        return None

    def take_screenshot(self, _session: DeviceSession) -> bytes:
        self.entered.set()
        self.release.wait(2)
        return _VALID_PNG


class _ConnectedSelector:
    def __init__(self, session: DeviceSession) -> None:
        self._session = session

    def get_selected_session(self) -> DeviceSession:
        return self._session


def test_utility_shutdown_retains_running_capture_worker(tmp_path) -> None:
    db = DatabaseService(str(tmp_path / "utility.db"))
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    service = _SlowCaptureService(session)
    controller = CaptureOperationController(ScreenCaptureService(service))
    selector = _ConnectedSelector(session)
    view = UtilityView(  # type: ignore[arg-type]
        db,
        service,
        selector,
        capture_controller=controller,
    )
    try:
        view._capture_screenshot()
        assert service.entered.wait(1.0)
        operation_id = view._capture_operation_id
        assert operation_id is not None

        assert view.shutdown(wait_ms=5) is False
        assert controller.is_operation_active(operation_id)

        service.release.set()
        assert view.shutdown(wait_ms=1000) is True
        assert not controller.is_busy
    finally:
        service.release.set()
        controller.shutdown(1000)
        view.close()
        view.deleteLater()
        db.close()


def test_visual_rule_dialog_closes_immediately_and_controller_retains_capture() -> None:
    session = DeviceSession(device=AndroidDevice("emulator-5554"))
    service = _SlowCaptureService(session)
    controller = CaptureOperationController(ScreenCaptureService(service))
    selector = _ConnectedSelector(session)
    dialog = VisualRuleDialog(  # type: ignore[arg-type]
        session_service=service,
        device_selector=selector,
        capture_controller=controller,
    )
    dialog.show()
    try:
        dialog._capture_screenshot()
        assert service.entered.wait(1.0)
        operation_id = dialog._capture_operation_id
        assert operation_id is not None

        dialog.reject()
        QApplication.processEvents()
        assert not dialog.isVisible()
        assert dialog._capture_operation_id is None
        assert controller.is_operation_active(operation_id)
    finally:
        service.release.set()
        controller.shutdown(1000)
        dialog.close()
        dialog.deleteLater()
