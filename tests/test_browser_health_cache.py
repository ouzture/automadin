from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from automadin.runtime import RuntimeComponentStatus
from automadin.services.browser_health import BrowserHealthResult, BrowserHealthState
from automadin.services.browser_health_cache import BrowserHealthCache
from automadin.services import browser_health_cache


def _status(path: Path, *, source: str = "system") -> RuntimeComponentStatus:
    return RuntimeComponentStatus(source=source, path=path, usable=True)


def _unavailable(message: str = "not installed") -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        source="unavailable",
        path=None,
        usable=False,
        error=message,
    )


def _statuses(chrome: RuntimeComponentStatus) -> dict[str, RuntimeComponentStatus]:
    return {
        "playwright": _unavailable(),
        "chrome": chrome,
        "msedge": _unavailable(),
    }


def _healthy(target: str) -> BrowserHealthResult:
    return BrowserHealthResult(
        target=target,
        state=BrowserHealthState.HEALTHY,
        stage="launch",
        checked_at=datetime.now(UTC),
        duration=0.01,
        message="healthy",
    )


def test_ttl_reuses_then_expires_result(tmp_path: Path) -> None:
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"chrome")
    now = [100.0]
    calls: list[str] = []

    def checker(target: str, **_kwargs: object) -> BrowserHealthResult:
        calls.append(target)
        return _healthy(target)

    cache = BrowserHealthCache(ttl_seconds=10, checker=checker, monotonic=lambda: now[0])
    first = cache.check_many(_statuses(_status(executable)), tmp_path)
    second = cache.check_many(_statuses(_status(executable)), tmp_path)
    now[0] += 11
    third = cache.check_many(_statuses(_status(executable)), tmp_path)

    assert first["chrome"] is second["chrome"]
    assert third["chrome"] is not second["chrome"]
    assert calls == ["chrome", "chrome"]


def test_file_repair_changes_fingerprint_before_ttl_expiry(tmp_path: Path) -> None:
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"old")
    calls = 0

    def checker(target: str, **_kwargs: object) -> BrowserHealthResult:
        nonlocal calls
        calls += 1
        return _healthy(target)

    cache = BrowserHealthCache(ttl_seconds=600, checker=checker)
    cache.check_many(_statuses(_status(executable)), tmp_path)
    executable.write_bytes(b"repaired-browser-payload")
    cache.check_many(_statuses(_status(executable)), tmp_path)

    assert calls == 2


def test_playwright_repair_invalidates_only_affected_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = tmp_path / "playwright"
    for target in ("chromium", "firefox", "webkit"):
        install = payload / f"{target}-123"
        install.mkdir(parents=True)
        (install / "INSTALLATION_COMPLETE").touch()
    chromium = payload / "chromium-123" / "chrome-win" / "chrome.exe"
    firefox = payload / "firefox-123" / "firefox" / "firefox.exe"
    webkit = payload / "webkit-123" / "Playwright.exe"
    chromium.parent.mkdir()
    firefox.parent.mkdir()
    chromium.write_bytes(b"chromium")
    firefox.write_bytes(b"firefox")
    webkit.write_bytes(b"webkit")
    monkeypatch.setattr(
        browser_health_cache,
        "playwright_browser_revisions",
        lambda: {target: "123" for target in ("chromium", "firefox", "webkit")},
    )
    calls: list[str] = []

    def checker(target: str, **_kwargs: object) -> BrowserHealthResult:
        calls.append(target)
        return _healthy(target)

    cache = BrowserHealthCache(checker=checker)
    statuses = {
        "playwright": _status(payload, source="managed"),
        "chrome": _unavailable(),
        "msedge": _unavailable(),
    }
    cache.check_many(statuses, tmp_path)
    chromium.write_bytes(b"repaired chromium executable")
    cache.check_many(statuses, tmp_path)

    assert calls.count("chromium") == 2
    assert calls.count("firefox") == 1
    assert calls.count("webkit") == 1


