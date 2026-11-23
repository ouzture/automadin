from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from automadin.services.screen_text_preprocessing import preprocess_screen_text_png


def _png() -> bytes:
    image = Image.new("RGB", (3, 2), (20, 100, 220))
    image.putpixel((1, 1), (240, 240, 240))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_original_profile_is_lossless() -> None:
    source = _png()

    assert preprocess_screen_text_png(source, "original") == source


@pytest.mark.parametrize("profile", ["grayscale", "contrast"])
def test_grayscale_profiles_preserve_crop_size_and_remove_colour(profile: str) -> None:
    output = preprocess_screen_text_png(_png(), profile)

    with Image.open(BytesIO(output)) as image:
        assert image.size == (3, 2)
        assert image.mode == "L"


def test_upscale_profile_doubles_crop_dimensions() -> None:
    output = preprocess_screen_text_png(_png(), "upscale_contrast")

    with Image.open(BytesIO(output)) as image:
        assert image.size == (6, 4)
        assert image.mode == "L"


def test_bright_color_text_profile_isolates_saturated_pixels_and_upscales() -> None:
    source = Image.new("RGB", (2, 1), (120, 90, 60))
    source.putpixel((1, 0), (230, 210, 20))
    encoded = BytesIO()
    source.save(encoded, format="PNG")

    output = preprocess_screen_text_png(
        encoded.getvalue(),
        "bright_color_text",
    )

    with Image.open(BytesIO(output)) as image:
        assert image.size == (8, 4)
        assert image.mode == "L"
        assert image.getpixel((0, 0)) == 0
        assert image.getpixel((7, 0)) == 255


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown OCR preprocessing profile"):
        preprocess_screen_text_png(_png(), "threshold")
