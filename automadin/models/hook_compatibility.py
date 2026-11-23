"""Hook compatibility derived from evidence lifetime rather than command lists."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automadin.models.hook import HookType

_BROWSER_ELEMENT_POINT: Mapping[str, object] = {
    "type": HookType.BROWSER_ELEMENT.value,
    "evidence": "browser_dom",
    "phases": ("before", "after"),
}


def hook_points_for_command(
    definition: Mapping[str, Any] | None,
) -> tuple[Mapping[str, object], ...]:
    """Return effective hook points for a command definition.

    Browser DOM evidence belongs to the active browser session and remains
    available around navigation, timing, control-flow, and element commands.
    Visual evidence is still command-specific because it depends on a fresh
    host screenshot.
    """
    declared = tuple(
        point
        for point in (definition or {}).get("hookPoints", ())
        if isinstance(point, Mapping)
    )
    if any(
        point.get("type") == HookType.BROWSER_ELEMENT.value
        for point in declared
    ):
        return declared
    return (*declared, _BROWSER_ELEMENT_POINT)


def command_supports_hook(
    definition: Mapping[str, Any] | None,
    hook_type: HookType,
    phase: object,
) -> bool:
    phase_value = getattr(phase, "value", str(phase))
    return any(
        point.get("type") == hook_type.value
        and phase_value in point.get("phases", ())
        for point in hook_points_for_command(definition)
    )


__all__ = ["command_supports_hook", "hook_points_for_command"]
