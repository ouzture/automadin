"""Portable image-file conversion helpers."""

from __future__ import annotations

import io

from PIL import Image, ImageOps


def normalize_image_bytes_to_png(
    data: bytes,
    *,
    optimize: bool = True,
) -> tuple[bytes, int, int]:
    """Decode supported image bytes and return a real PNG payload and dimensions."""
    with Image.open(io.BytesIO(data)) as source:
        source.seek(0)
        oriented = ImageOps.exif_transpose(source)
        has_transparency = (
            "A" in oriented.getbands() or "transparency" in oriented.info
        )
        converted = oriented.convert("RGBA" if has_transparency else "RGB")
        width, height = converted.size
        output = io.BytesIO()
        converted.save(output, format="PNG", optimize=optimize)
    return output.getvalue(), width, height