def test_concurrent_callers_single_flight_same_fingerprint(tmp_path: Path) -> None:
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"chrome")
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def checker(target: str, **_kwargs: object) -> BrowserHealthResult:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return _healthy(target)

    cache = BrowserHealthCache(checker=checker)
    outputs: list[dict[str, BrowserHealthResult]] = []

    def run() -> None:
        outputs.append(cache.check_many(_statuses(_status(executable)), tmp_path))

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert entered.wait(2)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(2)
    second.join(2)

    assert calls == 1
    assert len(outputs) == 2
    assert outputs[0]["chrome"] is outputs[1]["chrome"]


def test_invalidate_starts_new_generation_and_old_flight_cannot_repopulate(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"chrome")
    first_entered = threading.Event()
    release_first = threading.Event()
    calls = 0
    call_lock = threading.Lock()

    def checker(target: str, **_kwargs: object) -> BrowserHealthResult:
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(2)
        result = _healthy(target)
        return BrowserHealthResult(
            target=result.target,
            state=result.state,
            stage=result.stage,
            checked_at=result.checked_at,
            duration=result.duration,
            message=f"call {call_number}",
        )

    cache = BrowserHealthCache(max_workers=2, checker=checker)
    first_output: list[dict[str, BrowserHealthResult]] = []
    first = threading.Thread(
        target=lambda: first_output.append(
            cache.check_many(_statuses(_status(executable)), tmp_path)
        )
    )
    first.start()
    assert first_entered.wait(2)

    cache.invalidate()
    after_invalidation = cache.check_many(
        _statuses(_status(executable)),
        tmp_path,
    )
    release_first.set()
    first.join(2)
    cached = cache.check_many(_statuses(_status(executable)), tmp_path)

    assert not first.is_alive()
    assert calls == 2
    assert first_output[0]["chrome"].message == "call 1"
    assert after_invalidation["chrome"].message == "call 2"
    assert cached["chrome"] is after_invalidation["chrome"]


def test_waiting_caller_can_cancel_without_stopping_shared_probe(tmp_path: Path) -> None:
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"chrome")
    entered = threading.Event()
    release = threading.Event()

    def checker(target: str, **_kwargs: object) -> BrowserHealthResult:
        entered.set()
        assert release.wait(2)
        return _healthy(target)

    cache = BrowserHealthCache(checker=checker)
    leader = threading.Thread(
        target=lambda: cache.check_many(_statuses(_status(executable)), tmp_path)
    )
    leader.start()
    assert entered.wait(2)

    cancelled = cache.check_many(
        _statuses(_status(executable)),
        tmp_path,
        cancel=lambda: True,
    )
    release.set()
    leader.join(2)

    assert cancelled["chrome"].state is BrowserHealthState.CANCELLED


def test_waiter_cancel_hook_exception_is_failed_and_releases_waiter(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"chrome")
    entered = threading.Event()
    waiter_finished = threading.Event()
    leader_output: list[dict[str, BrowserHealthResult]] = []

    def checker(
        target: str,
        *,
        cancel,
        **_kwargs: object,
    ) -> BrowserHealthResult:
        entered.set()
        assert waiter_finished.wait(2)
        if cancel():
            return BrowserHealthResult(
                target=target,
                state=BrowserHealthState.CANCELLED,
                stage="cancel",
                checked_at=datetime.now(UTC),
                duration=0,
                message="cancelled",
            )
        return _healthy(target)

    cache = BrowserHealthCache(checker=checker)
    leader = threading.Thread(
        target=lambda: leader_output.append(
            cache.check_many(
                _statuses(_status(executable)),
                tmp_path,
                cancel=lambda: True,
            )
        )
    )
    leader.start()
    assert entered.wait(2)

    def broken_cancel() -> bool:
        raise RuntimeError("cancel hook broke")

    waiter = cache.check_many(
        _statuses(_status(executable)),
        tmp_path,
        cancel=broken_cancel,
    )
    waiter_finished.set()
    leader.join(2)

    assert not leader.is_alive()
    assert waiter["chrome"].state is BrowserHealthState.FAILED
    assert waiter["chrome"].stage == "coordination"
    assert "RuntimeError" in waiter["chrome"].message
    assert leader_output[0]["chrome"].state is BrowserHealthState.CANCELLED


