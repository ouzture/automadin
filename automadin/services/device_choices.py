"""Qt-free runtime availability assessment for device selectors."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from automadin.models.device import BrowserDevice, BrowserEngine, Device
from automadin.runtime import RuntimeComponentStatus, device_runtime_status
from automadin.services.browser_health import BrowserHealthResult

RuntimeStatusProvider = Callable[
    [Device | object, str | Path],
    RuntimeComponentStatus,
]


@dataclass(frozen=True, slots=True)
class DeviceChoice:
    """A device and the runtime information known when it was listed."""

    device: Device
    runtime_status: RuntimeComponentStatus | None
    assessment_error: str | None = None
    launch_health: BrowserHealthResult | None = None
    launch_assessment_error: str | None = None

    @property
    def selectable(self) -> bool:
        """Unknown assessment state remains selectable."""
        if self.runtime_status is not None and not self.runtime_status.usable:
            return False
        health = self.launch_health
        if health is None:
            return True
        return not health.blocks_new_launch


def runtime_family_for_device(device: Device | object) -> str | None:
    """Return the shared browser-runtime family to assess for ``device``."""
    if not isinstance(device, BrowserDevice):
        return None
    if device.config.cdp_endpoint or device.config.companion_target_id:
        return None

    engine = device.config.engine
    if engine in {
        BrowserEngine.CHROMIUM,
        BrowserEngine.FIREFOX,
        BrowserEngine.WEBKIT,
    }:
        return "playwright"
    if engine == BrowserEngine.CHROME:
        return "chrome"
    if engine == BrowserEngine.MSEDGE:
        return "msedge"
    return None


def browser_health_target_for_device(device: Device | object) -> str | None:
    """Return the exact launch-health target for a browser device."""
    if not isinstance(device, BrowserDevice):
        return None
    if device.config.cdp_endpoint or device.config.companion_target_id:
        return None
    return device.config.engine.value


def _error_text(error: BaseException) -> str:
    return str(error).strip() or type(error).__name__


def assess_device_choices(
    devices: Iterable[Device],
    data_dir: str | Path,
    status_provider: RuntimeStatusProvider = device_runtime_status,
) -> list[DeviceChoice]:
    """Assess browser runtimes once per family while preserving input choices.

    Non-browser devices are intentionally not probed in this selector-time
    layer. Provider failures represent unknown availability, so they are
    attached as assessment errors without making the corresponding choice
    unselectable.
    """
    family_results: dict[
        str,
        tuple[RuntimeComponentStatus | None, str | None],
    ] = {}
    choices: list[DeviceChoice] = []

    for device in devices:
        family = runtime_family_for_device(device)
        if family is None:
            choices.append(DeviceChoice(device=device, runtime_status=None))
            continue

        if family not in family_results:
            try:
                family_results[family] = (
                    status_provider(device, data_dir),
                    None,
                )
            except Exception as exc:
                family_results[family] = (None, _error_text(exc))

        runtime_status, assessment_error = family_results[family]
        choices.append(
            DeviceChoice(
                device=device,
                runtime_status=runtime_status,
                assessment_error=assessment_error,
            )
        )

    return choices


def browser_health_statuses(
    choices: Iterable[DeviceChoice],
) -> dict[str, RuntimeComponentStatus]:
    """Return one detected runtime status for each exact browser target."""
    statuses: dict[str, RuntimeComponentStatus] = {}
    for choice in choices:
        target = browser_health_target_for_device(choice.device)
        if target is not None and choice.runtime_status is not None:
            statuses.setdefault(target, choice.runtime_status)
    return statuses


def attach_browser_health(
    choices: Iterable[DeviceChoice],
    results: dict[str, BrowserHealthResult],
) -> list[DeviceChoice]:
    """Attach exact-engine launch results without changing identity or ordering."""
    attached: list[DeviceChoice] = []
    for choice in choices:
        target = browser_health_target_for_device(choice.device)
        attached.append(
            replace(
                choice,
                launch_health=results.get(target) if target is not None else None,
            )
        )
    return attached


def attach_browser_health_error(
    choices: Iterable[DeviceChoice],
    error: BaseException,
) -> list[DeviceChoice]:
    """Preserve detected choices when the optional launch-health layer fails."""
    message = _error_text(error)
    return [
        replace(
            choice,
            launch_assessment_error=(
                message
                if browser_health_target_for_device(choice.device) is not None
                else None
            ),
        )
        for choice in choices
    ]
