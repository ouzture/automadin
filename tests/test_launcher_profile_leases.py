from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from automadin.engine.browser_engine import BrowserSessionTeardownError
from automadin.execution.executor import FlowExecutor
from automadin.models.device import BrowserDevice, BrowserDeviceConfig, BrowserEngine
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import RunConfig, RunStatus
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.ui.launcher.launcher_view import (
    LauncherRunWorker,
    _acquire_launcher_profile_lease,
)


class _SessionService:
    def __init__(self, registry: BrowserProfileLeaseRegistry) -> None:
        self.browser_profile_lease_registry = registry


def _persistent_device(profile_id: str) -> BrowserDevice:
    return BrowserDevice(
        BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_name="Work",
            profile_id=profile_id,
        )
    )


def _worker(
    device: BrowserDevice,
    session_service: _SessionService,
    profile_lease=None,
    *,
    executor: FlowExecutor | None = None,
    flow: AutomationFlow | None = None,
) -> LauncherRunWorker:
    return LauncherRunWorker(
        executor=executor or FlowExecutor(),
        flow=flow or AutomationFlow(name="Launcher lease test"),
        config=RunConfig(),
        device=device,
        session_service=session_service,
        profile_lease=profile_lease,
    )


def test_launcher_profile_conflict_identifies_the_active_owner() -> None:
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    service = _SessionService(registry)
    current = registry.acquire_owner(
        profile_id,
        ProfileUse("interactive-browser", "Design browser", "selector-1"),
    )

    with pytest.raises(BrowserProfileInUseError) as caught:
        _acquire_launcher_profile_lease(
            _persistent_device(profile_id),
            service,
            run_id="run-1",
            run_label="Daily account",
        )

    assert caught.value.current.use.label == "Design browser"
    assert caught.value.requested.use.label == "Run setup 'Daily account'"
    current.release()


def test_launcher_worker_releases_profile_after_success_and_closes_browser_first() -> None:
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    service = _SessionService(registry)
    lease = _acquire_launcher_profile_lease(
        _persistent_device(profile_id),
        service,
        run_id="run-success",
        run_label="Success",
    )
    assert lease is not None

    close_snapshots = []

    class _BrowserData:
        def close(self) -> None:
            close_snapshots.append(registry.snapshot(profile_id))

    worker = _worker(_persistent_device(profile_id), service, lease)
    with patch(
        "automadin.engine.browser_engine.BrowserDeviceEngine.connect",
        return_value=_BrowserData(),
    ) as connect:
        worker.run()

    assert connect.call_args.kwargs["profile_lease"] is lease
    assert close_snapshots[0] is not None
    assert registry.snapshot(profile_id) is None
    assert lease.released
    assert worker.status == RunStatus.COMPLETED


def test_launcher_worker_releases_profile_after_connect_failure() -> None:
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    service = _SessionService(registry)
    device = _persistent_device(profile_id)
    lease = _acquire_launcher_profile_lease(
        device,
        service,
        run_id="run-connect-failure",
        run_label="Connect failure",
    )
    worker = _worker(device, service, lease)

    with patch(
        "automadin.engine.browser_engine.BrowserDeviceEngine.connect",
        side_effect=RuntimeError("browser could not start"),
    ):
        worker.run()

    assert registry.snapshot(profile_id) is None
    assert lease is not None and lease.released
    assert worker.status == RunStatus.ERROR


def test_launcher_worker_retains_lease_when_attachment_state_check_fails() -> None:
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    service = _SessionService(registry)
    device = _persistent_device(profile_id)
    worker = _worker(device, service)
    lease = _acquire_launcher_profile_lease(
        device,
        service,
        run_id="run-attachment-failure",
        run_label="Attachment failure",
    )
    assert lease is not None

    with (
        patch.object(worker, "isRunning", side_effect=RuntimeError("state unavailable")),
        pytest.raises(RuntimeError, match="state unavailable"),
    ):
        worker.attach_reserved_profile_lease(lease)

    assert worker._profile_lease is None
    assert worker._profile_cleanup_leases == [lease]
    assert registry.snapshot(profile_id) is not None

    original_release = registry._release_primary
    release_calls = 0

    def fail_once(profile_key, generation, token, mode):
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise RuntimeError("transient release failure")
        return original_release(profile_key, generation, token, mode)

    with patch.object(registry, "_release_primary", side_effect=fail_once):
        assert not worker.release_reserved_profile_lease()
        assert registry.snapshot(profile_id) is not None
        assert worker.release_reserved_profile_lease()

    assert registry.snapshot(profile_id) is None
    assert lease.released


