from __future__ import annotations

from io import BytesIO

from PIL import Image

from automadin.services.find_image import MatchResult
from automadin.services.image_search_region import (
    prepare_fixed_search_region,
    prepare_search_region,
    to_absolute_match,
)
from automadin.models.flow import FlowCommand
from automadin.ui.flow_editor.image_search_preview_dialog import _ImageSearchPreviewTask


def test_fixed_percent_region_crops_and_translates_matches_to_surface_coordinates() -> None:
    screenshot = BytesIO()
    Image.new("RGBA", (200, 100), "white").save(screenshot, format="PNG")
    params = {
        "search_region_mode": "fixed",
        "search_coordinate_unit": "percent",
        "search_start_x": 10,
        "search_start_y": 20,
        "search_end_x": 60,
        "search_end_y": 80,
    }
    region = prepare_fixed_search_region(
        screenshot.getvalue(),
        params,
        resolve_value=lambda name, default, _type: params.get(name, default),
    )

    assert region.box.left == 20
    assert region.box.top == 20
    assert region.box.width == 100
    assert region.box.height == 60
    assert to_absolute_match(MatchResult(7, 8, 10, 12, 0.95), region) == MatchResult(
        27, 28, 10, 12, 0.95
    )


def test_fixed_pixel_region_uses_runtime_axis_scales() -> None:
    screenshot = BytesIO()
    Image.new("RGBA", (400, 300), "white").save(screenshot, format="PNG")
    params = {
        "search_region_mode": "fixed",
        "search_coordinate_unit": "pixels",
        "search_start_x": 10,
        "search_start_y": 20,
        "search_end_x": 60,
        "search_end_y": 80,
    }

    region = prepare_search_region(
        screenshot.getvalue(),
        params,
        None,
        resolve_value=lambda name, default, _type: params.get(name, default),
        anchor_image_names=[],
        pixel_scale=(2.0, 1.5),
    )

    assert (region.box.left, region.box.top) == (20, 30)
    assert (region.box.width, region.box.height) == (100, 90)


def test_image_anchored_region_uses_a_separate_anchor_then_crops_target_scope(
    monkeypatch,
) -> None:
    screenshot = BytesIO()
    Image.new("RGBA", (300, 200), "white").save(screenshot, format="PNG")
    params = {
        "search_region_mode": "image_anchor",
        "search_coordinate_unit": "pixels",
        "search_anchor": "top_right",
        "search_offset_x": 5,
        "search_offset_y": 10,
        "search_region_width": 80,
        "search_region_height": 50,
    }

    class Database:
        def load_image(self, name: str) -> bytes | None:
            return b"anchor-template" if name == "marker" else None

    def find(_self, screenshot_bytes, template, *_args):
        assert screenshot_bytes == screenshot.getvalue()
        assert template == b"anchor-template"
        return MatchResult(100, 60, 40, 20, 0.97)

    monkeypatch.setattr("automadin.services.find_image.FindImageService.find", find)
    region = prepare_search_region(
        screenshot.getvalue(),
        params,
        Database(),
        resolve_value=lambda name, default, _type: params.get(name, default),
        anchor_image_names=["marker"],
    )

    assert region.matched_image == "marker"
    assert region.matched_box is not None
    assert region.matched_box.left == 100
    assert region.box.left == 145
    assert region.box.top == 70
    assert region.box.width == 80
    assert region.box.height == 50
    assert to_absolute_match(MatchResult(4, 3, 10, 10, 0.9), region) == MatchResult(
        149, 73, 10, 10, 0.9
    )


def test_image_anchored_pixel_region_scales_offsets_and_size() -> None:
    screenshot = BytesIO()
    Image.new("RGBA", (400, 300), "white").save(screenshot, format="PNG")
    params = {
        "search_region_mode": "image_anchor",
        "search_coordinate_unit": "pixels",
        "search_anchor": "top_left",
        "search_offset_x": 5,
        "search_offset_y": 10,
        "search_region_width": 40,
        "search_region_height": 20,
    }

    region = prepare_search_region(
        screenshot.getvalue(),
        params,
        object(),
        resolve_value=lambda name, default, _type: params.get(name, default),
        anchor_image_names=["marker"],
        pixel_scale=(2.0, 1.5),
        match_image=lambda _name, _png: MatchResult(100, 60, 40, 20, 0.97),
    )

    assert (region.box.left, region.box.top) == (110, 75)
    assert (region.box.width, region.box.height) == (80, 30)


def test_image_search_preview_uses_the_scoped_runtime_surface(monkeypatch) -> None:
    screenshot = BytesIO()
    Image.new("RGBA", (100, 80), "white").save(screenshot, format="PNG")
    command = FlowCommand(
        "find_image",
        params={
            "images": ["target"],
            "search_region_mode": "fixed",
            "search_coordinate_unit": "pixels",
            "search_start_x": 10,
            "search_start_y": 20,
            "search_end_x": 40,
            "search_end_y": 50,
        },
    )

    class Database:
        def load_image(self, name: str) -> bytes | None:
            return b"template" if name == "target" else None

    seen_sizes: list[tuple[int, int]] = []

    def find(_self, cropped_png, template, *_args):
        assert template == b"template"
        with Image.open(BytesIO(cropped_png)) as crop:
            seen_sizes.append(crop.size)
        return MatchResult(3, 4, 10, 8, 0.96)

    monkeypatch.setattr("automadin.services.find_image.FindImageService.find", find)
    task = _ImageSearchPreviewTask("operation", screenshot.getvalue(), command, Database())
    observed: list[tuple[object, str]] = []
    task.signals.completed.connect(lambda _id, result, error: observed.append((result, error)))
    task.run()

    assert seen_sizes == [(30, 30)]
    result, error = observed[0]
    assert error == ""
    assert result.match == MatchResult(13, 24, 10, 8, 0.96)
