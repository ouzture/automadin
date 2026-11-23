"""Runtime resolution transforms shared by coordinates and visual matching."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from automadin.models.resolution_scaling import (
    ResolutionScalingMode,
    ResolutionScalingOverride,
    ResolutionScalingSettings,
)


MIN_TEMPLATE_SCALE = 0.25
MAX_TEMPLATE_SCALE = 4.0
_MATCH_SCALE_OFFSETS = (1.0, 0.95, 1.05, 0.90, 1.10)


@dataclass(frozen=True, slots=True)
class ResolutionTransform:
    """Effective scaling for one current automation surface."""

    enabled: bool = False
    x_scale: float = 1.0
    y_scale: float = 1.0
    scale_pixel_geometry: bool = False

    @property
    def image_scale(self) -> float:
        return sqrt(self.x_scale * self.y_scale) if self.enabled else 1.0

    def template_scales(self) -> tuple[float, ...]:
        """Return bounded scales near the expected size plus legacy 100%."""
        if not self.enabled:
            return (1.0,)
        raw = [self.image_scale * offset for offset in _MATCH_SCALE_OFFSETS]
        # On aspect-ratio changes an application may preserve image size based
        # on either axis rather than the area-derived expected scale.
        raw.extend((self.x_scale, self.y_scale, 1.0))
        scales: list[float] = []
        for value in raw:
            bounded = max(MIN_TEMPLATE_SCALE, min(MAX_TEMPLATE_SCALE, value))
            rounded = round(bounded, 4)
            if rounded not in scales:
                scales.append(rounded)
        return tuple(scales)


def resolve_resolution_transform(
    settings: ResolutionScalingSettings,
    override: ResolutionScalingOverride,
    custom_scale_percent: int,
    surface_size: tuple[int, int],
    *,
    reference_size: tuple[int, int] | None = None,
) -> ResolutionTransform:
    """Resolve flow settings and a run override for one current surface."""
    current_width, current_height = surface_size
    if current_width < 1 or current_height < 1:
        raise RuntimeError("The current automation surface size is invalid.")

    if override is ResolutionScalingOverride.OFF:
        return ResolutionTransform()
    if override is ResolutionScalingOverride.CUSTOM:
        factor = custom_scale_percent / 100.0
        return ResolutionTransform(True, factor, factor, settings.scale_pixel_geometry)

    enabled = (
        override is ResolutionScalingOverride.ADAPTIVE
        or settings.mode is ResolutionScalingMode.ADAPTIVE
    )
    if not enabled:
        return ResolutionTransform()

    reference = reference_size or settings.reference_size
    if reference is None:
        raise RuntimeError(
            "Adaptive resolution scaling needs a reference surface size on the flow."
        )
    reference_width, reference_height = reference
    if reference_width < 1 or reference_height < 1:
        raise RuntimeError("The reference automation surface size is invalid.")
    return ResolutionTransform(
        True,
        current_width / reference_width,
        current_height / reference_height,
        settings.scale_pixel_geometry,
    )


__all__ = ["ResolutionTransform", "resolve_resolution_transform"]
