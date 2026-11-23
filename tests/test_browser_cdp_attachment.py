from __future__ import annotations

import base64
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from automadin.engine.browser_engine import (
    BrowserCdpConnectionError,
    BrowserDeviceEngine,
    BrowserSessionData,
)
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
    device_from_saved_id,
)
from automadin.services.device_choices import (
    browser_health_target_for_device,
    runtime_family_for_device,
)


def _config() -> BrowserDeviceConfig:
    return BrowserDeviceConfig(
        engine=BrowserEngine.MSEDGE,
        screen_mode=ScreenMode.DESKTOP,
        cdp_endpoint="http://127.0.0.1:9222",
        cdp_auto_launch=True,
    )


def test_cdp_target_round_trips_through_saved_device_id() -> None:
    saved = _config().to_id()

    device = device_from_saved_id(saved)

    assert device.config == _config()
    assert "Managed CDP" in device.name


def test_cdp_target_rejects_non_loopback_endpoint() -> None:
    config = BrowserDeviceConfig(
        engine=BrowserEngine.MSEDGE,
        cdp_endpoint="http://example.com:9222",
    )

    with pytest.raises(ValueError, match="loopback"):
        config.to_id()


def test_cdp_target_rejects_non_boolean_auto_launch_flag() -> None:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "auto_launch": "false",
                "engine": "msedge",
                "endpoint": "http://127.0.0.1:9222",
            }
        ).encode("utf-8")
    ).decode("ascii")

    with pytest.raises(ValueError, match="Malformed saved browser CDP device ID"):
        device_from_saved_id(f"browser-cdp|1|{payload}")


def test_device_discovery_includes_local_edge_cdp_target() -> None:
    devices = BrowserDeviceEngine().generate_devices()
    attached = [device for device in devices if device.config.cdp_endpoint]

    assert len(attached) == 1
    assert attached[0].config == _config()
    assert runtime_family_for_device(attached[0]) is None
    assert browser_health_target_for_device(attached[0]) is None


def test_cdp_connect_uses_existing_default_context() -> None:
    engine = BrowserDeviceEngine()
    playwright = MagicMock()
    browser = playwright.chromium.connect_over_cdp.return_value
    context = MagicMock()
    context.pages = [MagicMock()]
    browser.contexts = [context]

    with (
        patch.object(engine, "_ensure_cdp_browser"),
        patch("automadin.engine.browser_engine.BrowserSessionData") as session_type,
    ):
        engine._connect_started(playwright, _config())

    playwright.chromium.connect_over_cdp.assert_called_once_with(
        "http://127.0.0.1:9222",
        timeout=30_000,
        is_local=True,
    )
    session_type.assert_called_once_with(
        playwright=playwright,
        browser=browser,
        context=context,
        screen_mode=ScreenMode.DESKTOP,
        externally_owned=True,
    )


def test_cdp_auto_launch_starts_playwright_before_browser() -> None:
    engine = BrowserDeviceEngine()
    playwright = MagicMock()
    events: list[str] = []

    starter = MagicMock()
    starter.return_value.start.side_effect = lambda: (
        events.append("playwright") or playwright
    )

    with (
        patch("automadin.engine.browser_engine.sync_playwright", starter),
        patch.object(
            engine,
            "_ensure_cdp_browser",
            side_effect=lambda _config: events.append("browser"),
        ),
        patch.object(
            engine,
            "_connect_started",
            side_effect=lambda _playwright, _config: events.append("connect")
            or MagicMock(),
        ),
    ):
        engine.connect(BrowserDevice(config=_config()))

    assert events == ["playwright", "browser", "connect"]


def test_cdp_auto_launch_failure_stops_started_playwright() -> None:
    engine = BrowserDeviceEngine()
    playwright = MagicMock()
    starter = MagicMock()
    starter.return_value.start.return_value = playwright

    with (
        patch("automadin.engine.browser_engine.sync_playwright", starter),
        patch.object(
            engine,
            "_ensure_cdp_browser",
            side_effect=BrowserCdpConnectionError("not ready"),
        ),
        patch("automadin.engine.browser_engine._terminate_playwright") as terminate,
        pytest.raises(BrowserCdpConnectionError, match="not ready"),
    ):
        engine.connect(BrowserDevice(config=_config()))

    terminate.assert_called_once_with(playwright)


def test_cdp_disconnect_does_not_close_external_browser_or_context() -> None:
    playwright = MagicMock()
    browser = MagicMock()
    context = MagicMock()
    context.pages = []
    session = BrowserSessionData(
        playwright=playwright,
        browser=browser,
        context=context,
        externally_owned=True,
    )

    session.close()

    playwright.stop.assert_called_once_with()
    context.close.assert_not_called()
    browser.close.assert_not_called()


def test_managed_cdp_reuses_matching_running_edge() -> None:
    engine = BrowserDeviceEngine()

    with (
        patch.object(engine, "_cdp_product", return_value="Edg/152.0") as product,
        patch("automadin.engine.browser_engine.subprocess.Popen") as popen,
    ):
        engine._ensure_cdp_browser(_config())

    product.assert_called_once_with("http://127.0.0.1:9222")
    popen.assert_not_called()


def test_cdp_probe_rejects_non_cdp_http_service() -> None:
    error = urllib.error.HTTPError(
        "http://127.0.0.1:9222/json/version",
        404,
        "Not Found",
        {},
        None,
    )

    with patch("automadin.engine.browser_engine.urllib.request.urlopen", side_effect=error):
        with pytest.raises(BrowserCdpConnectionError, match="not serving a CDP"):
            BrowserDeviceEngine._cdp_product("http://127.0.0.1:9222")


def test_managed_cdp_launches_installed_edge_with_dedicated_profile(
    tmp_path, monkeypatch
) -> None:
    engine = BrowserDeviceEngine()
    monkeypatch.setenv("AUTOMADIN_DATA_DIR", str(tmp_path))
    runtime = MagicMock(usable=True, path=tmp_path / "msedge.exe", error=None)

    with (
        patch.object(engine, "_cdp_product", side_effect=[None, "Edg/152.0"]),
        patch(
            "automadin.engine.browser_engine.system_browser_channel_status",
            return_value=runtime,
        ),
        patch("automadin.engine.browser_engine.subprocess.Popen") as popen,
    ):
        engine._ensure_cdp_browser(_config())

    arguments = popen.call_args.args[0]
    assert str(runtime.path) == arguments[0]
    assert "--remote-debugging-port=9222" in arguments
    assert "--remote-debugging-address=127.0.0.1" in arguments
    assert (
        f"--user-data-dir={tmp_path / 'external-browser-profiles' / 'msedge'}"
        in arguments
    )
