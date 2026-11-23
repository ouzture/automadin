"""Persisted settings for resolution-aware visual flow execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ResolutionScalingMode(str, Enum):
    """How a flow treats pixel geometry and stored image templates."""

    OFF = "off"
    ADAPTIVE = "adaptive"


class ResolutionScalingOverride(str, Enum):
    """Per-run override layered over the selected flow's settings."""

    INHERIT = "inherit"
    OFF = "off"
    ADAPTIVE = "adaptive"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class ResolutionScalingSettings:
    """Reference surface and scaling policy stored with one flow.

    Reference dimensions describe the automation surface used when the flow was
    authored: browser viewport, Android display, or Windows application window.
    They deliberately do not describe the physical monitor unless the flow
    actually automates the full desktop.
    """

    mode: ResolutionScalingMode = ResolutionScalingMode.OFF
    reference_width: int = 0
    reference_height: int = 0
    scale_pixel_geometry: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ResolutionScalingMode):
            raise ValueError("Resolution scaling mode is invalid.")
        if type(self.reference_width) is not int or self.reference_width < 0:
            raise ValueError("Reference width must be a non-negative integer.")
        if type(self.reference_height) is not int or self.reference_height < 0:
            raise ValueError("Reference height must be a non-negative integer.")
        if self.reference_width > 16_384 or self.reference_height > 16_384:
            raise ValueError("Reference surface dimensions cannot exceed 16384 pixels.")
        if self.mode is ResolutionScalingMode.ADAPTIVE and (
            self.reference_width < 2 or self.reference_height < 2
        ):
            raise ValueError(
                "Adaptive resolution scaling requires reference width and height."
            )
        if type(self.scale_pixel_geometry) is not bool:
            raise ValueError("Pixel geometry scaling must be Boolean.")

    @property
    def reference_size(self) -> tuple[int, int] | None:
        if self.reference_width < 2 or self.reference_height < 2:
            return None
        return self.reference_width, self.reference_height

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "reference_width": self.reference_width,
            "reference_height": self.reference_height,
            "scale_pixel_geometry": self.scale_pixel_geometry,
        }

    @classmethod
    def from_dict(cls, data: object) -> ResolutionScalingSettings:
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("Resolution scaling settings must be an object.")
        try:
            mode = ResolutionScalingMode(data.get("mode", "off"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Resolution scaling mode is invalid.") from exc
        return cls(
            mode=mode,
            reference_width=data.get("reference_width", 0),
            reference_height=data.get("reference_height", 0),
            scale_pixel_geometry=data.get("scale_pixel_geometry", True),
        )


__all__ = [
    "ResolutionScalingMode",
    "ResolutionScalingOverride",
    "ResolutionScalingSettings",
]
