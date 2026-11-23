from __future__ import annotations

import threading
import time
import uuid

import pytest
from automadin.engine.browser_session import BrowserSessionHandle
from automadin.engine.session import DeviceSessionService
from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.runner import BrowserAwareRunWorker
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
)
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import RunConfig, RunMode, RunStatus
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseRegistry,
)


class _BrowserSessionData:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _BrowserEngine:
    def __init__(self, session_data: _BrowserSessionData) -> None:
        self.session_data = session_data
        self.connect_count = 0

    def connect(self, device, *, profile_lease=None) -> _BrowserSessionData:
        if device.config.profile_id:
            assert profile_lease is not None
            profile_lease.ensure_live(device.config.profile_id)
        else:
            assert profile_lease is None
        self.connect_count += 1
        return self.session_data

    def disconnect(self, session_data: _BrowserSessionData) -> None:
        assert session_data is self.session_data
        session_data.close()


def test_browser_step_through_preserves_context_and_pauses_each_command() -> None:
    device = BrowserDevice(
        config=BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM)
    )
    browser_data = _BrowserSessionData()
    engine = _BrowserEngine(browser_data)
    handle = BrowserSessionHandle(device, engine)  # type: ignore[arg-type]
    original_session = DeviceSession(
        device=device,
        data={"browser_data": handle, "original": True},
    )
    ctx = FlowContext(original_session)
    ctx.variables["account"] = "personal"
    ctx.command_last_run_times["earlier"] = 10.0
    ctx.elapsed_block_last_enter_times["block"] = 20.0
    ctx._last_screenshot = b"previous"
    ctx.current_short_id = "old"
    ctx.enable_step_through()

    first_ran = threading.Event()
    second_ran = threading.Event()
    executor = FlowExecutor()
    executor.register_handler("first", lambda _cmd, _ctx: first_ran.set())
    executor.register_handler("second", lambda _cmd, _ctx: second_ran.set())
    flow = AutomationFlow(
        name="browser step-through",
        commands=[FlowCommand("first"), FlowCommand("second")],
    )
    worker = BrowserAwareRunWorker(
        executor=executor,
        flow=flow,
        ctx=ctx,
        config=RunConfig(),
    )

    try:
        run_thread = threading.Thread(target=worker.run)
        run_thread.start()

        assert not first_ran.wait(timeout=0.05)
        assert worker.ctx is ctx
        assert worker.ctx._step_through
        assert not worker.ctx._paused.is_set()

        worker.ctx.resume()
        assert first_ran.wait(timeout=1.0)

        deadline = time.monotonic() + 1.0
        while worker.ctx._paused.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not worker.ctx._paused.is_set()
        assert not second_ran.is_set()

        worker.ctx.resume()
        assert second_ran.wait(timeout=1.0)
        run_thread.join(timeout=2.0)
    finally:
        handle.close()

    assert not run_thread.is_alive()
    assert engine.connect_count == 1
    assert worker.ctx is ctx
    assert worker.ctx.session is original_session
    assert worker.ctx.variables == {"account": "personal"}
    assert worker.ctx.command_last_run_times["earlier"] == 10.0
    assert worker.ctx.elapsed_block_last_enter_times["block"] == 20.0
    assert browser_data.closed
    assert worker.status == RunStatus.COMPLETED


def test_browser_step_through_worker_can_resume_after_each_command() -> None:
    device = BrowserDevice(
        config=BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM)
    )
    browser_data = _BrowserSessionData()
    handle = BrowserSessionHandle(device, _BrowserEngine(browser_data))  # type: ignore[arg-type]
    ctx = FlowContext(
        DeviceSession(device=device, data={"browser_data": handle})
    )
    first_ran = threading.Event()
    second_ran = threading.Event()
    executor = FlowExecutor()
    executor.register_handler("first", lambda _cmd, _ctx: first_ran.set())
    executor.register_handler("second", lambda _cmd, _ctx: second_ran.set())
    worker = BrowserAwareRunWorker(
        executor=executor,
        flow=AutomationFlow(
            name="browser repeated next step",
            commands=[FlowCommand("first"), FlowCommand("second")],
        ),
        ctx=ctx,
        config=RunConfig(),
    )
    ctx.enable_step_through()
    worker.request_pause()

    try:
        run_thread = threading.Thread(target=worker.run)
        run_thread.start()

        deadline = time.monotonic() + 1.0
        while worker.status is not RunStatus.PAUSED and time.monotonic() < deadline:
            time.sleep(0.001)
        assert worker.status is RunStatus.PAUSED

        assert worker.request_resume()
        assert first_ran.wait(timeout=1.0)

        deadline = time.monotonic() + 1.0
        while worker.status is not RunStatus.PAUSED and time.monotonic() < deadline:
            time.sleep(0.001)
        assert worker.status is RunStatus.PAUSED
        assert not second_ran.is_set()

        assert worker.request_resume()
        assert second_ran.wait(timeout=1.0)
        run_thread.join(timeout=2.0)
    finally:
        ctx.stop()
        handle.close()

    assert not run_thread.is_alive()
    assert worker.status is RunStatus.COMPLETED


