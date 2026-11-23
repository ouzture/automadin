"""Thread-safe, process-memory cache for isolated browser launch checks."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TypeAlias

from automadin.runtime import (
    BROWSER_EXECUTABLE_PATTERNS,
    RuntimeComponentStatus,
    playwright_browser_revisions,
)
from automadin.services.browser_health import (
    BrowserHealthResult,
    BrowserHealthState,
    check_browser_launch,
)

_TARGETS = ("chromium", "firefox", "webkit", "chrome", "msedge")
_PLAYWRIGHT_TARGETS = frozenset({"chromium", "firefox", "webkit"})
_FINGERPRINT_PROTOCOL = "automadin-browser-health-cache-v1"

CancelHook: TypeAlias = Callable[[], bool]
Checker: TypeAlias = Callable[..., BrowserHealthResult]
CommandBuilder: TypeAlias = Callable[[str], Sequence[str]]
Fingerprint: TypeAlias = tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    result: BrowserHealthResult
    expires_at: float


@dataclass(slots=True)
class _Flight:
    event: threading.Event
    generation: int
    waiters: int = 1
    result: BrowserHealthResult | None = None


def _result(
    target: str,
    state: BrowserHealthState,
    stage: str,
    message: str,
) -> BrowserHealthResult:
    return BrowserHealthResult(
        target=target,
        state=state,
        stage=stage,
        checked_at=datetime.now(UTC),
        duration=0.0,
        message=message,
    )


def _stat_fingerprint(path: Path | None) -> tuple[object, ...]:
    if path is None:
        return (None, None, None)
    try:
        stat = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _normalized_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    return Path(os.path.normcase(os.path.abspath(path.expanduser())))


def _playwright_package_version() -> str:
    try:
        return metadata.version("playwright")
    except metadata.PackageNotFoundError:
        return "missing"


def _playwright_executable(
    root: Path,
    target: str,
    revision: str | None,
) -> tuple[Path | None, Path | None]:
    if revision is None:
        return None, None
    install_dir = root / f"{target}-{revision}"
    marker = install_dir / "INSTALLATION_COMPLETE"
    for pattern in BROWSER_EXECUTABLE_PATTERNS[target]:
        executable = next(
            (candidate for candidate in install_dir.glob(pattern) if candidate.is_file()),
            None,
        )
        if executable is not None:
            return executable, marker
    return None, marker


class BrowserHealthCache:
    """Coordinate and cache browser probes across UI-independent callers."""

    def __init__(
        self,
        ttl_seconds: float = 600,
        timeout_seconds: float = 15,
        max_workers: int = 2,
        checker: Checker = check_browser_launch,
        command_builder: CommandBuilder | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than zero")
        self._ttl_seconds = float(ttl_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._max_workers = int(max_workers)
        self._checker = checker
        self._command_builder = command_builder
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._probe_slots = threading.Semaphore(self._max_workers)
        self._generation = 0
        self._cache: dict[Fingerprint, _CacheEntry] = {}
        self._flights: dict[tuple[int, Fingerprint], _Flight] = {}

    def invalidate(self) -> None:
        """Discard completed results without disrupting probes already running."""

        with self._lock:
            self._generation += 1
            self._cache.clear()

    def _result_ttl(self, result: BrowserHealthResult) -> float:
        if result.state in {
            BrowserHealthState.TIMED_OUT,
            BrowserHealthState.PROTOCOL_ERROR,
        }:
            return min(self._ttl_seconds, 60.0)
        return self._ttl_seconds

    @staticmethod
    def _status_for(
        target: str,
        statuses: Mapping[str, RuntimeComponentStatus],
    ) -> RuntimeComponentStatus:
        status = statuses.get(target)
        if status is not None:
            return status
        if target in _PLAYWRIGHT_TARGETS:
            status = statuses.get("playwright")
        elif target == "msedge":
            status = statuses.get("edge")
        if status is not None:
            return status
        return RuntimeComponentStatus(
            source="unavailable",
            path=None,
            usable=False,
            error=f"No runtime status was supplied for {target}.",
        )

    @staticmethod
    def _fingerprint(
        target: str,
        status: RuntimeComponentStatus,
    ) -> Fingerprint:
        runtime_path = _normalized_path(status.path)
        executable_path = runtime_path
        package_version: str | None = None
        revision: str | None = None
        extra_stats: tuple[object, ...] = ()

        if target in _PLAYWRIGHT_TARGETS:
            package_version = _playwright_package_version()
            revisions = playwright_browser_revisions()
            revision = revisions.get(target)
            if runtime_path is not None:
                executable, marker = _playwright_executable(
                    runtime_path,
                    target,
                    revision,
                )
                executable_path = _normalized_path(executable)
                extra_stats = (
                    *_stat_fingerprint(marker),
                    *_stat_fingerprint(runtime_path / "automadin-install.json"),
                )

        return (
            _FINGERPRINT_PROTOCOL,
            target,
            status.source,
            status.usable,
            status.version,
            str(runtime_path) if runtime_path is not None else None,
            *_stat_fingerprint(executable_path),
            *extra_stats,
            package_version,
            revision,
        )

    @staticmethod
    def _temp_parent(data_dir: str | Path) -> Path:
        root = Path(data_dir).expanduser().resolve()
        parent = root / "runtime" / "browser-health"
        parent.mkdir(parents=True, exist_ok=True)
        resolved = parent.resolve(strict=True)
        if not resolved.is_dir() or not resolved.is_relative_to(root):
            raise OSError("Browser health workspace escaped the application data directory.")
        return resolved

    def _builder(self) -> CommandBuilder:
        if self._command_builder is not None:
            return self._command_builder
        from automadin.runtime_components import browser_health_worker_command

        return browser_health_worker_command

    def _cancelled(self, target: str) -> BrowserHealthResult:
        return _result(
            target,
            BrowserHealthState.CANCELLED,
            "cancel",
            "The isolated headless launch check was cancelled.",
        )

    @staticmethod
    def _coordination_failure(
        target: str,
        message: str,
    ) -> BrowserHealthResult:
        return _result(
            target,
            BrowserHealthState.FAILED,
            "coordination",
            message,
        )

    def _probe(
        self,
        target: str,
        status: RuntimeComponentStatus,
        safe_temp_parent: Path,
        cancel: CancelHook,
    ) -> BrowserHealthResult:
        fingerprint = self._fingerprint(target, status)
        leader = False
        with self._lock:
            generation = self._generation
            now = self._monotonic()
            cached = self._cache.get(fingerprint)
            if cached is not None:
                if cached.expires_at > now:
                    return cached.result
                self._cache.pop(fingerprint, None)
            flight_key = (generation, fingerprint)
            flight = self._flights.get(flight_key)
            if flight is None:
                flight = _Flight(threading.Event(), generation)
                self._flights[flight_key] = flight
                leader = True
            else:
                flight.waiters += 1

        if not leader:
            while not flight.event.wait(0.02):
                try:
                    cancelled = cancel()
                except Exception as exc:
                    with self._lock:
                        flight.waiters -= 1
                    return self._coordination_failure(
                        target,
                        "The cancellation hook failed while waiting for a shared "
                        f"browser health check: {type(exc).__name__}.",
                    )
                if cancelled:
                    with self._lock:
                        flight.waiters -= 1
                    return self._cancelled(target)
            with self._lock:
                flight.waiters -= 1
                shared_result = flight.result
            if (
                shared_result is not None
                and shared_result.state is not BrowserHealthState.CANCELLED
            ):
                return shared_result
            if (
                shared_result is not None
                and shared_result.state is BrowserHealthState.CANCELLED
            ):
                try:
                    cancelled = cancel()
                except Exception as exc:
                    return self._coordination_failure(
                        target,
                        "The cancellation hook failed after a shared browser "
                        f"health check: {type(exc).__name__}.",
                    )
                if cancelled:
                    return self._cancelled(target)
                return self._probe(
                    target,
                    status,
                    safe_temp_parent,
                    cancel,
                )
            try:
                cancelled = cancel()
            except Exception as exc:
                return self._coordination_failure(
                    target,
                    "The cancellation hook failed after a shared browser health "
                    f"check: {type(exc).__name__}.",
                )
            if cancelled:
                return self._cancelled(target)
            return self._coordination_failure(
                target,
                "The shared browser health check did not produce a reusable result.",
            )

        def combined_cancel() -> bool:
            if not cancel():
                return False
            with self._lock:
                return flight.waiters <= 1

        acquired_slot = False
        try:
            while not self._probe_slots.acquire(timeout=0.02):
                try:
                    if combined_cancel():
                        result = self._cancelled(target)
                        break
                except Exception as exc:
                    result = self._coordination_failure(
                        target,
                        "The cancellation hook failed while waiting to start a "
                        f"browser health check: {type(exc).__name__}.",
                    )
                    break
            else:
                acquired_slot = True
            if not acquired_slot:
                return result
            result = self._checker(
                target,
                safe_temp_parent=safe_temp_parent,
                timeout=self._timeout_seconds,
                cancel=combined_cancel,
                command_builder=self._builder(),
                playwright_browsers_path=(
                    status.path if target in _PLAYWRIGHT_TARGETS else None
                ),
            )
        except Exception as exc:
            result = _result(
                target,
                BrowserHealthState.FAILED,
                "supervisor",
                f"The browser health checker failed: {type(exc).__name__}.",
            )
        finally:
            if acquired_slot:
                self._probe_slots.release()
            with self._lock:
                flight.result = result
                if (
                    "result" in locals()
                    and result.state is not BrowserHealthState.CANCELLED
                    and flight.generation == self._generation
                ):
                    self._cache[fingerprint] = _CacheEntry(
                        result=result,
                        expires_at=self._monotonic() + self._result_ttl(result),
                    )
                self._flights.pop(flight_key, None)
                flight.waiters -= 1
                flight.event.set()
        return result

    def check_many(
        self,
        statuses: Mapping[str, RuntimeComponentStatus],
        data_dir: str | Path,
        cancel: CancelHook = lambda: False,
    ) -> dict[str, BrowserHealthResult]:
        """Check all supported launch targets, with at most ``max_workers`` probes."""

        resolved = {
            target: self._status_for(target, statuses) for target in _TARGETS
        }
        results = {
            target: _result(
                target,
                BrowserHealthState.UNAVAILABLE,
                "runtime",
                status.error or "The browser runtime is not available.",
            )
            for target, status in resolved.items()
            if not status.usable
        }
        usable = [target for target in _TARGETS if target not in results]
        if not usable:
            return {target: results[target] for target in _TARGETS}

        try:
            safe_temp_parent = self._temp_parent(data_dir)
        except OSError as exc:
            for target in usable:
                results[target] = _result(
                    target,
                    BrowserHealthState.FAILED,
                    "workspace",
                    f"Could not prepare the browser health workspace: {type(exc).__name__}.",
                )
            return {target: results[target] for target in _TARGETS}

        with ThreadPoolExecutor(
            max_workers=min(self._max_workers, len(usable)),
            thread_name_prefix="browser-health",
        ) as executor:
            futures = {
                target: executor.submit(
                    self._probe,
                    target,
                    resolved[target],
                    safe_temp_parent,
                    cancel,
                )
                for target in usable
            }
            for target, future in futures.items():
                results[target] = future.result()

        return {target: results[target] for target in _TARGETS}


_DEFAULT_BROWSER_HEALTH_CACHE = BrowserHealthCache()


def default_browser_health_cache() -> BrowserHealthCache:
    """Return the process-wide cache shared by Setup and target selectors."""
    return _DEFAULT_BROWSER_HEALTH_CACHE
