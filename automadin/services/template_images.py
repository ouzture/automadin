"""Safe, Qt-free helpers for creating template images from screenshots."""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from automadin.services.screen_capture import (
    DEFAULT_MAX_CAPTURE_BYTES,
    DEFAULT_MAX_CAPTURE_DIMENSION,
    DEFAULT_MAX_CAPTURE_PIXELS,
    DEFAULT_MAX_DECODED_BYTES,
)


def crop_template_png(
    png_bytes: bytes,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[bytes, int, int]:
    """Crop an image selection and return canonical PNG bytes and dimensions.

    The requested rectangle is intersected with the decoded image bounds.  A
    useful template must retain at least two pixels in each dimension after
    clamping.
    """
    if not isinstance(png_bytes, bytes) or not png_bytes:
        raise ValueError("The screenshot is empty or invalid.")
    if len(png_bytes) > DEFAULT_MAX_CAPTURE_BYTES:
        raise ValueError("The screenshot is too large to crop safely.")
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (x, y, width, height)
    ):
        raise ValueError("The crop rectangle must use whole-pixel values.")
    if width <= 0 or height <= 0:
        raise ValueError("The crop selection must have a positive size.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(png_bytes)) as source:
                image_width, image_height = source.size
                _validate_image_bounds(image_width, image_height)
                oriented = ImageOps.exif_transpose(source)
                has_transparency = (
                    "A" in oriented.getbands()
                    or "transparency" in oriented.info
                )
                image = oriented.convert("RGBA" if has_transparency else "RGB")
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ValueError(
            "The screenshot is not a supported image or is too large to crop safely."
        ) from exc

    image_width, image_height = image.size
    left = max(0, min(image_width, x))
    top = max(0, min(image_height, y))
    right = max(0, min(image_width, x + width))
    bottom = max(0, min(image_height, y + height))
    crop_width = right - left
    crop_height = bottom - top
    if crop_width < 2 or crop_height < 2:
        raise ValueError(
            "Select an area at least 2 × 2 pixels inside the screenshot."
        )

    cropped = image.crop((left, top, right, bottom))
    output = io.BytesIO()
    cropped.save(output, format="PNG", optimize=False)
    payload = output.getvalue()
    if len(payload) > DEFAULT_MAX_CAPTURE_BYTES:
        raise ValueError("The cropped template is too large to save safely.")
    return payload, crop_width, crop_height


def _validate_image_bounds(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("The screenshot has invalid dimensions.")
    if (
        width > DEFAULT_MAX_CAPTURE_DIMENSION
        or height > DEFAULT_MAX_CAPTURE_DIMENSION
        or width * height > DEFAULT_MAX_CAPTURE_PIXELS
        or width * height * 4 > DEFAULT_MAX_DECODED_BYTES
    ):
        raise ValueError("The screenshot is too large to crop safely.")
