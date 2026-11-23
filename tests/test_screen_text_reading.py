from __future__ import annotations

from io import BytesIO

from PIL import Image

from automadin.services import screen_text_reading
from automadin.services.find_image import MatchResult
from automadin.services.ocr import OcrRecognitionOptions
from automadin.services.screen_text_reading import read_screen_text
from automadin.services.screen_text_preprocessing import preprocess_screen_text_png


class _Engine:
    def __init__(self) -> None:
        self.crops: list[tuple[int, int]] = []

    def recognize_png(self, png: bytes) -> str:
        with Image.open(BytesIO(png)) as image:
            self.crops.append(image.size)
        return "12,5"


class _ConfigurableEngine(_Engine):
    def __init__(self, result: str = "0:00\nneee") -> None:
        super().__init__()
        self.result = result
        self.options: list[OcrRecognitionOptions] = []

    def recognize_png_with_options(
        self,
        png: bytes,
        options: OcrRecognitionOptions,
    ) -> str:
        with Image.open(BytesIO(png)) as image:
            self.crops.append(image.size)
        self.options.append(options)
        return self.result


class _Database:
    @staticmethod
    def load_image(name: str) -> bytes | None:
        return b"template" if name == "marker" else None


def _screenshot() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (200, 100), "white").save(output, format="PNG")
    return output.getvalue()


def _literal(params: dict[str, object]):
    return lambda name, default, _type: params.get(name, default)


def test_shared_reader_crops_and_parses_the_same_typed_result_used_by_preview() -> None:
    params = {
        "coordinate_unit": "percent",
        "start_x": 10,
        "start_y": 20,
        "end_x": 60,
        "end_y": 80,
        "result_type": "number",
        "decimal_separator": "comma",
    }
    engine = _Engine()

    result = read_screen_text(
        _screenshot(),
        params,
        None,
        engine,
        resolve_value=_literal(params),
        image_names=[],
    )

    assert result.value == 12.5
    assert result.prepared.box.width == 100
    assert result.prepared.box.height == 60
    assert engine.crops == [(100, 60)]


def test_shared_reader_scales_fixed_pixel_region() -> None:
    params = {
        "coordinate_unit": "pixels",
        "start_x": 10,
        "start_y": 10,
        "end_x": 40,
        "end_y": 30,
    }
    engine = _Engine()

    result = read_screen_text(
        _screenshot(),
        params,
        None,
        engine,
        resolve_value=_literal(params),
        image_names=[],
        pixel_scale=(2.0, 1.5),
    )

    assert (result.prepared.box.left, result.prepared.box.top) == (20, 15)
    assert engine.crops == [(60, 30)]


def test_shared_reader_reports_the_matched_anchor_box(
    monkeypatch,
) -> None:
    params = {
        "region_mode": "image_anchor",
        "coordinate_unit": "pixels",
        "anchor": "top_right",
        "offset_x": 5,
        "offset_y": 2,
        "region_width": 50,
        "region_height": 20,
    }
    monkeypatch.setattr(
        screen_text_reading.FindImageService,
        "find",
        lambda *_args, **_kwargs: MatchResult(20, 20, 30, 10, 0.99),
    )

    result = read_screen_text(
        _screenshot(),
        params,
        _Database(),
        _Engine(),
        resolve_value=_literal(params),
        image_names=["marker"],
    )

    assert result.prepared.matched_image == "marker"
    assert result.prepared.matched_box is not None
    assert result.prepared.box.left == 55
    assert result.prepared.box.top == 22


def test_shared_reader_applies_optional_preprocessing_before_ocr() -> None:
    params = {
        "coordinate_unit": "pixels",
        "start_x": 0,
        "start_y": 0,
        "end_x": 20,
        "end_y": 10,
    }
    engine = _Engine()

    result = read_screen_text(
        _screenshot(),
        params,
        None,
        engine,
        resolve_value=_literal(params),
        image_names=[],
        preprocess_png=lambda png: preprocess_screen_text_png(png, "upscale_contrast"),
    )

    assert result.prepared.box.width == 20
    assert engine.crops == [(40, 20)]


def test_single_value_duration_uses_provider_hints_and_first_match() -> None:
    params = {
        "coordinate_unit": "pixels",
        "start_x": 0,
        "start_y": 0,
        "end_x": 20,
        "end_y": 10,
        "result_type": "duration_ms",
        "duration_format": "minutes_seconds",
        "recognition_mode": "single_value",
        "value_match_mode": "first_match",
    }
    engine = _ConfigurableEngine()

    result = read_screen_text(
        _screenshot(),
        params,
        None,
        engine,
        resolve_value=_literal(params),
        image_names=[],
    )

    assert result.value == 0
    assert engine.options == [
        OcrRecognitionOptions(
            page_segmentation_mode=7,
            character_whitelist="0123456789:",
        )
    ]


def test_single_value_falls_back_for_basic_ocr_provider() -> None:
    params = {
        "coordinate_unit": "pixels",
        "start_x": 0,
        "start_y": 0,
        "end_x": 20,
        "end_y": 10,
        "result_type": "number",
        "decimal_separator": "comma",
        "recognition_mode": "single_value",
    }
    engine = _Engine()

    result = read_screen_text(
        _screenshot(),
        params,
        None,
        engine,
        resolve_value=_literal(params),
        image_names=[],
    )

    assert result.value == 12.5


def test_saved_preprocessing_profile_is_applied_by_shared_reader() -> None:
    params = {
        "coordinate_unit": "pixels",
        "start_x": 0,
        "start_y": 0,
        "end_x": 20,
        "end_y": 10,
        "result_type": "duration_ms",
        "recognition_mode": "single_value",
        "preprocessing": "bright_color_text",
    }
    engine = _ConfigurableEngine("0:00")

    result = read_screen_text(
        _screenshot(),
        params,
        None,
        engine,
        resolve_value=_literal(params),
        image_names=[],
    )

    assert result.value == 0
    assert engine.crops == [(80, 40)]
