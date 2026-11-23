from __future__ import annotations

import threading

import pytest

from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    WindowsDevice,
)
from automadin.services.device_operation_leases import (
    DeviceOperationConflictError,
    DeviceOperationLeaseRegistry,
    DeviceOperationUse,
    default_device_operation_registry,
    device_operation_key,
)


def _use(kind: str, operation_id: str = "operation-1") -> DeviceOperationUse:
    return DeviceOperationUse(
        kind=kind,
        label=f"{kind.title()} operation",
        operation_id=operation_id,
    )


def test_device_operation_key_tracks_stable_android_provider_identity() -> None:
    first = AndroidDevice(
        device_id="127.0.0.1:5555",
        provider_id="mumu",
        provider_instance_id="instance-7",
        provider_display_name="MuMu 7",
    )
    reconnected = AndroidDevice(
        device_id="127.0.0.1:7555",
        provider_id="mumu",
        provider_instance_id="instance-7",
        provider_display_name="Renamed instance",
    )

    assert device_operation_key(first) == device_operation_key(reconnected)
    assert device_operation_key(first).startswith("android:")


def test_device_operation_key_normalizes_window_title_across_hwnd_refresh() -> None:
    first = WindowsDevice(hwnd=100, title="  Calculator   App ")
    refreshed = WindowsDevice(hwnd=900, title="calculator app")

    assert device_operation_key(first) == "windows-title:calculator app"
    assert device_operation_key(first) == device_operation_key(refreshed)


def test_device_operation_key_rejects_browser_targets() -> None:
    browser = BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))

    with pytest.raises(TypeError, match="Android and Windows"):
        device_operation_key(browser)


def test_same_session_is_exclusive_and_conflict_exposes_current_use() -> None:
    registry = DeviceOperationLeaseRegistry()
    current = registry.acquire("session-1", _use("capture"))
    requested = _use("run", "operation-2")

    with pytest.raises(DeviceOperationConflictError) as caught:
        registry.acquire("session-1", requested)

    assert caught.value.session_id == "session-1"
    assert caught.value.current == current.use
    assert caught.value.requested == requested
    assert "Capture operation" in str(caught.value)
    assert registry.active_count == 1


def test_different_sessions_can_be_owned_independently() -> None:
    registry = DeviceOperationLeaseRegistry()
    first = registry.acquire("session-1", _use("capture"))
    second = registry.acquire("session-2", _use("run"))

    assert registry.active_count == 2
    assert registry.snapshot("session-1") == first.use
    assert registry.snapshot("session-2") == second.use
    first.release()
    second.release()
    assert registry.active_count == 0


def test_release_is_idempotent_and_context_manager_releases_on_error() -> None:
    registry = DeviceOperationLeaseRegistry()
    lease = registry.acquire("session-1", _use("capture"))

    assert lease.release() is True
    assert lease.release() is False
    assert lease.released is True

    with pytest.raises(RuntimeError, match="body failed"):
        with registry.acquire("session-1", _use("run")) as active:
            assert active.released is False
            raise RuntimeError("body failed")

    assert registry.snapshot("session-1") is None


def test_session_ids_and_use_fields_are_normalized() -> None:
    registry = DeviceOperationLeaseRegistry()
    use = DeviceOperationUse(" capture ", " Capture current screen ", " op-1 ")
    lease = registry.acquire("  session-1  ", use)

    assert lease.session_id == "session-1"
    assert lease.use == DeviceOperationUse(
        "capture",
        "Capture current screen",
        "op-1",
    )
    assert registry.snapshot(" session-1 ") == lease.use
    lease.release()


@pytest.mark.parametrize("value", ["", " ", "\t\r\n"])
def test_empty_session_ids_are_rejected(value: str) -> None:
    registry = DeviceOperationLeaseRegistry()

    with pytest.raises(ValueError, match="session ID"):
        registry.acquire(value, _use("capture"))
    with pytest.raises(ValueError, match="session ID"):
        registry.snapshot(value)


def test_non_text_session_id_and_invalid_use_are_rejected() -> None:
    registry = DeviceOperationLeaseRegistry()

    with pytest.raises(TypeError, match="session ID"):
        registry.acquire(None, _use("capture"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DeviceOperationUse"):
        registry.acquire("session-1", object())  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["kind", "label", "operation_id"])
@pytest.mark.parametrize("value", ["", "   ", None])
def test_use_requires_nonempty_text(field: str, value: object) -> None:
    values: dict[str, object] = {
        "kind": "capture",
        "label": "Capture operation",
        "operation_id": "operation-1",
    }
    values[field] = value
    expected = TypeError if value is None else ValueError

    with pytest.raises(expected, match=field):
        DeviceOperationUse(**values)  # type: ignore[arg-type]


def test_concurrent_acquisition_has_exactly_one_winner() -> None:
    registry = DeviceOperationLeaseRegistry()
    contender_count = 16
    barrier = threading.Barrier(contender_count)
    winners = []
    conflicts: list[DeviceOperationConflictError] = []
    result_lock = threading.Lock()

    def contend(index: int) -> None:
        use = _use("capture", f"operation-{index}")
        barrier.wait(timeout=10)
        try:
            lease = registry.acquire("session-1", use)
        except DeviceOperationConflictError as exc:
            with result_lock:
                conflicts.append(exc)
        else:
            with result_lock:
                winners.append(lease)

    threads = [
        threading.Thread(target=contend, args=(index,))
        for index in range(contender_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(winners) == 1
    assert len(conflicts) == contender_count - 1
    assert all(conflict.current == winners[0].use for conflict in conflicts)
    assert registry.active_count == 1
    winners[0].release()


def test_stale_generation_and_token_cannot_release_replacement_owner() -> None:
    registry = DeviceOperationLeaseRegistry()
    old = registry.acquire("session-1", _use("capture"))
    old_generation = old.generation
    old_token = old._token  # noqa: SLF001
    assert old.release() is True

    replacement = registry.acquire("session-1", _use("run", "operation-2"))
    assert replacement.generation > old_generation

    assert registry._release(  # noqa: SLF001
        "session-1",
        old_generation,
        old_token,
    ) is False
    assert registry._release(  # noqa: SLF001
        "session-1",
        replacement.generation,
        old_token,
    ) is False
    assert registry.snapshot("session-1") == replacement.use
    replacement.release()


def test_default_registry_is_a_process_singleton() -> None:
    assert default_device_operation_registry() is default_device_operation_registry()
