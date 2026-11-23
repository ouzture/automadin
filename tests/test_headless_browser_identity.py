from __future__ import annotations

from unittest.mock import Mock

from automadin.engine.browser_engine import (
    BrowserDeviceEngine,
    _headed_desktop_user_agent,
)
from automadin.models.device import (
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
)


def test_headless_chromium_uses_reduced_headed_desktop_user_agent() -> None:
    config = BrowserDeviceConfig(
        BrowserEngine.CHROMIUM,
        headless=True,
    )

    user_agent = _headed_desktop_user_agent(config, "145.0.7632.6")

    assert user_agent is not None
    assert "Chrome/145.0.0.0" in user_agent
    assert "HeadlessChrome" not in user_agent


def test_standard_headless_identity_can_be_restored_explicitly() -> None:
    config = BrowserDeviceConfig(
        BrowserEngine.CHROMIUM,
        headless=True,
        headless_uses_headed_user_agent=False,
    )

    assert _headed_desktop_user_agent(config, "145.0.7632.6") is None
    assert "user_agent" not in BrowserDeviceEngine._build_context_kwargs(
        config,
        browser_version="145.0.7632.6",
    )


def test_headed_and_mobile_contexts_keep_their_existing_identity() -> None:
    headed = BrowserDeviceConfig(BrowserEngine.CHROMIUM, headless=False)
    mobile = BrowserDeviceConfig(
        BrowserEngine.CHROMIUM,
        headless=True,
        screen_mode=ScreenMode.MOBILE_ANDROID,
    )

    assert "user_agent" not in BrowserDeviceEngine._build_context_kwargs(
        headed,
        browser_version="145.0.7632.6",
    )
    mobile_kwargs = BrowserDeviceEngine._build_context_kwargs(
        mobile,
        browser_version="145.0.7632.6",
    )
    assert "Mobile" in mobile_kwargs["user_agent"]
    assert "HeadlessChrome" not in mobile_kwargs["user_agent"]


def test_edge_compatibility_identity_preserves_edge_product_token() -> None:
    config = BrowserDeviceConfig(BrowserEngine.MSEDGE, headless=True)

    user_agent = _headed_desktop_user_agent(config, "145.0.7632.6")

    assert user_agent is not None
    assert "Chrome/145.0.0.0" in user_agent
    assert "Edg/145.0.0.0" in user_agent


def test_persistent_launch_receives_profile_version_matched_user_agent(
    tmp_path,
) -> None:
    (tmp_path / "Last Version").write_text("145.0.7632.6", encoding="utf-8")
    playwright = Mock()
    config = BrowserDeviceConfig(BrowserEngine.CHROMIUM, headless=True)

    BrowserDeviceEngine._launch_persistent(playwright, config, str(tmp_path))

    kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["headless"] is True
    assert "Chrome/145.0.0.0" in kwargs["user_agent"]
    assert "HeadlessChrome" not in kwargs["user_agent"]
