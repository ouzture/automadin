"""Qt-free runtime-presence checks for managed browser profiles.

Runtime availability and profile-data health are deliberately independent.
Profiles may be created, copied, repaired, exported, or deleted without their
browser runtime being installed.  Launch preflight remains authoritative when
the profile is actually used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from automadin.runtime import (
    RuntimeComponentStatus,
    browser_runtime_status,
    system_browser_channel_status,
)

SUPPORTED_PROFILE_ENGINES = frozenset({"chromium", "firefox", "chrome", "msedge"})

RuntimeStatusProvider = Callable[[str | Path], RuntimeComponentStatus]
SystemChannelStatusProvider = Callable[[str], RuntimeComponentStatus]

_ENGINE_NAMES = {
    "chromium": "Chromium",
    "firefox": "Firefox",
    "chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
}
_MANAGED_ENGINES = frozenset({"chromium", "firefox"})
_SYSTEM_CHANNELS = {
    "chrome": "chrome",
    "msedge": "msedge",
}


class BrowserProfileReadinessState(str, Enum):
    """Known runtime-presence state for one exact profile engine."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BrowserProfileReadiness:
    """A user-presentable presence assessment for one exact profile engine."""

    engine: str
    state: BrowserProfileReadinessState
    message: str
    path: Path | None
    source: str

    @property
    def launch_blocking(self) -> bool:
        """Return whether the exact browser runtime is proven unavailable."""

        return self.state is BrowserProfileReadinessState.UNAVAILABLE

    @property
    def available(self) -> bool:
        return self.state is BrowserProfileReadinessState.AVAILABLE


def _canonical_engine(engine: str) -> str:
    if not isinstance(engine, str):
        raise TypeError("Browser profile engine must be text.")
    canonical = engine.strip().casefold()
    if canonical not in SUPPORTED_PROFILE_ENGINES:
        raise ValueError(f"Unsupported browser profile engine: {engine!r}.")
    return canonical


def _available_message(engine: str) -> str:
    display_name = _ENGINE_NAMES[engine]
    if engine in _MANAGED_ENGINES:
        return (
            f"{display_name} is available from Automadin's managed Playwright runtime "
            "for profile runs. Automadin checks again when a run starts."
        )
    return (
        f"{display_name} is installed for this profile's runs. Automadin checks "
        "again when a run starts."
    )


def _unavailable_message(engine: str) -> str:
    display_name = _ENGINE_NAMES[engine]
    if engine in _MANAGED_ENGINES:
        return (
            f"{display_name} cannot run yet. Open Tools > Runtime Components and "
            "install or repair Playwright browsers. You can still create, copy, "
            "repair, or delete profiles."
        )
    return (
        f"{display_name} cannot run yet. Install {display_name}, then check again. "
        "You can still copy, repair, or delete profiles."
    )


def _unknown_message(engine: str) -> str:
    return (
        f"Could not check {_ENGINE_NAMES[engine]} availability. Profile management "
        "remains available; Automadin checks the runtime again before launch."
    )


def _status_for_engine(
    engine: str,
    data_dir: str | Path,
    *,
    runtime_status_provider: RuntimeStatusProvider,
    system_channel_status_provider: SystemChannelStatusProvider,
) -> RuntimeComponentStatus:
    if engine in _MANAGED_ENGINES:
        return runtime_status_provider(data_dir)
    return system_channel_status_provider(_SYSTEM_CHANNELS[engine])


def assess_browser_profile_readiness(
    engine: str,
    data_dir: str | Path,
    *,
    runtime_status_provider: RuntimeStatusProvider = browser_runtime_status,
    system_channel_status_provider: SystemChannelStatusProvider = (system_browser_channel_status),
) -> BrowserProfileReadiness:
    """Assess fresh runtime presence for one exact profile engine.

    Provider failures are an unknown, nonblocking state.  The fixed public
    message intentionally does not expose provider or filesystem exception
    details.
    """

    canonical = _canonical_engine(engine)
    try:
        status = _status_for_engine(
            canonical,
            data_dir,
            runtime_status_provider=runtime_status_provider,
            system_channel_status_provider=system_channel_status_provider,
        )
        usable = bool(status.usable)
        source = str(status.source).strip() or "unknown"
        path = status.path
    except Exception:
        return BrowserProfileReadiness(
            engine=canonical,
            state=BrowserProfileReadinessState.UNKNOWN,
            message=_unknown_message(canonical),
            path=None,
            source="unknown",
        )

    return BrowserProfileReadiness(
        engine=canonical,
        state=(
            BrowserProfileReadinessState.AVAILABLE
            if usable
            else BrowserProfileReadinessState.UNAVAILABLE
        ),
        message=(_available_message(canonical) if usable else _unavailable_message(canonical)),
        path=path,
        source=source,
    )


def assess_browser_profile_readiness_many(
    engines: tuple[str, ...],
    data_dir: str | Path,
    *,
    runtime_status_provider: RuntimeStatusProvider = browser_runtime_status,
    system_channel_status_provider: SystemChannelStatusProvider = (system_browser_channel_status),
) -> dict[str, BrowserProfileReadiness]:
    """Assess several exact engines while scanning the managed payload once."""

    canonical_engines = tuple(dict.fromkeys(_canonical_engine(engine) for engine in engines))
    managed_checked = False
    managed_status: RuntimeComponentStatus | None = None
    managed_failed = False

    def memoized_runtime_status(requested_data_dir: str | Path) -> RuntimeComponentStatus:
        nonlocal managed_checked, managed_failed, managed_status
        if not managed_checked:
            managed_checked = True
            try:
                managed_status = runtime_status_provider(requested_data_dir)
            except Exception:
                managed_failed = True
        if managed_failed or managed_status is None:
            raise RuntimeError("Managed browser runtime status is unavailable.")
        return managed_status

    return {
        engine: assess_browser_profile_readiness(
            engine,
            data_dir,
            runtime_status_provider=memoized_runtime_status,
            system_channel_status_provider=system_channel_status_provider,
        )
        for engine in canonical_engines
    }
