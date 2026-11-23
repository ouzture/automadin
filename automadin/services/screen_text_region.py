"""Validated screenshot-region geometry for screen-text recognition."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class CoordinateUnit(str, Enum):
    PIXELS = "pixels"
    PERCENT = "percent"


class AnchorPoint(str, Enum):
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    CENTER = "center"


class ScreenTextRegionError(ValueError):
    """A screen-text region cannot be represented on the target surface."""


@dataclass(frozen=True, slots=True)
class ScreenTextBox:
    """A half-open pixel box: ``left <= x < right`` and ``top <= y < bottom``."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def validate(self, surface_size: tuple[int, int]) -> ScreenTextBox:
        surface_width, surface_height = surface_size
        if surface_width <= 0 or surface_height <= 0:
            raise ScreenTextRegionError("Screenshot surface must have positive dimensions.")
        if self.width <= 0 or self.height <= 0:
            raise ScreenTextRegionError("Screen-text region must have non-zero area.")
        if not (
            0 <= self.left < self.right <= surface_width
            and 0 <= self.top < self.bottom <= surface_height
        ):
            raise ScreenTextRegionError(
                "Screen-text region must stay within the screenshot surface."
            )
        return self


def _as_unit(value: CoordinateUnit | str) -> CoordinateUnit:
    try:
        return value if isinstance(value, CoordinateUnit) else CoordinateUnit(str(value).casefold())
    except ValueError as exc:
        raise ScreenTextRegionError("Coordinate unit must be Pixels or Percent.") from exc


def _as_anchor(value: AnchorPoint | str) -> AnchorPoint:
    try:
        return value if isinstance(value, AnchorPoint) else AnchorPoint(str(value).casefold())
    except ValueError as exc:
        raise ScreenTextRegionError(
            "Anchor point must be a supported image corner or center."
        ) from exc


def _coordinate(
    value: object,
    size: int,
    unit: CoordinateUnit,
    *,
    allow_negative_percent: bool = False,
) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScreenTextRegionError("Screen-text coordinates must be finite numbers.")
    number = float(value)
    if not math.isfinite(number):
        raise ScreenTextRegionError("Screen-text coordinates must be finite numbers.")
    if unit is CoordinateUnit.PERCENT:
        lower_bound = -100 if allow_negative_percent else 0
        if number < lower_bound or number > 100:
            raise ScreenTextRegionError(
                "Percentage coordinates must be between "
                f"{lower_bound} and 100."
            )
        return round(number / 100 * size)
    return round(number)


def resolve_fixed_box(
    start_x: object,
    start_y: object,
    end_x: object,
    end_y: object,
    *,
    unit: CoordinateUnit | str,
    surface_size: tuple[int, int],
) -> ScreenTextBox:
    """Resolve a fixed start/end region against the captured surface."""
    normalized_unit = _as_unit(unit)
    width, height = surface_size
    box = ScreenTextBox(
        _coordinate(start_x, width, normalized_unit),
        _coordinate(start_y, height, normalized_unit),
        _coordinate(end_x, width, normalized_unit),
        _coordinate(end_y, height, normalized_unit),
    )
    return box.validate(surface_size)


def _anchor_coordinates(match: ScreenTextBox, anchor: AnchorPoint) -> tuple[float, float]:
    if anchor is AnchorPoint.TOP_LEFT:
        return match.left, match.top
    if anchor is AnchorPoint.TOP_RIGHT:
        return match.right, match.top
    if anchor is AnchorPoint.BOTTOM_LEFT:
        return match.left, match.bottom
    if anchor is AnchorPoint.BOTTOM_RIGHT:
        return match.right, match.bottom
    return (match.left + match.right) / 2, (match.top + match.bottom) / 2


def resolve_anchored_box(
    match_box: ScreenTextBox,
    *,
    anchor: AnchorPoint | str,
    offset_x: object,
    offset_y: object,
    width: object,
    height: object,
    unit: CoordinateUnit | str,
    surface_size: tuple[int, int],
) -> ScreenTextBox:
    """Build and validate a region from a matched image and anchor geometry."""
    normalized_unit = _as_unit(unit)
    normalized_anchor = _as_anchor(anchor)
    match_box.validate(surface_size)
    surface_width, surface_height = surface_size
    anchor_x, anchor_y = _anchor_coordinates(match_box, normalized_anchor)
    offset_pixels_x = _coordinate(
        offset_x,
        surface_width,
        normalized_unit,
        allow_negative_percent=True,
    )
    offset_pixels_y = _coordinate(
        offset_y,
        surface_height,
        normalized_unit,
        allow_negative_percent=True,
    )
    box_width = _coordinate(width, surface_width, normalized_unit)
    box_height = _coordinate(height, surface_height, normalized_unit)
    if box_width <= 0 or box_height <= 0:
        raise ScreenTextRegionError("Screen-text region width and height must be positive.")
    box = ScreenTextBox(
        round(anchor_x) + offset_pixels_x,
        round(anchor_y) + offset_pixels_y,
        round(anchor_x) + offset_pixels_x + box_width,
        round(anchor_y) + offset_pixels_y + box_height,
    )
    return box.validate(surface_size)


__all__ = [
    "AnchorPoint",
    "CoordinateUnit",
    "ScreenTextBox",
    "ScreenTextRegionError",
    "resolve_anchored_box",
    "resolve_fixed_box",
]
