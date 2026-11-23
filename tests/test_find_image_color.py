from __future__ import annotations

import numpy as np

from automadin.services.find_image import FindImageService, cv2


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return bytes(encoded)


def test_color_match_tolerates_small_palette_shift_across_renderers() -> None:
    template = np.zeros((10, 132, 3), dtype=np.uint8)
    template[:4, :] = (82, 81, 107)
    template[7:, :] = (74, 77, 8)
    screenshot = template.copy()
    screenshot[7:, :] = (74, 77, 16)

    match = FindImageService().find(
        _png(screenshot),
        _png(template),
        threshold=0.99,
        color_match=True,
        color_tolerance=0.3,
    )

    assert match is not None
    assert match.confidence >= 0.99
    assert match.color_distance is not None
    assert match.color_distance <= 0.3


def test_color_match_rejects_substantial_recolor_with_same_grayscale_shape() -> None:
    template = np.zeros((20, 20, 3), dtype=np.uint8)
    template[3:17, 4:16] = (0, 100, 0)
    recolored = np.zeros_like(template)
    # These colors have effectively the same grayscale intensity, so shape
    # matching succeeds and the color check has to distinguish the states.
    recolored[3:17, 4:16] = (0, 0, 196)
    service = FindImageService()

    shape_match = service.find(
        _png(recolored),
        _png(template),
        threshold=0.99,
        color_match=False,
    )
    color_match = service.find(
        _png(recolored),
        _png(template),
        threshold=0.99,
        color_match=True,
        color_tolerance=0.3,
    )

    assert shape_match is not None
    assert color_match is None


def test_color_match_limits_isolated_antialiasing_outliers() -> None:
    template = np.full((20, 20, 3), (30, 140, 210), dtype=np.uint8)
    screenshot = template.copy()
    screenshot[4:6, 4:6] = (210, 30, 140)

    assert FindImageService._color_matches(
        screenshot,
        template,
        0,
        0,
        tolerance=0.3,
    )


def test_color_match_rejects_localized_state_change() -> None:
    template = np.full((20, 20, 3), (30, 140, 210), dtype=np.uint8)
    screenshot = template.copy()
    screenshot[8:12, :] = (210, 30, 140)

    assert not FindImageService._color_matches(
        screenshot,
        template,
        0,
        0,
        tolerance=0.3,
    )


def test_color_match_tolerates_one_pixel_render_shift() -> None:
    template = np.zeros((24, 24, 3), dtype=np.uint8)
    template[4:20, 5:19] = (35, 145, 220)
    screenshot = np.zeros_like(template)
    screenshot[5:21, 6:20] = (35, 145, 220)

    assert FindImageService._color_matches(
        screenshot,
        template,
        0,
        0,
        tolerance=0.3,
    )


def test_color_match_rejects_greyed_out_state() -> None:
    template = np.zeros((24, 24, 3), dtype=np.uint8)
    template[4:20, 5:19] = (35, 145, 220)
    grey = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    screenshot = cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)
    service = FindImageService()

    shape_match = service.find(
        _png(screenshot),
        _png(template),
        threshold=0.99,
        color_match=False,
    )
    color_match = service.find(
        _png(screenshot),
        _png(template),
        threshold=0.99,
        color_match=True,
        color_tolerance=0.3,
    )

    assert shape_match is not None
    assert color_match is None


def test_color_match_treats_hue_boundary_as_circular() -> None:
    template_hsv = np.full((20, 20, 3), (179, 220, 180), dtype=np.uint8)
    screenshot_hsv = np.full((20, 20, 3), (1, 220, 180), dtype=np.uint8)
    template = cv2.cvtColor(template_hsv, cv2.COLOR_HSV2BGR)
    screenshot = cv2.cvtColor(screenshot_hsv, cv2.COLOR_HSV2BGR)

    assert FindImageService._color_matches(
        screenshot,
        template,
        0,
        0,
        tolerance=0.3,
    )
