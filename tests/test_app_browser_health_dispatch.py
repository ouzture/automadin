from __future__ import annotations

import sys
from types import ModuleType

import pytest

from automadin import app


def test_support_report_worker_dispatch_precedes_all_other_modes(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[list[str]] = []
    worker_module = ModuleType("automadin.support_report_worker")

    def worker_main(argv: list[str]) -> int:
        calls.append(argv)
        return 11

    worker_module.main = worker_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "automadin.support_report_worker", worker_module)
    monkeypatch.setattr(
        app,
        "_release_diagnostics_self_test_request",
        lambda: pytest.fail("support-report worker inspected diagnostics mode"),
    )
    monkeypatch.setattr(
        app,
        "_release_browser_health_self_test_parent",
        lambda: pytest.fail("support-report worker inspected release mode"),
    )
    monkeypatch.setattr(
        app,
        "_browser_health_worker_target",
        lambda: pytest.fail("support-report worker inspected browser worker mode"),
    )
    monkeypatch.setattr(
        app,
        "_resolve_data_dir",
        lambda: pytest.fail("support-report worker resolved the data directory"),
    )
    request_path = tmp_path / "request.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["Automadin.exe", "--support-report-worker", str(request_path)],
    )

    with pytest.raises(SystemExit) as raised:
        app.main()

    assert raised.value.code == 11
    assert calls == [[str(request_path)]]


def test_diagnostics_self_test_dispatch_precedes_other_modes(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []
    self_test_module = ModuleType("automadin.diagnostics_self_test")

    def self_test(parent: str, *, nonce: str | None = None) -> int:
        calls.extend((parent, nonce or ""))
        return 73

    self_test_module.run_release_diagnostics_self_test = self_test  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "automadin.diagnostics_self_test", self_test_module)
    monkeypatch.setattr(
        app,
        "_support_report_worker_request",
        lambda: None,
    )
    monkeypatch.setattr(
        app,
        "_release_browser_health_self_test_parent",
        lambda: pytest.fail("diagnostics self-test inspected browser self-test mode"),
    )
    monkeypatch.setattr(
        app,
        "_browser_health_worker_target",
        lambda: pytest.fail("diagnostics self-test inspected worker mode"),
    )
    monkeypatch.setattr(
        app,
        "_resolve_data_dir",
        lambda: pytest.fail("diagnostics self-test resolved the data directory"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "Automadin.exe",
            "--release-diagnostics-self-test",
            str(tmp_path),
            "--release-self-test-nonce",
            "0123456789abcdef0123456789abcdef",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        app.main()

    assert raised.value.code == 73
    assert calls == [str(tmp_path), "0123456789abcdef0123456789abcdef"]


def test_pushover_self_test_dispatch_precedes_normal_startup(monkeypatch) -> None:
    calls: list[bool] = []
    self_test_module = ModuleType("automadin.pushover_self_test")

    def self_test() -> int:
        calls.append(True)
        return 78

    self_test_module.run_release_pushover_credential_self_test = self_test  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "automadin.pushover_self_test", self_test_module)
    monkeypatch.setattr(app, "_support_report_worker_request", lambda: None)
    monkeypatch.setattr(
        app,
        "_release_diagnostics_self_test_request",
        lambda: pytest.fail("Pushover self-test inspected diagnostics mode"),
    )
    monkeypatch.setattr(
        app,
        "_resolve_data_dir",
        lambda: pytest.fail("Pushover self-test resolved the data directory"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["Automadin.exe", "--release-pushover-credential-self-test"],
    )

    with pytest.raises(SystemExit) as raised:
        app.main()

    assert raised.value.code == 78
    assert calls == [True]


def test_browser_health_worker_dispatch_precedes_normal_startup(monkeypatch) -> None:
    calls: list[list[str]] = []
    worker_module = ModuleType("automadin.browser_health_worker")

    def worker_main(argv: list[str]) -> int:
        calls.append(argv)
        return 7

    worker_module.main = worker_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "automadin.browser_health_worker", worker_module)
    monkeypatch.setattr(app, "_support_report_worker_request", lambda: None)
    monkeypatch.setattr(
        app,
        "_resolve_data_dir",
        lambda: pytest.fail("normal startup resolved the data directory"),
    )
    monkeypatch.setattr(
        app,
        "_runtime_manager_requested",
        lambda: pytest.fail("normal startup inspected runtime-manager mode"),
    )
    monkeypatch.setattr(sys, "argv", ["Automadin.exe", "--browser-health-worker", "webkit"])

    with pytest.raises(SystemExit) as raised:
        app.main()

    assert raised.value.code == 7
    assert calls == [["webkit"]]


def test_release_self_test_dispatch_precedes_worker_and_normal_startup(
    monkeypatch, tmp_path
) -> None:
    calls: list[str] = []
    self_test_module = ModuleType("automadin.release_self_test")

    def self_test(parent: str) -> int:
        calls.append(parent)
        return 67

    self_test_module.run_release_browser_health_self_test = self_test  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "automadin.release_self_test", self_test_module)
    monkeypatch.setattr(app, "_support_report_worker_request", lambda: None)
    monkeypatch.setattr(
        app,
        "_browser_health_worker_target",
        lambda: pytest.fail("release self-test inspected worker mode"),
    )
    monkeypatch.setattr(
        app,
        "_resolve_data_dir",
        lambda: pytest.fail("release self-test resolved the data directory"),
    )
    monkeypatch.setattr(
        app,
        "_runtime_manager_requested",
        lambda: pytest.fail("release self-test inspected runtime-manager mode"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "Automadin.exe",
            "--release-browser-health-self-test",
            str(tmp_path),
            "--browser-health-worker",
            "chromium",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        app.main()

    assert raised.value.code == 67
    assert calls == [str(tmp_path)]
