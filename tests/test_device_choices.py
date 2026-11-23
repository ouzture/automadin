from pathlib import Path
from datetime import UTC, datetime

import pytest

from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
    WindowsDevice,
)
from automadin.runtime import RuntimeComponentStatus
from automadin.services.device_choices import (
    DeviceChoice,
    attach_browser_health,
    assess_device_choices,
    browser_health_statuses,
    browser_health_target_for_device,
    runtime_family_for_device,
)
from automadin.services.browser_health import BrowserHealthResult, BrowserHealthState


def _browser(
    engine: BrowserEngine,
    *,
    profile_id: str | None = None,
    screen_mode: ScreenMode = ScreenMode.DESKTOP,
) -> BrowserDevice:
    return BrowserDevice(
        BrowserDeviceConfig(
            engine=engine,
            profile_id=profile_id,
            screen_mode=screen_mode,
        )
    )


@pytest.mark.parametrize(
    ("engine", "family"),
    [
        (BrowserEngine.CHROMIUM, "playwright"),
        (BrowserEngine.FIREFOX, "playwright"),
        (BrowserEngine.WEBKIT, "playwright"),
        (BrowserEngine.CHROME, "chrome"),
        (BrowserEngine.MSEDGE, "msedge"),
    ],
)
def test_runtime_family_for_browser_engine(
    engine: BrowserEngine,
    family: str,
) -> None:
    assert runtime_family_for_device(_browser(engine)) == family


def test_non_browser_devices_have_no_selector_runtime_family() -> None:
    assert runtime_family_for_device(AndroidDevice("serial")) is None
    assert runtime_family_for_device(WindowsDevice(12, "Window")) is None


def test_assessment_coalesces_browser_probes_by_runtime_family(
    tmp_path: Path,
) -> None:
    devices = [
        _browser(BrowserEngine.CHROMIUM),
        _browser(BrowserEngine.FIREFOX),
        _browser(BrowserEngine.WEBKIT),
        _browser(BrowserEngine.CHROME),
        _browser(BrowserEngine.CHROME, screen_mode=ScreenMode.MOBILE_ANDROID),
        _browser(BrowserEngine.MSEDGE),
    ]
    statuses = {
        "playwright": RuntimeComponentStatus("managed", tmp_path / "pw", True),
        "chrome": RuntimeComponentStatus("system", tmp_path / "chrome.exe", True),
        "msedge": RuntimeComponentStatus("unavailable", None, False),
    }
    calls: list[BrowserDevice] = []

    def provider(device, data_dir):
        assert data_dir == tmp_path
        assert isinstance(device, BrowserDevice)
        calls.append(device)
        family = runtime_family_for_device(device)
        assert family is not None
        return statuses[family]

    choices = assess_device_choices(devices, tmp_path, provider)

    assert calls == [devices[0], devices[3], devices[5]]
    assert [choice.runtime_status for choice in choices] == [
        statuses["playwright"],
        statuses["playwright"],
        statuses["playwright"],
        statuses["chrome"],
        statuses["chrome"],
        statuses["msedge"],
    ]
    assert [choice.selectable for choice in choices] == [
        True,
        True,
        True,
        True,
        True,
        False,
    ]


def test_provider_failure_is_coalesced_and_remains_selectable(
    tmp_path: Path,
) -> None:
    devices = [
        _browser(BrowserEngine.CHROMIUM),
        _browser(BrowserEngine.FIREFOX),
    ]
    calls = 0

    def failing_provider(device, data_dir):
        nonlocal calls
        calls += 1
        raise RuntimeError("probe exploded")

    choices = assess_device_choices(devices, tmp_path, failing_provider)

    assert calls == 1
    assert [choice.runtime_status for choice in choices] == [None, None]
    assert [choice.assessment_error for choice in choices] == [
        "probe exploded",
        "probe exploded",
    ]
    assert all(choice.selectable for choice in choices)


