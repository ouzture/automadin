from __future__ import annotations

import io

from PIL import Image

from automadin.services.image_files import normalize_image_bytes_to_png


def test_jpeg_import_is_normalized_to_real_png() -> None:
    source = io.BytesIO()
    Image.new("RGB", (7, 5), (10, 20, 30)).save(source, format="JPEG")

    data, width, height = normalize_image_bytes_to_png(source.getvalue())

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert (width, height) == (7, 5)


def test_palette_transparency_is_preserved() -> None:
    source = io.BytesIO()
    image = Image.new("P", (2, 1))
    image.putpalette([255, 0, 0, 0, 0, 0] + [0, 0, 0] * 254)
    image.putdata([0, 1])
    image.info["transparency"] = 1
    image.save(source, format="PNG")

    data, _, _ = normalize_image_bytes_to_png(source.getvalue())

    with Image.open(io.BytesIO(data)) as converted:
        assert converted.mode == "RGBA"
        assert converted.getpixel((1, 0))[3] == 0
