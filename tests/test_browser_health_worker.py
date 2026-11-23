from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from automadin import browser_health_worker
from automadin.browser_health_worker import run_worker_check
from automadin.services.browser_health import (
    CONTAINMENT_READY_ENV,
    RESULT_PATH_ENV,
    SUPPORTED_BROWSER_HEALTH_TARGETS,
)


@dataclass
class FakeResource:
    events: list[str]
    name: str
    fail_close: bool = False

    def close(self) -> None:
        self.events.append(f"{self.name}.close")
        if self.fail_close:
            raise RuntimeError("close failed")


@dataclass
class FakePage(FakeResource):
    def goto(self, url: str, **kwargs: Any) -> None:
        self.events.append(f"page.goto:{url}")
        assert kwargs == {"timeout": 5000}


@dataclass
class FakeContext(FakeResource):
    page: FakePage | None = None

    def new_page(self) -> FakePage:
        self.events.append("context.new_page")
        assert self.page is not None
        return self.page


@dataclass
class FakeBrowser(FakeResource):
    context: FakeContext | None = None

    def new_context(self) -> FakeContext:
        self.events.append("browser.new_context")
        assert self.context is not None
        return self.context


@dataclass
class FakeBrowserType:
    events: list[str]
    browser: FakeBrowser
    launch_calls: list[dict[str, Any]] = field(default_factory=list)

    def launch(self, **kwargs: Any) -> FakeBrowser:
        self.events.append("browser_type.launch")
        self.launch_calls.append(kwargs)
        return self.browser


@dataclass
class FakePlaywright:
    events: list[str]
    chromium: FakeBrowserType
    firefox: FakeBrowserType
    webkit: FakeBrowserType

    def stop(self) -> None:
        self.events.append("playwright.stop")


@dataclass
class FakeStarter:
    events: list[str]
    playwright: FakePlaywright

    def start(self) -> FakePlaywright:
        self.events.append("playwright.start")
        return self.playwright


def _fake_stack(*, fail_page_close: bool = False):
    events: list[str] = []
    page = FakePage(events, "page", fail_close=fail_page_close)
    context = FakeContext(events, "context", page=page)
    browser = FakeBrowser(events, "browser", context=context)
    chromium = FakeBrowserType(events, browser)
    firefox = FakeBrowserType(events, browser)
    webkit = FakeBrowserType(events, browser)
    playwright = FakePlaywright(events, chromium, firefox, webkit)
    starter = FakeStarter(events, playwright)
    return events, starter, {"chromium": chromium, "firefox": firefox, "webkit": webkit}


@pytest.mark.parametrize("target", sorted(SUPPORTED_BROWSER_HEALTH_TARGETS))
def test_worker_uses_only_ephemeral_headless_launch(target: str, monkeypatch) -> None:
    events, starter, types = _fake_stack()
    monkeypatch.setenv("AUTOMADIN_PROFILES_DIR", "forbidden")

    result = run_worker_check(target, playwright_factory=lambda: starter)

    assert result["state"] == "healthy"
    assert "AUTOMADIN_PROFILES_DIR" not in os.environ
    browser_type = types[target if target in types else "chromium"]
    expected = {"headless": True, "timeout": 5000}
    if target in {"chrome", "msedge"}:
        expected["channel"] = target
    assert browser_type.launch_calls == [expected]
    assert events == [
        "playwright.start",
        "browser_type.launch",
        "browser.new_context",
        "context.new_page",
        "page.goto:about:blank",
        "page.close",
        "context.close",
        "browser.close",
        "playwright.stop",
    ]


def test_worker_cleanup_failure_overrides_success() -> None:
    events, starter, _types = _fake_stack(fail_page_close=True)

    result = run_worker_check("chromium", playwright_factory=lambda: starter)

    assert result["state"] == "cleanup_failed"
    assert result["stage"] == "cleanup"
    assert events[-4:] == [
        "page.close",
        "context.close",
        "browser.close",
        "playwright.stop",
    ]


def test_worker_classifies_missing_executable_without_claiming_broader_readiness() -> None:
    events, starter, types = _fake_stack()

    def missing(**_kwargs: Any) -> None:
        raise RuntimeError("Executable doesn't exist")

    types["chromium"].launch = missing  # type: ignore[method-assign]
    result = run_worker_check("chromium", playwright_factory=lambda: starter)

    assert result == {
        "target": "chromium",
        "state": "unavailable",
        "stage": "launch",
        "message": "The isolated headless browser executable was unavailable.",
    }
    assert "playwright.stop" in events
    forbidden_claims = ("sign-in", "signed in", "network", "profile", "headed")
    assert not any(claim in result["message"].casefold() for claim in forbidden_claims)


def test_worker_main_uses_private_result_file_without_console(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_path = tmp_path / "result.json"
    marker = tmp_path / "contained.ready"
    marker.write_text("contained\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(RESULT_PATH_ENV, str(result_path))
    monkeypatch.setenv(CONTAINMENT_READY_ENV, str(marker))
    monkeypatch.setattr(browser_health_worker.sys, "stdout", None)
    monkeypatch.setattr(
        browser_health_worker,
        "run_worker_check",
        lambda target: {
            "target": target,
            "state": "healthy",
            "stage": "complete",
            "message": "The isolated headless launch check completed.",
        },
    )

    assert browser_health_worker.main(["webkit"]) == 0
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "target": "webkit",
        "state": "healthy",
        "stage": "complete",
        "message": "The isolated headless launch check completed.",
    }