def test_debugger_holds_profile_borrow_between_repeated_runs() -> None:
    profile_id = str(uuid.uuid4())
    device = BrowserDevice(
        config=BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_name="Work",
            profile_id=profile_id,
        )
    )
    browser_data = _BrowserSessionData()
    engine = _BrowserEngine(browser_data)
    registry = BrowserProfileLeaseRegistry()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )
    session = service.create_session(device)
    ctx = FlowContext(session)
    first_ran = threading.Event()
    second_ran = threading.Event()
    calls: list[int] = []
    executor = FlowExecutor()

    def record_run(_command, _ctx) -> None:
        calls.append(len(calls) + 1)
        (first_ran if len(calls) == 1 else second_ran).set()

    executor.register_handler("record", record_run)
    worker = BrowserAwareRunWorker(
        executor=executor,
        flow=AutomationFlow(
            name="repeated browser flow",
            commands=[FlowCommand("record")],
        ),
        ctx=ctx,
        config=RunConfig(
            mode=RunMode.COUNT,
            count=2,
            delay_between_runs_ms=300,
        ),
    )
    run_thread = threading.Thread(target=worker.run)

    try:
        run_thread.start()
        assert first_ran.wait(1)
        assert not second_ran.wait(0.05)
        snapshot = registry.snapshot(profile_id)
        assert snapshot is not None
        assert snapshot.borrowed_by is not None
        assert snapshot.borrowed_by.kind == "flow-debugger"

        with pytest.raises(RuntimeError, match="Flow debugger"):
            service.close_session(session.session_id)

        run_thread.join(2)
        assert not run_thread.is_alive()
        assert second_ran.is_set()
        assert registry.snapshot(profile_id).borrowed_by is None
    finally:
        if run_thread.is_alive():
            ctx.stop()
            run_thread.join(2)
        service.close_session(session.session_id)

    assert registry.snapshot(profile_id) is None


def test_connected_run_can_reserve_profile_borrow_before_worker_start() -> None:
    profile_id = str(uuid.uuid4())
    device = BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_name="Scheduled",
            profile_id=profile_id,
        )
    )
    engine = _BrowserEngine(_BrowserSessionData())
    registry = BrowserProfileLeaseRegistry()
    service = DeviceSessionService(  # type: ignore[arg-type]
        browser_engine=engine,
        profile_lease_registry=registry,
    )
    session = service.create_session(device)
    flow = AutomationFlow("Scheduled", [FlowCommand("wait", params={"ms": 1})])
    first = BrowserAwareRunWorker(
        FlowExecutor(),
        flow,
        FlowContext(session),
        RunConfig(),
        profile_use_kind="run-setup",
        profile_use_label="First scheduled run",
    )
    second = BrowserAwareRunWorker(
        FlowExecutor(),
        flow,
        FlowContext(session),
        RunConfig(),
        profile_use_kind="run-setup",
        profile_use_label="Second scheduled run",
    )

    try:
        first.reserve_browser_borrow()
        snapshot = registry.snapshot(profile_id)
        assert snapshot is not None
        assert snapshot.borrowed_by is not None
        assert snapshot.borrowed_by.label == "First scheduled run"

        with pytest.raises(BrowserProfileInUseError):
            second.reserve_browser_borrow()

        assert first.release_reserved_profile_lease()
        assert registry.snapshot(profile_id).borrowed_by is None
    finally:
        first.release_reserved_profile_lease()
        second.release_reserved_profile_lease()
        service.close_session(session.session_id)
