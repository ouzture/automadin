from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from automadin import release_self_test
from automadin.services.browser_health import BrowserHealthResult, BrowserHealthState


def _result(state: BrowserHealthState, stage: str) -> BrowserHealthResult:
    return BrowserHealthResult(
        target="chromium",
        state=state,
        stage=stage,
        checked_at=datetime.now(UTC),
        duration=0.01,
        message="test result",
    )


def test_self_test_uses_real_contract_and_removes_all_temporary_files(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[object, ...]] = []

    def command_builder(target: str) -> list[str]:
        calls.append(("command", target))
        return ["Automadin.exe", "--browser-health-worker", target]

    def checker(target: str, **kwargs) -> BrowserHealthResult:
        calls.append(
            (
                "check",
                target,
                kwargs["safe_temp_parent"],
                kwargs["playwright_browsers_path"],
                kwargs["command_builder"],
            )
        )
        assert Path(kwargs["safe_temp_parent"]).is_dir()
        assert list(Path(kwargs["playwright_browsers_path"]).iterdir()) == []
        assert kwargs["command_builder"]("chromium") == [
            "Automadin.exe",
            "--browser-health-worker",
            "chromium",
        ]
        return _result(BrowserHealthState.UNAVAILABLE, "launch")

    monkeypatch.setattr(release_self_test, "browser_health_worker_command", command_builder)
    monkeypatch.setattr(release_self_test, "check_browser_launch", checker)
    before = set(tmp_path.iterdir())

    exit_code = release_self_test.run_release_browser_health_self_test(tmp_path)

    assert exit_code == release_self_test.RELEASE_SELF_TEST_OK
    assert set(tmp_path.iterdir()) == before
    assert calls[0][0:2] == ("check", "chromium")
    assert calls[1] == ("command", "chromium")


def test_self_test_rejects_nonexistent_parent_without_calling_checker(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        release_self_test,
        "check_browser_launch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("checker called")),
    )

    exit_code = release_self_test.run_release_browser_health_self_test(
        tmp_path / "missing"
    )

    assert exit_code == release_self_test.RELEASE_SELF_TEST_INVALID_PARENT
    assert list(tmp_path.iterdir()) == []


def test_self_test_rejects_any_result_other_than_unavailable_at_launch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        release_self_test,
        "check_browser_launch",
        lambda *args, **kwargs: _result(BrowserHealthState.HEALTHY, "complete"),
    )

    exit_code = release_self_test.run_release_browser_health_self_test(tmp_path)

    assert exit_code == release_self_test.RELEASE_SELF_TEST_UNEXPECTED_RESULT
    assert list(tmp_path.iterdir()) == []


def test_self_test_reports_checker_exception_and_removes_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    def checker(*args, **kwargs) -> BrowserHealthResult:
        raise RuntimeError("checker failed")

    monkeypatch.setattr(release_self_test, "check_browser_launch", checker)

    exit_code = release_self_test.run_release_browser_health_self_test(tmp_path)

    assert exit_code == release_self_test.RELEASE_SELF_TEST_CHECK_FAILED
    assert list(tmp_path.iterdir()) == []


def test_self_test_reports_checker_residue_but_still_removes_it(
    tmp_path: Path, monkeypatch
) -> None:
    def checker(*args, **kwargs) -> BrowserHealthResult:
        (Path(kwargs["safe_temp_parent"]) / "leaked").write_text(
            "residue", encoding="utf-8"
        )
        return _result(BrowserHealthState.UNAVAILABLE, "launch")

    monkeypatch.setattr(release_self_test, "check_browser_launch", checker)

    exit_code = release_self_test.run_release_browser_health_self_test(tmp_path)

    assert exit_code == release_self_test.RELEASE_SELF_TEST_RESIDUE_DETECTED
    assert list(tmp_path.iterdir()) == []
