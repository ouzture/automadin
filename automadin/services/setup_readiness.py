"""Read-only setup readiness assessment for Automadin.

The collector in this module is deliberately Qt-free.  It performs no runtime
configuration, device discovery, database migration, or profile mutation, so a
UI can safely run it on a retained background worker and discard stale results.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from automadin.build_info import packaged_app_version
from automadin.runtime import (
    RuntimeComponentStatus,
    adb_runtime_status,
    browser_runtime_status,
    probe_adb,
    system_browser_channel_status,
)
from automadin.services.browser_health import BrowserHealthResult
from automadin.services.ocr import TesseractStatus, inspect_tesseract, load_tesseract_configuration


class SetupReadinessCancelled(RuntimeError):
    """Raised when a caller cooperatively cancels readiness collection."""


@dataclass(frozen=True, slots=True)
class SetupReadinessSnapshot:
    """Immutable result of one independent readiness assessment."""

    checked_at: datetime
    app_version: str
    data_dir: Path
    data_writable: bool | None
    data_error: str | None
    schema_version: int | None
    schema_error: str | None
    flow_count: int | None
    run_config_count: int | None
    workspace_error: str | None
    profile_total: int | None
    profile_ready: int | None
    profile_error: str | None
    playwright: RuntimeComponentStatus
    chrome: RuntimeComponentStatus
    edge: RuntimeComponentStatus
    adb: RuntimeComponentStatus
    ocr: TesseractStatus = field(
        default_factory=lambda: TesseractStatus(
            None,
            False,
            error="Tesseract is not configured.",
        )
    )
    browser_health: tuple[tuple[str, BrowserHealthResult], ...] = ()
    browser_health_error: str | None = None


def _raise_if_cancelled(cancel: Callable[[], bool]) -> None:
    if cancel():
        raise SetupReadinessCancelled("Setup readiness collection was cancelled.")


def _error_text(error: BaseException) -> str:
    return str(error).strip() or type(error).__name__


def _joined_errors(errors: list[str]) -> str | None:
    return "; ".join(errors) if errors else None


def _app_version() -> str:
    return packaged_app_version("Development build")


def _probe_data_directory(data_dir: Path) -> tuple[bool, str | None]:
    """Write, sync, and remove one uniquely named file in ``data_dir``."""

    probe_path: Path | None = None
    writable = False
    error: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".automadin-write-probe-",
            suffix=".tmp",
            dir=data_dir,
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            probe.write(b"automadin setup readiness\n")
            probe.flush()
            os.fsync(probe.fileno())
        writable = True
    except Exception as exc:
        error = _error_text(exc)
    finally:
        if probe_path is not None and os.path.lexists(probe_path):
            try:
                probe_path.unlink()
            except Exception as exc:
                writable = False
                cleanup_error = f"Temporary write probe cleanup failed: {_error_text(exc)}"
                error = f"{error}; {cleanup_error}" if error else cleanup_error
    return writable, error


def _runtime_error(error: BaseException) -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        source="error",
        path=None,
        usable=False,
        error=_error_text(error),
    )


def _configured_adb_status(configured_adb_path: str | Path) -> RuntimeComponentStatus:
    probe = probe_adb(Path(str(configured_adb_path).strip()).expanduser())
    return RuntimeComponentStatus(
        source="configured",
        path=probe.path,
        usable=probe.usable,
        version=probe.version,
        error=probe.error,
        ignored_override=probe.ignored_override,
    )


def collect_setup_readiness(
    database,
    data_dir: str | Path,
    configured_adb_path: str | Path | None = None,
    cancel: Callable[[], bool] = lambda: False,
    browser_health_cache=None,
) -> SetupReadinessSnapshot:
    """Collect independent setup signals without changing application state.

    Each assessment family fails independently.  Counts use ``None`` for an
    unknown result; errors are never converted into a misleading zero count.
    Callers should run this function outside the GUI thread because ADB probing
    launches a bounded subprocess and filesystem checks may still be slow.
    """

    _raise_if_cancelled(cancel)
    app_version = _app_version()
    root = Path(data_dir).expanduser().absolute()

    _raise_if_cancelled(cancel)
    data_writable, data_error = _probe_data_directory(root)

    _raise_if_cancelled(cancel)
    schema_version: int | None = None
    schema_error: str | None = None
    try:
        schema_version = int(database.schema_version)
    except Exception as exc:
        schema_error = _error_text(exc)

    flow_count: int | None = None
    run_config_count: int | None = None
    workspace_errors: list[str] = []

    _raise_if_cancelled(cancel)
    try:
        flow_count = len(database.list_flows())
    except Exception as exc:
        workspace_errors.append(f"Flows: {_error_text(exc)}")

    _raise_if_cancelled(cancel)
    try:
        run_config_count = len(database.list_run_configs())
    except Exception as exc:
        workspace_errors.append(f"Run configurations: {_error_text(exc)}")

    profile_total: int | None = None
    profile_ready: int | None = None
    profile_errors: list[str] = []

    _raise_if_cancelled(cancel)
    try:
        profile_total = len(database.list_profile_records())
    except Exception as exc:
        profile_errors.append(f"Stored profiles: {_error_text(exc)}")

    _raise_if_cancelled(cancel)
    try:
        profile_ready = len(database.profile_engine_specs())
    except Exception as exc:
        profile_errors.append(f"Ready profiles: {_error_text(exc)}")

    _raise_if_cancelled(cancel)
    try:
        playwright = browser_runtime_status(root)
    except Exception as exc:
        playwright = _runtime_error(exc)

    _raise_if_cancelled(cancel)
    try:
        chrome = system_browser_channel_status("chrome")
    except Exception as exc:
        chrome = _runtime_error(exc)

    _raise_if_cancelled(cancel)
    try:
        edge = system_browser_channel_status("msedge")
    except Exception as exc:
        edge = _runtime_error(exc)

    browser_health: tuple[tuple[str, BrowserHealthResult], ...] = ()
    browser_health_error: str | None = None
    if browser_health_cache is not None:
        _raise_if_cancelled(cancel)
        try:
            health_results = browser_health_cache.check_many(
                {
                    "playwright": playwright,
                    "chrome": chrome,
                    "msedge": edge,
                },
                root,
                cancel=cancel,
            )
            browser_health = tuple(health_results.items())
        except Exception as exc:
            browser_health_error = _error_text(exc)

    _raise_if_cancelled(cancel)
    try:
        configured = (
            str(configured_adb_path).strip()
            if configured_adb_path is not None
            else ""
        )
        adb = (
            _configured_adb_status(configured)
            if configured
            else adb_runtime_status(root)
        )
    except Exception as exc:
        adb = _runtime_error(exc)

    _raise_if_cancelled(cancel)
    try:
        ocr = inspect_tesseract(load_tesseract_configuration(database))
    except Exception as exc:
        ocr = TesseractStatus(
            None,
            False,
            error=_error_text(exc),
            source="error",
        )

    _raise_if_cancelled(cancel)
    return SetupReadinessSnapshot(
        checked_at=datetime.now(UTC),
        app_version=app_version,
        data_dir=root,
        data_writable=data_writable,
        data_error=data_error,
        schema_version=schema_version,
        schema_error=schema_error,
        flow_count=flow_count,
        run_config_count=run_config_count,
        workspace_error=_joined_errors(workspace_errors),
        profile_total=profile_total,
        profile_ready=profile_ready,
        profile_error=_joined_errors(profile_errors),
        playwright=playwright,
        chrome=chrome,
        edge=edge,
        adb=adb,
        ocr=ocr,
        browser_health=browser_health,
        browser_health_error=browser_health_error,
    )
