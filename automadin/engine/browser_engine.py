from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
)
from automadin.engine.browser_pages import BrowserPageRegistry, BrowserTabsSnapshot
from automadin.services.browser_profile_manifest import read_managed_profile_manifest
from automadin.services.browser_profile_leases import OwnerProfileLease
from automadin.services.browser_profile_metadata import (
    BrowserProfileTarget,
    profile_layout_ready,
)
from automadin.services.browser_profile_paths import resolve_profile_storage_path
from automadin.runtime import playwright_browser_versions
from automadin.runtime import system_browser_channel_status

_MOBILE_UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

_MOBILE_UA_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

_VIEWPORTS: dict[ScreenMode, dict[str, int]] = {
    ScreenMode.DESKTOP: {"width": 1280, "height": 800},
    ScreenMode.MOBILE_ANDROID: {"width": 393, "height": 873},
    ScreenMode.MOBILE_IOS: {"width": 390, "height": 844},
}

_CDP_START_LOCK = threading.Lock()
_CDP_START_TIMEOUT_SECONDS = 20.0


def _headed_desktop_user_agent(
    config: BrowserDeviceConfig,
    browser_version: str | None,
) -> str | None:
    """Build a regular desktop UA for a compatible headless Chromium run."""
    if (
        not config.headless
        or not config.headless_uses_headed_user_agent
        or config.screen_mode is not ScreenMode.DESKTOP
        or config.engine
        not in {BrowserEngine.CHROMIUM, BrowserEngine.CHROME, BrowserEngine.MSEDGE}
    ):
        return None
    match = re.match(r"^(\d+)(?:\.\d+){0,3}$", str(browser_version or ""))
    if match is None:
        return None
    reduced_version = f"{match.group(1)}.0.0.0"
    if sys.platform == "darwin":
        platform = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform.startswith("linux"):
        platform = "X11; Linux x86_64"
    else:
        platform = "Windows NT 10.0; Win64; x64"
    user_agent = (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{reduced_version} Safari/537.36"
    )
    if config.engine is BrowserEngine.MSEDGE:
        user_agent += f" Edg/{reduced_version}"
    return user_agent


def _persistent_browser_version(
    config: BrowserDeviceConfig,
    user_data_dir: str,
) -> str | None:
    """Read the profile's last product version, then use package metadata."""
    try:
        version = (Path(user_data_dir) / "Last Version").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        version = ""
    if version:
        return version
    if config.engine is BrowserEngine.CHROMIUM:
        return playwright_browser_versions().get("chromium")
    return None


class BrowserProfileLaunchError(RuntimeError):
    """Raised when a persistent profile cannot be safely resolved for launch."""


class BrowserCdpConnectionError(RuntimeError):
    """Raised when an external Chromium CDP target cannot be safely prepared."""


class BrowserSessionTeardownError(RuntimeError):
    """Raised when browser termination cannot be confirmed safely."""

    def __init__(self, errors: list[BaseException]) -> None:
        detail = "; ".join(str(error) for error in errors if str(error))
        super().__init__(
            "Browser teardown could not be confirmed; any managed profile "
            "must remain locked until Automadin restarts"
            + (f": {detail}" if detail else ".")
        )
        self.errors = tuple(errors)


