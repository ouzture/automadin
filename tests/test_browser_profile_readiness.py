from pathlib import Path

import pytest

from automadin.runtime import RuntimeComponentStatus
from automadin.services.browser_profile_readiness import (
    BrowserProfileReadinessState,
    assess_browser_profile_readiness,
    assess_browser_profile_readiness_many,
)


@pytest.mark.parametrize("engine", ["chromium", "firefox"])
def test_managed_profile_engines_use_managed_playwright_runtime(
    engine: str,
    tmp_path: Path,
) -> None:
    runtime_path = tmp_path / "browsers"
    status = RuntimeComponentStatus("managed", runtime_path, True)
    calls: list[Path] = []

    result = assess_browser_profile_readiness(
        engine,
        tmp_path,
        runtime_status_provider=lambda data_dir: calls.append(Path(data_dir)) or status,
        system_channel_status_provider=lambda channel: pytest.fail(
            f"system channel provider called for {channel}"
        ),
    )

    assert calls == [tmp_path]
    assert result.engine == engine
    assert result.state is BrowserProfileReadinessState.AVAILABLE
    assert result.available
    assert not result.launch_blocking
    assert result.path == runtime_path
    assert result.source == "managed"
    assert "managed Playwright runtime" in result.message


@pytest.mark.parametrize(
    ("engine", "expected_channel", "display_name"),
    [
        ("chrome", "chrome", "Google Chrome"),
        ("msedge", "msedge", "Microsoft Edge"),
    ],
)
def test_vendor_profile_engines_use_their_exact_system_channel(
    engine: str,
    expected_channel: str,
    display_name: str,
    tmp_path: Path,
) -> None:
    executable = tmp_path / f"{expected_channel}.exe"
    calls: list[str] = []

    result = assess_browser_profile_readiness(
        engine,
        tmp_path,
        runtime_status_provider=lambda data_dir: pytest.fail(
            f"managed runtime provider called for {data_dir}"
        ),
        system_channel_status_provider=lambda channel: (
            calls.append(channel) or RuntimeComponentStatus("system", executable, True)
        ),
    )

    assert calls == [expected_channel]
    assert result.engine == engine
    assert result.state is BrowserProfileReadinessState.AVAILABLE
    assert not result.launch_blocking
    assert result.path == executable
    assert result.source == "system"
    assert display_name in result.message


@pytest.mark.parametrize(
    ("engine", "display_name"),
    [
        ("chromium", "Chromium"),
        ("firefox", "Firefox"),
        ("chrome", "Google Chrome"),
        ("msedge", "Microsoft Edge"),
    ],
)
def test_proven_absence_is_launch_blocking_but_profile_management_stays_available(
    engine: str,
    display_name: str,
    tmp_path: Path,
) -> None:
    provider_error = "private provider detail must not be shown"
    unavailable = RuntimeComponentStatus(
        "unavailable",
        None,
        False,
        error=provider_error,
    )

    result = assess_browser_profile_readiness(
        engine,
        tmp_path,
        runtime_status_provider=lambda _data_dir: unavailable,
        system_channel_status_provider=lambda _channel: unavailable,
    )

    assert result.state is BrowserProfileReadinessState.UNAVAILABLE
    assert result.launch_blocking
    assert not result.available
    assert result.path is None
    assert result.source == "unavailable"
    assert display_name in result.message
    assert provider_error not in result.message
    assert "still" in result.message
    if engine in {"chromium", "firefox"}:
        assert "Tools > Runtime Components" in result.message
    else:
        assert f"Install {display_name}" in result.message


@pytest.mark.parametrize("engine", ["chromium", "firefox", "chrome", "msedge"])
def test_provider_failure_is_unknown_nonblocking_and_does_not_leak_details(
    engine: str,
    tmp_path: Path,
) -> None:
    private_detail = r"C:\Users\person\secret-profile"

    def fail(_argument) -> RuntimeComponentStatus:
        raise OSError(private_detail)

    result = assess_browser_profile_readiness(
        engine,
        tmp_path,
        runtime_status_provider=fail,
        system_channel_status_provider=fail,
    )

    assert result.engine == engine
    assert result.state is BrowserProfileReadinessState.UNKNOWN
    assert not result.launch_blocking
    assert not result.available
    assert result.path is None
    assert result.source == "unknown"
    assert "Could not check" in result.message
    assert "checks the runtime again before launch" in result.message
    assert private_detail not in result.message


def test_assessment_is_fresh_and_observes_runtime_disappearance(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "chrome.exe"
    statuses = iter(
        (
            RuntimeComponentStatus("system", executable, True),
            RuntimeComponentStatus("unavailable", None, False),
        )
    )
    calls: list[str] = []

    def status_provider(channel: str) -> RuntimeComponentStatus:
        calls.append(channel)
        return next(statuses)

    first = assess_browser_profile_readiness(
        "chrome",
        tmp_path,
        system_channel_status_provider=status_provider,
    )
    second = assess_browser_profile_readiness(
        "chrome",
        tmp_path,
        system_channel_status_provider=status_provider,
    )

    assert calls == ["chrome", "chrome"]
    assert first.state is BrowserProfileReadinessState.AVAILABLE
    assert second.state is BrowserProfileReadinessState.UNAVAILABLE
    assert second.launch_blocking


def test_engine_is_canonicalized_but_unsupported_values_are_rejected(
    tmp_path: Path,
) -> None:
    available = RuntimeComponentStatus("managed", tmp_path / "browsers", True)

    result = assess_browser_profile_readiness(
        "  FIREFOX ",
        tmp_path,
        runtime_status_provider=lambda _data_dir: available,
    )

    assert result.engine == "firefox"
    with pytest.raises(ValueError, match="Unsupported browser profile engine"):
        assess_browser_profile_readiness("webkit", tmp_path)
    with pytest.raises(TypeError, match="must be text"):
        assess_browser_profile_readiness(None, tmp_path)  # type: ignore[arg-type]


def test_many_assessment_deduplicates_engines_and_scans_managed_runtime_once(
    tmp_path: Path,
) -> None:
    managed_calls: list[Path] = []
    channel_calls: list[str] = []
    managed = RuntimeComponentStatus("managed", tmp_path / "browsers", True)
    system = RuntimeComponentStatus("system", tmp_path / "browser.exe", True)

    results = assess_browser_profile_readiness_many(
        ("chromium", "firefox", "chrome", "msedge", "chromium"),
        tmp_path,
        runtime_status_provider=lambda data_dir: managed_calls.append(Path(data_dir)) or managed,
        system_channel_status_provider=lambda channel: channel_calls.append(channel) or system,
    )

    assert tuple(results) == ("chromium", "firefox", "chrome", "msedge")
    assert managed_calls == [tmp_path]
    assert channel_calls == ["chrome", "msedge"]
    assert all(result.available for result in results.values())


def test_many_assessment_contains_one_managed_provider_failure_for_both_engines(
    tmp_path: Path,
) -> None:
    calls: list[Path] = []

    def fail(data_dir: str | Path) -> RuntimeComponentStatus:
        calls.append(Path(data_dir))
        raise OSError("private managed runtime detail")

    results = assess_browser_profile_readiness_many(
        ("chromium", "firefox"),
        tmp_path,
        runtime_status_provider=fail,
    )

    assert calls == [tmp_path]
    assert all(result.state is BrowserProfileReadinessState.UNKNOWN for result in results.values())
    assert all(
        "private managed runtime detail" not in result.message for result in results.values()
    )
