from __future__ import annotations

import pytest

from automadin.services.screen_text_region import (
    AnchorPoint,
    CoordinateUnit,
    ScreenTextBox,
    ScreenTextRegionError,
    resolve_anchored_box,
    resolve_fixed_box,
)


def test_fixed_pixel_box_is_validated_against_surface() -> None:
    box = resolve_fixed_box(
        10, 20, 110, 80, unit=CoordinateUnit.PIXELS, surface_size=(800, 600)
    )
    assert box == ScreenTextBox(10, 20, 110, 80)
    assert (box.width, box.height) == (100, 60)


def test_fixed_percent_box_uses_human_friendly_zero_to_hundred_values() -> None:
    box = resolve_fixed_box(
        10, 20, 50, 60, unit=CoordinateUnit.PERCENT, surface_size=(800, 600)
    )
    assert box == ScreenTextBox(80, 120, 400, 360)


@pytest.mark.parametrize(
    ("start", "end"),
    [((10, 10), (10, 20)), ((10, 10), (20, 10)), ((-1, 0), (20, 20))],
)
def test_fixed_box_rejects_empty_or_out_of_bounds_regions(
    start: tuple[int, int], end: tuple[int, int]
) -> None:
    with pytest.raises(ScreenTextRegionError):
        resolve_fixed_box(
            *start,
            *end,
            unit=CoordinateUnit.PIXELS,
            surface_size=(100, 100),
        )


def test_anchored_box_uses_match_corner_and_pixel_offsets() -> None:
    box = resolve_anchored_box(
        ScreenTextBox(100, 100, 200, 160),
        anchor=AnchorPoint.BOTTOM_RIGHT,
        offset_x=10,
        offset_y=5,
        width=120,
        height=40,
        unit=CoordinateUnit.PIXELS,
        surface_size=(800, 600),
    )
    assert box == ScreenTextBox(210, 165, 330, 205)


def test_anchored_percent_geometry_is_relative_to_surface_not_match_size() -> None:
    box = resolve_anchored_box(
        ScreenTextBox(100, 100, 200, 160),
        anchor=AnchorPoint.TOP_LEFT,
        offset_x=10,
        offset_y=10,
        width=20,
        height=10,
        unit=CoordinateUnit.PERCENT,
        surface_size=(800, 600),
    )
    assert box == ScreenTextBox(180, 160, 340, 220)


def test_anchored_box_accepts_negative_percent_offsets() -> None:
    box = resolve_anchored_box(
        ScreenTextBox(100, 100, 200, 160),
        anchor=AnchorPoint.TOP_LEFT,
        offset_x=-10,
        offset_y=-5,
        width=20,
        height=10,
        unit=CoordinateUnit.PERCENT,
        surface_size=(800, 600),
    )
    assert box == ScreenTextBox(20, 70, 180, 130)


def test_anchored_box_rejects_region_that_leaves_surface() -> None:
    with pytest.raises(ScreenTextRegionError, match="within"):
        resolve_anchored_box(
            ScreenTextBox(90, 90, 100, 100),
            anchor=AnchorPoint.BOTTOM_RIGHT,
            offset_x=1,
            offset_y=1,
            width=20,
            height=20,
            unit=CoordinateUnit.PIXELS,
            surface_size=(100, 100),
        )


def test_invalid_unit_and_non_finite_coordinates_fail_closed() -> None:
    with pytest.raises(ScreenTextRegionError, match="Coordinate unit"):
        resolve_fixed_box(0, 0, 1, 1, unit="screen", surface_size=(10, 10))
    with pytest.raises(ScreenTextRegionError, match="finite"):
        resolve_fixed_box(float("nan"), 0, 1, 1, unit="pixels", surface_size=(10, 10))


def test_invalid_anchor_fails_with_region_error() -> None:
    with pytest.raises(ScreenTextRegionError, match="Anchor point"):
        resolve_anchored_box(
            ScreenTextBox(1, 1, 2, 2),
            anchor="baseline",
            offset_x=0,
            offset_y=0,
            width=1,
            height=1,
            unit="pixels",
            surface_size=(10, 10),
        )
