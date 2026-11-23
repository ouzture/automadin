"""Scoped image matching that preserves absolute surface coordinates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from automadin.models.typed_value import ValueType
from automadin.services.find_image import MatchResult
from automadin.services.screen_text_region import (
    ScreenTextBox,
    resolve_anchored_box,
    resolve_fixed_box,
)


class ImageSearchRegionError(RuntimeError):
    """Raised when a configured image-search region cannot be used."""


@dataclass(frozen=True, slots=True)
class SearchSurface:
    """Cropped search pixels and their absolute top-left offset."""

    png: bytes
    box: ScreenTextBox
    matched_image: str | None = None
    matched_box: ScreenTextBox | None = None
    surface_size: tuple[int, int] = (0, 0)


def prepare_fixed_search_region(
    screenshot: bytes,
    params: dict[str, object],
    *,
    resolve_value: Callable[[str, object, ValueType], object],
) -> SearchSurface:
    """Return a full or fixed search crop (compatibility helper)."""
    return prepare_search_region(
        screenshot,
        params,
        database=None,
        resolve_value=resolve_value,
        anchor_image_names=[],
    )


def _open_surface(screenshot: bytes) -> Any:
    from PIL import Image

    try:
        return Image.open(BytesIO(screenshot)).convert("RGBA")
    except Exception as exc:
        raise ImageSearchRegionError("Image search screenshot is invalid.") from exc


def _number(
    params: dict[str, object],
    resolve_value: Callable[[str, object, ValueType], object],
    name: str,
    default: object,
) -> float:
    try:
        return float(resolve_value(name, params.get(name, default), ValueType.NUMBER))
    except (TypeError, ValueError) as exc:
        raise ImageSearchRegionError(
            f"Image search {name.replace('_', ' ')} must be a number."
        ) from exc


def _crop(image: Any, box: ScreenTextBox) -> bytes:
    crop = image.crop((box.left, box.top, box.right, box.bottom))
    output = BytesIO()
    crop.save(output, format="PNG")
    return output.getvalue()


def prepare_search_region(
    screenshot: bytes,
    params: dict[str, object],
    database: Any,
    *,
    resolve_value: Callable[[str, object, ValueType], object],
    anchor_image_names: list[str],
    pixel_scale: tuple[float, float] = (1.0, 1.0),
    match_image: Callable[[str, bytes], MatchResult | None] | None = None,
) -> SearchSurface:
    """Resolve a full, fixed, or image-anchored image-search surface.

    Anchor templates are deliberately separate from the templates being searched
    for. The anchor is located against the complete screenshot before its box
    becomes the scope for the target-template search.
    """
    mode = str(params.get("search_region_mode", "full")).casefold()
    image = _open_surface(screenshot)
    surface_size = image.size
    if mode == "full":
        return SearchSurface(
            screenshot,
            ScreenTextBox(0, 0, image.width, image.height),
            surface_size=surface_size,
        )
    if mode == "fixed":
        try:
            pixel_unit = str(
                params.get("search_coordinate_unit", "pixels")
            ).casefold() in {"pixel", "pixels", "px"}
            x_scale, y_scale = pixel_scale if pixel_unit else (1.0, 1.0)
            box = resolve_fixed_box(
                _number(params, resolve_value, "search_start_x", 0) * x_scale,
                _number(params, resolve_value, "search_start_y", 0) * y_scale,
                _number(params, resolve_value, "search_end_x", image.width) * x_scale,
                _number(params, resolve_value, "search_end_y", image.height) * y_scale,
                unit=params.get("search_coordinate_unit", "pixels"),
                surface_size=image.size,
            )
        except Exception as exc:
            raise ImageSearchRegionError(
                f"Image search region is invalid: {exc}"
            ) from exc
        return SearchSurface(_crop(image, box), box, surface_size=surface_size)
    if mode != "image_anchor":
        raise ImageSearchRegionError(
            "Image search region mode must be full, fixed, or image_anchor."
        )
    if database is None:
        raise ImageSearchRegionError("Image search database is unavailable.")
    if not anchor_image_names:
        raise ImageSearchRegionError("Image search anchor images are required.")

    from automadin.services.find_image import FindImageService

    service = FindImageService()
    matched_name: str | None = None
    matched_result = None
    for image_name in anchor_image_names:
        if match_image is not None:
            result = match_image(image_name, screenshot)
        else:
            template = database.load_image(image_name)
            if template is None:
                continue
            result = service.find(
                screenshot,
                template,
                _number(params, resolve_value, "search_anchor_threshold", 0.9),
                bool(
                    resolve_value(
                        "search_anchor_color_match",
                        params.get("search_anchor_color_match", False),
                        ValueType.BOOLEAN,
                    )
                ),
                _number(params, resolve_value, "search_anchor_color_tolerance", 0.3),
            )
        if result is not None:
            matched_name = image_name
            matched_result = result
            break
    if matched_result is None:
        raise ImageSearchRegionError("Image search anchor image was not found.")

    try:
        matched_box = ScreenTextBox(
            int(matched_result.x),
            int(matched_result.y),
            int(matched_result.x + matched_result.width),
            int(matched_result.y + matched_result.height),
        )
        pixel_unit = str(
            params.get("search_coordinate_unit", "pixels")
        ).casefold() in {"pixel", "pixels", "px"}
        x_scale, y_scale = pixel_scale if pixel_unit else (1.0, 1.0)
        box = resolve_anchored_box(
            matched_box,
            anchor=params.get("search_anchor", "top_left"),
            offset_x=_number(params, resolve_value, "search_offset_x", 0) * x_scale,
            offset_y=_number(params, resolve_value, "search_offset_y", 0) * y_scale,
            width=_number(params, resolve_value, "search_region_width", 0) * x_scale,
            height=_number(params, resolve_value, "search_region_height", 0) * y_scale,
            unit=params.get("search_coordinate_unit", "pixels"),
            surface_size=image.size,
        )
    except Exception as exc:
        raise ImageSearchRegionError(
            f"Image search anchored region is invalid: {exc}"
        ) from exc
    return SearchSurface(
        _crop(image, box),
        box,
        matched_name,
        matched_box,
        surface_size,
    )


def to_absolute_match(result: MatchResult, region: SearchSurface) -> MatchResult:
    """Translate a crop-relative template match back into surface coordinates."""
    return MatchResult(
        result.x + region.box.left,
        result.y + region.box.top,
        result.width,
        result.height,
        result.confidence,
        result.color_distance,
    )