def test_cancelled_probe_is_not_cached(tmp_path: Path) -> None:
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"chrome")
    calls = 0

    def checker(
        target: str,
        *,
        cancel,
        **_kwargs: object,
    ) -> BrowserHealthResult:
        nonlocal calls
        calls += 1
        if cancel():
            return BrowserHealthResult(
                target=target,
                state=BrowserHealthState.CANCELLED,
                stage="cancel",
                checked_at=datetime.now(UTC),
                duration=0,
                message="cancelled",
            )
        return _healthy(target)

    cache = BrowserHealthCache(checker=checker)
    assert (
        cache.check_many(
            _statuses(_status(executable)), tmp_path, cancel=lambda: True
        )["chrome"].state
        is BrowserHealthState.CANCELLED
    )
    assert (
        cache.check_many(_statuses(_status(executable)), tmp_path)["chrome"].state
        is BrowserHealthState.HEALTHY
    )
    assert calls == 2


def test_unavailable_status_does_not_spawn_and_targets_are_independent(
    tmp_path: Path,
) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"chrome")
    calls: list[str] = []

    def checker(target: str, **_kwargs: object) -> BrowserHealthResult:
        calls.append(target)
        return _healthy(target)

    results = BrowserHealthCache(checker=checker).check_many(
        _statuses(_status(chrome)),
        tmp_path,
    )

    assert tuple(results) == ("chromium", "firefox", "webkit", "chrome", "msedge")
    assert calls == ["chrome"]
    assert results["chrome"].state is BrowserHealthState.HEALTHY
    assert all(
        results[target].state is BrowserHealthState.UNAVAILABLE
        for target in ("chromium", "firefox", "webkit", "msedge")
    )


def test_check_many_limits_probe_concurrency_and_passes_payload(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "playwright"
    payload.mkdir()
    chrome = tmp_path / "chrome.exe"
    edge = tmp_path / "msedge.exe"
    chrome.write_bytes(b"chrome")
    edge.write_bytes(b"edge")
    statuses = {
        "playwright": _status(payload, source="managed"),
        "chrome": _status(chrome),
        "msedge": _status(edge),
    }
    lock = threading.Lock()
    active = 0
    maximum = 0
    payloads: dict[str, Path | None] = {}

    def checker(
        target: str,
        *,
        playwright_browsers_path: Path | None,
        **_kwargs: object,
    ) -> BrowserHealthResult:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            payloads[target] = playwright_browsers_path
        time.sleep(0.03)
        with lock:
            active -= 1
        return _healthy(target)

    results = BrowserHealthCache(max_workers=2, checker=checker).check_many(
        statuses,
        tmp_path,
    )

    assert maximum == 2
    assert all(result.state is BrowserHealthState.HEALTHY for result in results.values())
    assert all(payloads[target] == payload for target in ("chromium", "firefox", "webkit"))
    assert payloads["chrome"] is None
    assert payloads["msedge"] is None


def test_max_workers_is_shared_across_overlapping_check_many_calls(
    tmp_path: Path,
) -> None:
    first_executable = tmp_path / "first" / "chrome.exe"
    second_executable = tmp_path / "second" / "chrome.exe"
    first_executable.parent.mkdir()
    second_executable.parent.mkdir()
    first_executable.write_bytes(b"first")
    second_executable.write_bytes(b"second")
    lock = threading.Lock()
    release = threading.Event()
    first_entered = threading.Event()
    active = 0
    maximum = 0

    def checker(target: str, **_kwargs: object) -> BrowserHealthResult:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            first_entered.set()
        assert release.wait(2)
        with lock:
            active -= 1
        return _healthy(target)

    cache = BrowserHealthCache(max_workers=1, checker=checker)
    outputs: list[dict[str, BrowserHealthResult]] = []
    first = threading.Thread(
        target=lambda: outputs.append(
            cache.check_many(_statuses(_status(first_executable)), tmp_path)
        )
    )
    second = threading.Thread(
        target=lambda: outputs.append(
            cache.check_many(_statuses(_status(second_executable)), tmp_path)
        )
    )
    first.start()
    assert first_entered.wait(2)
    second.start()
    time.sleep(0.05)

    assert maximum == 1
    release.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(outputs) == 2
    assert maximum == 1