def test_launcher_worker_quarantines_profile_after_uncertain_startup_cleanup() -> None:
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    service = _SessionService(registry)
    device = _persistent_device(profile_id)
    lease = _acquire_launcher_profile_lease(
        device,
        service,
        run_id="run-startup-quarantine",
        run_label="Startup quarantine",
    )
    worker = _worker(device, service, lease)

    with patch(
        "automadin.engine.browser_engine.BrowserDeviceEngine.connect",
        side_effect=BrowserSessionTeardownError(
            [RuntimeError("partial browser still alive")]
        ),
    ):
        worker.run()

    assert lease is not None and not lease.released
    assert registry.snapshot(profile_id) is not None
    assert worker.status == RunStatus.ERROR
    assert not worker.release_reserved_profile_lease()
    assert registry.snapshot(profile_id) is not None


def test_launcher_worker_releases_profile_after_execution_failure() -> None:
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    service = _SessionService(registry)
    device = _persistent_device(profile_id)
    lease = _acquire_launcher_profile_lease(
        device,
        service,
        run_id="run-execution-failure",
        run_label="Execution failure",
    )
    executor = FlowExecutor()

    def fail(_command, _ctx) -> None:
        raise RuntimeError("step failed")

    executor.register_handler("fail", fail)
    flow = AutomationFlow(
        name="Failing flow",
        commands=[FlowCommand(id="fail")],
    )
    closed = []

    class _BrowserData:
        def close(self) -> None:
            closed.append(True)

    worker = _worker(
        device,
        service,
        lease,
        executor=executor,
        flow=flow,
    )
    with patch(
        "automadin.engine.browser_engine.BrowserDeviceEngine.connect",
        return_value=_BrowserData(),
    ):
        worker.run()

    assert closed == [True]
    assert registry.snapshot(profile_id) is None
    assert lease is not None and lease.released
    assert worker.status == RunStatus.ERROR


def test_launcher_worker_quarantines_profile_after_unconfirmed_teardown() -> None:
    profile_id = str(uuid.uuid4())
    registry = BrowserProfileLeaseRegistry()
    service = _SessionService(registry)
    device = _persistent_device(profile_id)
    lease = _acquire_launcher_profile_lease(
        device,
        service,
        run_id="run-teardown-failure",
        run_label="Teardown failure",
    )

    class _BrowserData:
        def close(self) -> None:
            raise RuntimeError("browser still owns profile")

    worker = _worker(device, service, lease)
    errors: list[str] = []
    worker.error_occurred.connect(errors.append)
    with patch(
        "automadin.engine.browser_engine.BrowserDeviceEngine.connect",
        return_value=_BrowserData(),
    ):
        worker.run()

    assert lease is not None and not lease.released
    assert registry.snapshot(profile_id) is not None
    assert worker.status == RunStatus.ERROR
    assert errors and "locked until Automadin restarts" in errors[-1]
    assert not worker.release_reserved_profile_lease()
    assert registry.snapshot(profile_id) is not None


def test_temporary_launcher_browser_does_not_request_a_profile_lease() -> None:
    registry = BrowserProfileLeaseRegistry()
    service = _SessionService(registry)
    device = BrowserDevice(BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM))

    lease = _acquire_launcher_profile_lease(
        device,
        service,
        run_id="temporary-run",
        run_label="Temporary",
    )
    worker = _worker(device, service, lease)

    class _BrowserData:
        def close(self) -> None:
            pass

    with patch(
        "automadin.engine.browser_engine.BrowserDeviceEngine.connect",
        return_value=_BrowserData(),
    ) as connect:
        worker.run()

    assert lease is None
    assert connect.call_args.kwargs["profile_lease"] is None