def test_empty_provider_error_uses_exception_type(tmp_path: Path) -> None:
    def failing_provider(device, data_dir):
        raise RuntimeError

    choice = assess_device_choices(
        [_browser(BrowserEngine.CHROME)],
        tmp_path,
        failing_provider,
    )[0]

    assert choice.assessment_error == "RuntimeError"
    assert choice.selectable


def test_order_identity_and_profile_ids_are_preserved(tmp_path: Path) -> None:
    first_profile_id = "31132eb4-1dec-4fab-9728-2e02901e3996"
    second_profile_id = "f687881f-d676-4303-8d1e-19c83fb611b3"
    devices = [
        WindowsDevice(22, "Editor"),
        _browser(BrowserEngine.CHROMIUM, profile_id=first_profile_id),
        AndroidDevice("serial-1", model="Phone"),
        _browser(BrowserEngine.CHROMIUM, profile_id=second_profile_id),
    ]
    status = RuntimeComponentStatus("managed", tmp_path / "pw", True)
    calls = 0

    def provider(device, data_dir):
        nonlocal calls
        calls += 1
        return status

    choices = assess_device_choices(devices, tmp_path, provider)

    assert calls == 1
    assert len(choices) == len(devices)
    assert all(choice.device is device for choice, device in zip(choices, devices))
    assert choices[1].device.id.endswith(first_profile_id)
    assert choices[3].device.id.endswith(second_profile_id)
    assert choices[0].runtime_status is None
    assert choices[2].runtime_status is None


def test_device_choice_is_immutable_and_only_known_unusable_is_disabled(
    tmp_path: Path,
) -> None:
    device = _browser(BrowserEngine.CHROME)
    unavailable = DeviceChoice(
        device,
        RuntimeComponentStatus("unavailable", None, False),
    )
    unknown = DeviceChoice(device, None, assessment_error="unknown")

    assert not unavailable.selectable
    assert unknown.selectable
    with pytest.raises(AttributeError):
        unknown.assessment_error = None  # type: ignore[misc]


def _health(
    target: str,
    state: BrowserHealthState,
    stage: str,
) -> BrowserHealthResult:
    return BrowserHealthResult(
        target,
        state,
        stage,
        datetime.now(UTC),
        0.01,
        "test result",
    )


@pytest.mark.parametrize("engine", list(BrowserEngine))
def test_exact_health_target_matches_browser_engine(engine: BrowserEngine) -> None:
    assert browser_health_target_for_device(_browser(engine)) == engine.value


def test_health_attachment_preserves_devices_and_blocks_only_cleanup_failure(
    tmp_path: Path,
) -> None:
    chromium = _browser(BrowserEngine.CHROMIUM)
    chrome = _browser(BrowserEngine.CHROME)
    edge = _browser(BrowserEngine.MSEDGE)
    firefox = _browser(BrowserEngine.FIREFOX)
    detected = RuntimeComponentStatus("managed", tmp_path / "browsers", True)
    choices = [
        DeviceChoice(chromium, detected),
        DeviceChoice(chrome, detected),
        DeviceChoice(edge, detected),
        DeviceChoice(firefox, detected),
    ]

    attached = attach_browser_health(
        choices,
        {
            "chromium": _health(
                "chromium", BrowserHealthState.HEALTHY, "complete"
            ),
            "chrome": _health("chrome", BrowserHealthState.FAILED, "launch"),
            "msedge": _health(
                "msedge", BrowserHealthState.PROTOCOL_ERROR, "protocol"
            ),
            "firefox": _health(
                "firefox", BrowserHealthState.CLEANUP_FAILED, "cleanup"
            ),
        },
    )

    assert all(
        attached_choice.device is original.device
        for attached_choice, original in zip(attached, choices)
    )
    assert [choice.selectable for choice in attached] == [True, True, True, False]
    assert browser_health_statuses(attached) == {
        "chromium": detected,
        "chrome": detected,
        "msedge": detected,
        "firefox": detected,
    }
