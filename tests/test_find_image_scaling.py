from __future__ import annotations

import numpy as np
import pytest

from automadin.services.find_image import FindImageService, cv2


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return bytes(encoded)


@pytest.mark.parametrize("scale", [0.5, 1.5, 2.0])
def test_find_scaled_matches_resized_template(scale: float) -> None:
    rng = np.random.default_rng(71)
    template = rng.integers(0, 256, size=(18, 24, 3), dtype=np.uint8)
    width = round(template.shape[1] * scale)
    height = round(template.shape[0] * scale)
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(template, (width, height), interpolation=interpolation)
    screenshot = np.zeros((140, 220, 3), dtype=np.uint8)
    screenshot[47 : 47 + height, 83 : 83 + width] = resized

    result = FindImageService().find_scaled(
        _png(screenshot),
        _png(template),
        (scale,),
        threshold=0.99,
    )

    assert result is not None
    assert (result.x, result.y, result.width, result.height) == (
        83,
        47,
        width,
        height,
    )


def test_find_scaled_keeps_original_size_as_a_fallback_candidate() -> None:
    template = np.zeros((12, 16, 3), dtype=np.uint8)
    template[2:10, 3:13] = (20, 180, 240)
    screenshot = np.zeros((80, 100, 3), dtype=np.uint8)
    screenshot[30:42, 40:56] = template

    result = FindImageService().find_scaled(
        _png(screenshot),
        _png(template),
        (1.8, 1.0),
        threshold=0.99,
    )

    assert result is not None
    assert (result.x, result.y, result.width, result.height) == (40, 30, 16, 12)
