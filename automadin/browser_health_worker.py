"""Child-process worker for an ephemeral Playwright browser launch check."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from automadin.services.browser_health import (
    CONTAINMENT_READY_ENV,
    LAUNCH_TIMEOUT_ENV,
    MAX_PROTOCOL_BYTES,
    RESULT_PATH_ENV,
    SUPPORTED_BROWSER_HEALTH_TARGETS,
)

_UNAVAILABLE_MARKERS = (
    "executable doesn't exist",
    "executable does not exist",
    "browser executable",
    "playwright install",
    "not found",
)


def _message(state: str) -> str:
    return {
        "healthy": "The isolated headless launch check completed.",
        "unavailable": "The isolated headless browser executable was unavailable.",
        "failed": "The isolated headless launch check failed.",
        "cleanup_failed": "The isolated headless launch resources could not be fully closed.",
    }[state]


def _state_for_exception(error: BaseException) -> str:
    text = str(error).casefold()
    return "unavailable" if any(marker in text for marker in _UNAVAILABLE_MARKERS) else "failed"


def _launch_timeout_ms() -> int:
    try:
        timeout = int(os.environ.get(LAUNCH_TIMEOUT_ENV, "5000"))
    except ValueError:
        return 5000
    return min(10_000, max(500, timeout))


def run_worker_check(
    target: str,
    *,
    playwright_factory: Callable[[], Any] | None = None,
) -> dict[str, str]:
    """Launch one non-persistent, headless browser and close every resource."""

    os.environ.pop("AUTOMADIN_PROFILES_DIR", None)
    if target not in SUPPORTED_BROWSER_HEALTH_TARGETS:
        return {
            "target": target,
            "state": "unavailable",
            "stage": "validation",
            "message": "This isolated headless launch target is not supported.",
        }

    if playwright_factory is None:
        from playwright.sync_api import sync_playwright

        playwright_factory = sync_playwright

    playwright = browser = context = page = None
    state = "healthy"
    stage = "playwright_start"
    try:
        playwright = playwright_factory().start()
        browser_type = getattr(
            playwright,
            target if target in {"chromium", "firefox", "webkit"} else "chromium",
        )
        stage = "launch"
        timeout_ms = _launch_timeout_ms()
        launch_options: dict[str, Any] = {
            "headless": True,
            "timeout": timeout_ms,
        }
        if target in {"chrome", "msedge"}:
            launch_options["channel"] = target
        browser = browser_type.launch(**launch_options)
        stage = "context"
        context = browser.new_context()
        stage = "page"
        page = context.new_page()
        page.goto("about:blank", timeout=timeout_ms)
        stage = "complete"
    except Exception as exc:
        state = _state_for_exception(exc)
    finally:
        cleanup_failed = False
        for resource in (page, context, browser, playwright):
            if resource is None:
                continue
            try:
                resource.close() if resource is not playwright else resource.stop()
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            state = "cleanup_failed"
            stage = "cleanup"

    return {
        "target": target,
        "state": state,
        "stage": stage,
        "message": _message(state),
    }


def _bounded_json(record: dict[str, str]) -> bytes:
    encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROTOCOL_BYTES:
        raise ValueError("Worker record exceeded the protocol limit.")
    return encoded + b"\n"


def _write_record(record: dict[str, str]) -> None:
    raw_path = os.environ.get(RESULT_PATH_ENV)
    if not raw_path:
        raise RuntimeError("Browser health result path was not supplied.")
    workspace = Path.cwd().resolve()
    result_path = Path(raw_path).expanduser().resolve()
    if result_path.parent != workspace or result_path.name != "result.json":
        raise RuntimeError("Browser health result path escaped its workspace.")
    descriptor = os.open(
        result_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_bounded_json(record))
        stream.flush()
        os.fsync(stream.fileno())


def _wait_for_containment() -> None:
    raw_path = os.environ.get(CONTAINMENT_READY_ENV)
    if not raw_path:
        raise RuntimeError("Browser health containment marker was not supplied.")
    workspace = Path.cwd().resolve()
    marker = Path(raw_path).expanduser().resolve()
    if marker.parent != workspace or marker.name != "contained.ready":
        raise RuntimeError("Browser health containment marker escaped its workspace.")
    deadline = time.monotonic() + 2
    while not marker.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("Browser health worker containment was not confirmed.")
        time.sleep(0.01)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    target = args[0] if len(args) == 1 else ""
    try:
        _wait_for_containment()
        record = run_worker_check(target)
    except Exception:
        record = {
            "target": target,
            "state": "failed",
            "stage": "worker",
            "message": "The isolated headless launch worker failed.",
        }
    try:
        _write_record(record)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
