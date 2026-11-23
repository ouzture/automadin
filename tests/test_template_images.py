from __future__ import annotations

import io

import pytest
from PIL import Image

from automadin.services.template_images import crop_template_png


def _source_bytes(
    *,
    size: tuple[int, int] = (5, 4),
    image_format: str = "PNG",
) -> bytes:
    image = Image.new("RGB", size)
    for pixel_y in range(size[1]):
        for pixel_x in range(size[0]):
            image.putpixel(
                (pixel_x, pixel_y),
                (pixel_x * 30, pixel_y * 40, pixel_x + pixel_y),
            )
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _decoded(payload: bytes) -> Image.Image:
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        return image.copy()


def test_crop_template_png_preserves_exact_pixels_and_dimensions() -> None:
    source = _source_bytes()

    payload, width, height = crop_template_png(source, 1, 1, 3, 2)

    cropped = _decoded(payload)
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert (width, height) == (3, 2)
    assert cropped.size == (3, 2)
    assert [
        cropped.getpixel((pixel_x, pixel_y))
        for pixel_y in range(cropped.height)
        for pixel_x in range(cropped.width)
    ] == [
        (30, 40, 2),
        (60, 40, 3),
        (90, 40, 4),
        (30, 80, 3),
        (60, 80, 4),
        (90, 80, 5),
    ]


def test_crop_template_png_accepts_an_ordinary_supported_image_format() -> None:
    source = _source_bytes(image_format="BMP")

    payload, width, height = crop_template_png(source, 0, 0, 2, 3)

    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert (width, height) == (2, 3)


def test_crop_template_png_clamps_selection_at_bottom_right_edge() -> None:
    source = _source_bytes()

    payload, width, height = crop_template_png(source, 3, 2, 20, 20)

    assert (width, height) == (2, 2)
    assert _decoded(payload).getpixel((0, 0)) == (90, 80, 5)


def test_crop_template_png_clamps_negative_origin_using_rectangle_intersection() -> None:
    source = _source_bytes()

    payload, width, height = crop_template_png(source, -1, -2, 4, 4)

    assert (width, height) == (3, 2)
    assert _decoded(payload).getpixel((2, 1)) == (60, 40, 3)


@pytest.mark.parametrize(
    ("rect", "message"),
    [
        ((10, 10, 3, 3), "at least 2"),
        ((-10, -10, 3, 3), "at least 2"),
        ((4, 0, 5, 4), "at least 2"),
        ((0, 3, 5, 5), "at least 2"),
        ((0, 0, 1, 3), "at least 2"),
        ((0, 0, 3, 1), "at least 2"),
        ((0, 0, 0, 3), "positive size"),
    ],
)
def test_crop_template_png_rejects_out_of_bounds_or_too_small_selections(
    rect: tuple[int, int, int, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        crop_template_png(_source_bytes(), *rect)


def test_crop_template_png_rejects_invalid_image_bytes() -> None:
    with pytest.raises(ValueError, match="not a supported image"):
        crop_template_png(b"not an image", 0, 0, 2, 2)


def test_crop_template_png_rejects_decompression_bomb_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_bytes(size=(9, 8))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 40)

    with pytest.raises(ValueError, match="too large to crop safely"):
        crop_template_png(source, 0, 0, 2, 2)


def test_crop_template_png_does_not_mutate_input_bytes() -> None:
    source = _source_bytes()
    original = bytes(source)

    crop_template_png(source, 1, 1, 2, 2)

    assert source == original
