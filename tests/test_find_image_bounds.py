from __future__ import annotations

import io
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from automadin.services.find_image import FindImageService


def _png(
    size: tuple[int, int],
    *,
    uniform: bool = False,
) -> bytes:
    image = Image.new("RGB", size, (80, 80, 80))
    if not uniform:
        draw = ImageDraw.Draw(image)
        draw.rectangle((2, 2, max(2, size[0] // 2), max(2, size[1] // 2)), fill=(230, 40, 20))
        draw.line((0, size[1] - 1, size[0] - 1, 0), fill=(20, 180, 220), width=1)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_find_best_does_not_materialize_every_threshold_hit() -> None:
    service = FindImageService()
    screenshot = _png((600, 400), uniform=True)
    template = _png((2, 2), uniform=True)

    with patch(
        "automadin.services.find_image.np.where",
        side_effect=AssertionError("unbounded np.where must not be used"),
    ):
        result = service.find_best(screenshot, template)

    assert result is not None
    assert result.confidence >= 0.90


def test_find_best_caps_color_validation_candidates() -> None:
    service = FindImageService()
    screenshot = _png((180, 120), uniform=True)
    template = _png((2, 2), uniform=True)

    with patch.object(
        service,
        "_color_matches",
        return_value=False,
    ) as color_matches:
        result = service.find_best(
            screenshot,
            template,
            color_match=True,
            max_candidates=7,
        )

    assert result is None
    assert color_matches.call_count <= 7


def test_runtime_find_caps_color_candidates_to_keep_polling_responsive() -> None:
    service = FindImageService()
    screen = np.zeros((1, 601, 3), dtype=np.uint8)
    template = np.zeros((1, 2, 3), dtype=np.uint8)
    scores = np.full((1, 600), 0.95, dtype=np.float32)

    with (
        patch.object(
            service,
            "_prepare_match",
            return_value=(screen, template, scores, 2, 1),
        ),
        patch.object(
            service,
            "_color_matches",
            side_effect=lambda _screen, _template, x, _y, _tolerance: x == 599,
        ) as color_matches,
        patch(
            "automadin.services.find_image.np.where",
            side_effect=AssertionError("unbounded np.where must not be used"),
        ),
    ):
        result = service.find(b"screen", b"template", color_match=True)

    assert result is None
    assert color_matches.call_count == service.DEFAULT_MAX_BEST_CANDIDATES


def test_find_all_honors_candidate_and_result_budgets_on_flat_images() -> None:
    service = FindImageService()
    screenshot = _png((240, 160), uniform=True)
    template = _png((2, 2), uniform=True)

    matches = service.find_all(
        screenshot,
        template,
        max_candidates=35,
        max_matches=4,
    )

    assert 1 <= len(matches) <= 4


def test_find_methods_reject_non_positive_work_budgets() -> None:
    service = FindImageService()
    screenshot = _png((20, 20))
    template = _png((4, 4))

    for call in (
        lambda: service.find_best(screenshot, template, max_candidates=0),
        lambda: service.find_all(screenshot, template, max_candidates=0),
        lambda: service.find_all(screenshot, template, max_matches=0),
    ):
        try:
            call()
        except ValueError as exc:
            assert "positive" in str(exc)
        else:  # pragma: no cover - explicit assertion message is clearer here
            raise AssertionError("Expected a positive-budget validation error")