def _terminate_playwright(playwright: Any) -> None:
    """Stop Playwright or synchronously confirm forced driver-tree termination."""
    errors: list[BaseException] = []
    try:
        playwright.stop()
        return
    except Exception as exc:
        errors.append(exc)

    try:
        process = playwright._impl_obj._connection._transport._proc
        pid = getattr(process, "pid", None)
        if os.name == "nt":
            if not isinstance(pid, int) or pid <= 0:
                raise RuntimeError(
                    "Playwright driver process ID is unavailable; process-tree "
                    "termination cannot be confirmed."
                )
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            taskkill = system_root / "System32" / "taskkill.exe"
            if not taskkill.is_file():
                raise FileNotFoundError(
                    f"Windows process-tree terminator was not found: {taskkill}"
                )
            completed = subprocess.run(
                [str(taskkill), "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).decode(
                    errors="replace"
                ).strip()
                raise RuntimeError(
                    "Windows could not confirm Playwright process-tree "
                    f"termination{f': {detail}' if detail else ''}."
                )
            return

        process.kill()
        wait = getattr(process, "wait", None)
        if not callable(wait) or inspect.iscoroutinefunction(wait):
            raise RuntimeError(
                "The Playwright driver was killed, but this platform cannot "
                "confirm its termination synchronously."
            )
        try:
            wait(timeout=5)
        except TypeError:
            wait()
        raise RuntimeError(
            "The Playwright driver exited, but browser child-tree termination "
            "cannot be confirmed on this platform."
        )
    except Exception as exc:
        errors.append(exc)

    raise BrowserSessionTeardownError(errors)


@dataclass
class BrowserSessionData:
    """Holds all Playwright resources for a single browser session."""

    playwright: Playwright
    browser: Browser | None  # None for persistent contexts
    context: BrowserContext
    screen_mode: ScreenMode = ScreenMode.DESKTOP
    externally_owned: bool = False
    page_registry: BrowserPageRegistry = field(init=False, repr=False)
    element_snapshots: dict[str, tuple[Page, tuple[Any, ...]]] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.page_registry = BrowserPageRegistry(self.context)
        self.element_snapshots = {}

    @property
    def active_page(self) -> Page:
        return self.page_registry.active_page

    def tabs_snapshot(self) -> BrowserTabsSnapshot:
        """Return immutable tab metadata on this session's owner thread."""
        return self.page_registry.snapshot()

    def select_tab(self, tab_id: str) -> None:
        """Select one tracked tab on this session's owner thread."""
        self.page_registry.select_tab(tab_id)

    def screenshot(self) -> bytes:
        """Capture the active page while already on this session's owner thread."""
        return self.active_page.screenshot()

    def close(self) -> None:
        """Close resources or raise when browser termination is uncertain."""
        context_closed = False
        browser_closed = self.browser is None

        for _page, handles in self.element_snapshots.values():
            for handle in handles:
                try:
                    handle.dispose()
                except Exception:
                    pass
        self.element_snapshots.clear()

        if self.externally_owned:
            # A CDP attachment owns only its Playwright driver connection.  The
            # browser, default context, pages, and profile belong to the user.
            _terminate_playwright(self.playwright)
            return

        try:
            self.context.close()
            context_closed = True
        except Exception:
            pass

        if self.browser is not None:
            try:
                self.browser.close()
                browser_closed = True
            except Exception:
                pass

        try:
            _terminate_playwright(self.playwright)
            return
        except BrowserSessionTeardownError:
            # A persistent context is the browser owner, so confirmed context
            # close is enough to make its profile reusable.  For a launched
            # browser, browser.close (not merely context.close) must succeed.
            safely_closed = (
                context_closed if self.browser is None else browser_closed
            )
            if not safely_closed:
                raise


class BrowserDeviceEngine:
    """Generates BrowserDevice combinations and manages Playwright connections."""

    def generate_devices(
        self,
        profiles: list[BrowserProfileTarget] | None = None,
    ) -> list[BrowserDevice]:
        """Generate all browser device combinations.

        For each engine x screen_mode (headed only) = 15 base devices.
        Manifest-verified profiles add devices for their declared engine.  A
        profile's editable display name is carried only for presentation; saved
        device identity and storage resolution use its stable UUID.
        """
        devices: list[BrowserDevice] = []
        engines = list(BrowserEngine)
        screen_modes = list(ScreenMode)

        for engine in engines:
            valid_modes = [ScreenMode.DESKTOP] if engine == BrowserEngine.FIREFOX else screen_modes
            for mode in valid_modes:
                config = BrowserDeviceConfig(
                    engine=engine,
                    headless=False,
                    screen_mode=mode,
                )
                devices.append(BrowserDevice(config=config))

        for profile in profiles or []:
            if not isinstance(profile, BrowserProfileTarget):
                # Old name/tuple inputs are intentionally not converted.  Doing
                # so would recreate the unsafe name-to-folder binding removed by
                # stable profile identity.
                continue
            try:
                engine = BrowserEngine(profile.engine)
            except ValueError:
                continue
            valid_modes = (
                [ScreenMode.DESKTOP]
                if engine == BrowserEngine.FIREFOX
                else screen_modes
            )
            for mode in valid_modes:
                config = BrowserDeviceConfig(
                    engine=engine,
                    headless=False,
                    screen_mode=mode,
                    profile_name=profile.name,
                    profile_id=profile.profile_id,
                )
                devices.append(BrowserDevice(config=config))

        devices.append(
            BrowserDevice(
                config=BrowserDeviceConfig(
                    engine=BrowserEngine.MSEDGE,
                    screen_mode=ScreenMode.DESKTOP,
                    cdp_endpoint="http://127.0.0.1:9222",
                    cdp_auto_launch=True,
                )
            )
        )

        from automadin.services.browser_companion_broker import browser_companion_broker

        broker = browser_companion_broker()
        if broker is not None:
            companion_devices: list[BrowserDevice] = []
            for target in broker.targets():
                try:
                    companion_engine = BrowserEngine(target.engine)
                except ValueError:
                    companion_engine = BrowserEngine.CHROME
                companion_devices.append(
                    BrowserDevice(
                        BrowserDeviceConfig(
                            engine=companion_engine,
                            companion_client_id=target.client_id,
                            companion_target_id=target.target_id,
                            companion_title=target.title,
                            companion_url=target.url,
                        )
                    )
                )
            devices = companion_devices + devices

        return devices

    def connect(
        self,
        device: BrowserDevice,
        *,
        profile_lease: OwnerProfileLease | None = None,
    ) -> Any:
        """Launch a Playwright browser and return session data.

        For profile-based devices, uses ``launch_persistent_context`` so that
        cookies, localStorage, and other browser state are preserved across runs.
        """
        config = device.config
        if config.companion_client_id and config.companion_target_id:
            from automadin.engine.browser_companion import CompanionBrowserPort
            from automadin.services.browser_companion_broker import browser_companion_broker

            broker = browser_companion_broker()
            if broker is None:
                raise RuntimeError("Browser companion service is not running")
            targets = broker.targets()
            target = next(
                (
                    item for item in targets
                    if item.client_id == config.companion_client_id
                    and item.target_id == config.companion_target_id
                ),
                None,
            )
            if target is None and config.companion_url:
                matches = [item for item in targets if item.url == config.companion_url]
                target = matches[0] if len(matches) == 1 else None
            if target is None and config.companion_title:
                matches = [item for item in targets if item.title == config.companion_title]
                target = matches[0] if len(matches) == 1 else None
            if target is None:
                raise RuntimeError("The granted companion tab is no longer available")
            return CompanionBrowserPort(broker, target)
        if config.profile_id or config.profile_name:
            # Validate storage first so setup errors remain specific and useful.
            # The owner token is still required before Playwright can start.
            self._profile_path(config)
        if config.profile_id:
            if profile_lease is None:
                raise BrowserProfileLaunchError(
                    "This managed browser profile is not reserved for this "
                    "session. Reconnect the device and try again."
                )
            profile_lease.ensure_live(config.profile_id)
        elif profile_lease is not None:
            raise BrowserProfileLaunchError(
                "A managed profile reservation cannot be used for a temporary "
                "browser session."
            )
        pw = sync_playwright().start()

        try:
            # Start Playwright before launching an external CDP browser.  On
            # Windows, starting both process trees in the opposite order can
            # leave the driver handshake waiting indefinitely while the new
            # browser is still initializing its profile and extensions.
            if config.cdp_endpoint and config.cdp_auto_launch:
                self._ensure_cdp_browser(config)
            return self._connect_started(pw, config)
        except Exception as startup_error:
            try:
                _terminate_playwright(pw)
            except BrowserSessionTeardownError as teardown_error:
                raise teardown_error from startup_error
            raise

    def _connect_started(
        self,
        pw: Playwright,
        config: BrowserDeviceConfig,
    ) -> BrowserSessionData:

        if config.cdp_endpoint:
            if config.engine not in {
                BrowserEngine.CHROMIUM,
                BrowserEngine.CHROME,
                BrowserEngine.MSEDGE,
            }:
                raise ValueError("CDP attachment requires a Chromium-based browser")
            browser = pw.chromium.connect_over_cdp(
                config.cdp_endpoint,
                timeout=30_000,
                is_local=True,
            )
            contexts = browser.contexts
            if not contexts:
                raise RuntimeError("The attached browser has no accessible context")
            context = contexts[0]
            if not context.pages:
                context.new_page()
            return BrowserSessionData(
                playwright=pw,
                browser=browser,
                context=context,
                screen_mode=ScreenMode.DESKTOP,
                externally_owned=True,
            )

        if config.profile_id or config.profile_name:
            # Persistent context — profile data is saved/loaded from disk
            user_data_dir = self._profile_path(config)

            context = self._launch_persistent(pw, config, str(user_data_dir))
            browser = None

            # Persistent context may already have pages open
            if not context.pages:
                context.new_page()
        else:
            # Temporary context — no profile persistence
            browser = self._launch_browser(pw, config)
            context_kwargs = self._build_context_kwargs(
                config,
                browser_version=getattr(browser, "version", None),
            )
            context = browser.new_context(**context_kwargs)
            context.new_page()

        return BrowserSessionData(
            playwright=pw,
            browser=browser,
            context=context,
            screen_mode=config.screen_mode,
        )

    @staticmethod
    def _cdp_product(endpoint: str) -> str | None:
        url = endpoint.rstrip("/") + "/json/version"
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BrowserCdpConnectionError(
                f"Port {endpoint} is in use, but it is not serving a CDP browser."
            ) from exc
        except (OSError, TimeoutError, urllib.error.URLError):
            return None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrowserCdpConnectionError(
                f"Port {endpoint} responded, but it is not a valid CDP endpoint."
            ) from exc
        product = payload.get("Browser") if isinstance(payload, dict) else None
        if not isinstance(product, str) or not product.strip():
            raise BrowserCdpConnectionError(
                f"Port {endpoint} responded, but did not identify a CDP browser."
            )
        return product.strip()

    @staticmethod
    def _validate_cdp_product(config: BrowserDeviceConfig, product: str) -> None:
        expected_prefixes = {
            BrowserEngine.MSEDGE: ("Edg/",),
            BrowserEngine.CHROME: ("Chrome/",),
            BrowserEngine.CHROMIUM: ("Chrome/", "Chromium/", "HeadlessChrome/"),
        }
        prefixes = expected_prefixes.get(config.engine, ())
        if prefixes and not product.startswith(prefixes):
            raise BrowserCdpConnectionError(
                f"The CDP endpoint is {product}, not the selected "
                f"{config.engine.value} browser."
            )

    @staticmethod
    def _managed_cdp_profile_path(config: BrowserDeviceConfig) -> Path:
        data_dir = Path(os.environ.get("AUTOMADIN_DATA_DIR", ".")).resolve()
        return data_dir / "external-browser-profiles" / config.engine.value

    def _ensure_cdp_browser(self, config: BrowserDeviceConfig) -> None:
        endpoint = config.cdp_endpoint
        if endpoint is None:
            raise BrowserCdpConnectionError("CDP endpoint is missing.")
        if config.engine not in {BrowserEngine.CHROME, BrowserEngine.MSEDGE}:
            raise BrowserCdpConnectionError(
                "Automatic CDP launch currently supports installed Chrome or Edge."
            )

        with _CDP_START_LOCK:
            product = self._cdp_product(endpoint)
            if product is not None:
                self._validate_cdp_product(config, product)
                return

            channel = config.engine.value
            runtime = system_browser_channel_status(channel)
            if not runtime.usable or runtime.path is None:
                raise BrowserCdpConnectionError(
                    runtime.error or f"Installed browser '{channel}' was not found."
                )

            profile_path = self._managed_cdp_profile_path(config)
            profile_path.mkdir(parents=True, exist_ok=True)
            from urllib.parse import urlsplit

            parsed = urlsplit(endpoint)
            if parsed.port is None:
                raise BrowserCdpConnectionError("CDP endpoint port is missing.")
            arguments = [
                str(runtime.path),
                f"--remote-debugging-port={parsed.port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile_path}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ]
            try:
                subprocess.Popen(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            except OSError as exc:
                raise BrowserCdpConnectionError(
                    f"Could not start installed {config.engine.value}: {exc}"
                ) from exc

            deadline = time.monotonic() + _CDP_START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                product = self._cdp_product(endpoint)
                if product is not None:
                    self._validate_cdp_product(config, product)
                    return
                time.sleep(0.2)
            raise BrowserCdpConnectionError(
                f"Installed {config.engine.value} started, but its CDP endpoint "
                f"did not become ready within {_CDP_START_TIMEOUT_SECONDS:.0f} seconds."
            )

    def disconnect(self, session_data: Any) -> None:
        """Close all Playwright resources."""
        session_data.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _profile_path(config: BrowserDeviceConfig) -> Path:
        """Resolve and validate a stable persistent profile without mutating it."""
        if not config.profile_id:
            raise BrowserProfileLaunchError(
                "This browser target uses an unresolved legacy profile name. "
                "Open Tools > Browser Profiles and explicitly attach or import "
                "the profile before running it."
            )

        profile_path = resolve_profile_storage_path(config.profile_id)
        manifest = read_managed_profile_manifest(config.profile_id)
        expected_engine = config.engine.value
        if manifest.engine != expected_engine:
            raise BrowserProfileLaunchError(
                f"Browser profile '{config.profile_name or config.profile_id}' "
                f"requires engine '{manifest.engine}', but this target requests "
                f"'{expected_engine}'. Select the profile again."
            )
        if not profile_layout_ready(profile_path, manifest.engine):
            raise BrowserProfileLaunchError(
                f"Browser profile '{config.profile_name or config.profile_id}' "
                "does not contain a compatible managed browser layout. Repair or "
                "re-import it from Tools > Browser Profiles."
            )
        return profile_path

    @staticmethod
    def _launch_browser(pw: Playwright, config: BrowserDeviceConfig) -> Browser:
        launch_kwargs: dict = {"headless": config.headless}

        if config.engine in (BrowserEngine.CHROMIUM, BrowserEngine.CHROME, BrowserEngine.MSEDGE):
            if config.engine == BrowserEngine.CHROME:
                launch_kwargs["channel"] = "chrome"
            elif config.engine == BrowserEngine.MSEDGE:
                launch_kwargs["channel"] = "msedge"
            return pw.chromium.launch(**launch_kwargs)

        if config.engine == BrowserEngine.FIREFOX:
            return pw.firefox.launch(**launch_kwargs)

        # WEBKIT
        return pw.webkit.launch(**launch_kwargs)

    @staticmethod
    def _launch_persistent(
        pw: Playwright, config: BrowserDeviceConfig, user_data_dir: str
    ) -> BrowserContext:
        """Launch a persistent browser context backed by a profile directory."""
        kwargs: dict = {"headless": config.headless}

        if config.engine in (BrowserEngine.CHROMIUM, BrowserEngine.CHROME, BrowserEngine.MSEDGE):
            if config.engine == BrowserEngine.CHROME:
                kwargs["channel"] = "chrome"
            elif config.engine == BrowserEngine.MSEDGE:
                kwargs["channel"] = "msedge"
            bt = pw.chromium
        elif config.engine == BrowserEngine.FIREFOX:
            bt = pw.firefox
        else:
            bt = pw.webkit

        kwargs.update(
            BrowserDeviceEngine._build_context_kwargs(
                config,
                browser_version=_persistent_browser_version(config, user_data_dir),
            )
        )

        return bt.launch_persistent_context(user_data_dir, **kwargs)

    @staticmethod
    def _build_context_kwargs(
        config: BrowserDeviceConfig,
        *,
        browser_version: str | None = None,
    ) -> dict:
        viewport = _VIEWPORTS[config.screen_mode]
        kwargs: dict[str, Any] = {"viewport": viewport}

        if config.screen_mode == ScreenMode.MOBILE_ANDROID:
            kwargs["user_agent"] = _MOBILE_UA_ANDROID
            kwargs["is_mobile"] = True
            kwargs["has_touch"] = True
        elif config.screen_mode == ScreenMode.MOBILE_IOS:
            kwargs["user_agent"] = _MOBILE_UA_IOS
            kwargs["is_mobile"] = True
            kwargs["has_touch"] = True
        else:
            user_agent = _headed_desktop_user_agent(config, browser_version)
            if user_agent is not None:
                kwargs["user_agent"] = user_agent

        return kwargs
