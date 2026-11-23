"""Shared screenshot-region OCR preparation for execution and editor preview."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from automadin.models.typed_value import TypedValue, ValueType
from automadin.services.find_image import FindImageService
from automadin.services.find_image import MatchResult
from automadin.services.ocr import (
    ConfigurableScreenTextOcrEngine,
    OcrEngineError,
    OcrRecognitionOptions,
    ScreenTextOcrEngine,
)
from automadin.services.screen_text import (
    parse_screen_text,
)
from automadin.services.screen_text_options import (
    DecimalSeparator,
    DurationFormat,
    OcrRecognitionMode,
    ValueMatchMode,
)
from automadin.services.screen_text_preprocessing import preprocess_screen_text_png
from automadin.services.screen_text_region import (
    ScreenTextBox,
    resolve_anchored_box,
    resolve_fixed_box,
)


class ScreenTextReadingError(RuntimeError):
    """A user-displayable failure while preparing or recognising screen text."""


_SINGLE_VALUE_WHITELISTS = {
    ValueType.INTEGER: "+-0123456789",
    ValueType.NUMBER: "+-0123456789.,",
    ValueType.DURATION_MS: "0123456789:",
}


@dataclass(frozen=True, slots=True)
class PreparedScreenText:
    """The resolved surface box and crop used by one OCR attempt."""

    box: ScreenTextBox
    crop_png: bytes
    matched_image: str | None = None
    matched_box: ScreenTextBox | None = None


@dataclass(frozen=True, slots=True)
class ScreenTextReadResult:
    """One completed OCR attempt, including the raw and typed values."""

    prepared: PreparedScreenText
    recognized_text: str
    value: TypedValue


def _as_number(value: object, *, parameter: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ScreenTextReadingError(
            f"read_screen_text: {parameter} must be a number"
        ) from exc


def prepare_screen_text(
    screenshot: bytes,
    params: dict[str, object],
    database: Any,
    *,
    resolve_value: Callable[[str, object, ValueType], object],
    image_names: list[str],
    preprocess_png: Callable[[bytes], bytes] | None = None,
    pixel_scale: tuple[float, float] = (1.0, 1.0),
    match_image: Callable[[str, bytes], MatchResult | None] | None = None,
) -> PreparedScreenText:
    """Resolve, validate, and crop one fixed or image-anchored OCR region."""
    from PIL import Image

    try:
        image = Image.open(BytesIO(screenshot)).convert("RGBA")
    except Exception as exc:
        raise ScreenTextReadingError(
            "read_screen_text: captured screenshot is invalid"
        ) from exc

    surface_size = image.size
    region_mode = str(params.get("region_mode", "fixed")).casefold()
    if region_mode == "fixed":
        pixel_unit = str(params.get("coordinate_unit", "pixels")).casefold() in {
            "pixel",
            "pixels",
            "px",
        }
        x_scale, y_scale = pixel_scale if pixel_unit else (1.0, 1.0)
        box = resolve_fixed_box(
            _as_number(
                resolve_value("start_x", 0, ValueType.NUMBER), parameter="Start X"
            ) * x_scale,
            _as_number(
                resolve_value("start_y", 0, ValueType.NUMBER), parameter="Start Y"
            ) * y_scale,
            _as_number(
                resolve_value("end_x", surface_size[0], ValueType.NUMBER),
                parameter="End X",
            ) * x_scale,
            _as_number(
                resolve_value("end_y", surface_size[1], ValueType.NUMBER),
                parameter="End Y",
            ) * y_scale,
            unit=params.get("coordinate_unit", "pixels"),
            surface_size=surface_size,
        )
        matched_name = None
        matched_box = None
    elif region_mode == "image_anchor":
        if not image_names:
            raise ScreenTextReadingError("read_screen_text: anchor images are required")
        if database is None:
            raise ScreenTextReadingError("read_screen_text: database is unavailable")
        matched_name = None
        matched_result = None
        service = FindImageService()
        for image_name in image_names:
            if match_image is not None:
                result = match_image(image_name, screenshot)
            else:
                template = database.load_image(image_name)
                if template is None:
                    continue
                result = service.find(
                    screenshot,
                    template,
                    _as_number(
                        resolve_value("threshold", 0.9, ValueType.NUMBER),
                        parameter="Anchor Threshold",
                    ),
                    bool(resolve_value("color_match", False, ValueType.BOOLEAN)),
                    _as_number(
                        resolve_value("color_tolerance", 0.3, ValueType.NUMBER),
                        parameter="Anchor Color Tolerance",
                    ),
                )
            if result is not None:
                matched_name = image_name
                matched_result = result
                break
        if matched_result is None:
            raise ScreenTextReadingError("read_screen_text: no anchor image matched")
        matched_box = ScreenTextBox(
            int(matched_result.x),
            int(matched_result.y),
            int(matched_result.x + matched_result.width),
            int(matched_result.y + matched_result.height),
        )
        pixel_unit = str(params.get("coordinate_unit", "pixels")).casefold() in {
            "pixel",
            "pixels",
            "px",
        }
        x_scale, y_scale = pixel_scale if pixel_unit else (1.0, 1.0)
        box = resolve_anchored_box(
            matched_box,
            anchor=params.get("anchor", "top_left"),
            offset_x=_as_number(
                resolve_value("offset_x", 0, ValueType.NUMBER), parameter="Offset X"
            ) * x_scale,
            offset_y=_as_number(
                resolve_value("offset_y", 0, ValueType.NUMBER), parameter="Offset Y"
            ) * y_scale,
            width=_as_number(
                resolve_value("region_width", 0, ValueType.NUMBER),
                parameter="Region Width",
            ) * x_scale,
            height=_as_number(
                resolve_value("region_height", 0, ValueType.NUMBER),
                parameter="Region Height",
            ) * y_scale,
            unit=params.get("coordinate_unit", "pixels"),
            surface_size=surface_size,
        )
    else:
        raise ScreenTextReadingError(
            "read_screen_text: region mode must be fixed or image_anchor"
        )
    crop = image.crop((box.left, box.top, box.right, box.bottom))
    crop_bytes = BytesIO()
    crop.save(crop_bytes, format="PNG")
    crop_png = crop_bytes.getvalue()
    if preprocess_png is not None:
        crop_png = preprocess_png(crop_png)
    return PreparedScreenText(box, crop_png, matched_name, matched_box)


def read_screen_text(
    screenshot: bytes,
    params: dict[str, object],
    database: Any,
    engine: ScreenTextOcrEngine,
    *,
    resolve_value: Callable[[str, object, ValueType], object],
    image_names: list[str],
    preprocess_png: Callable[[bytes], bytes] | None = None,
    pixel_scale: tuple[float, float] = (1.0, 1.0),
    match_image: Callable[[str, bytes], MatchResult | None] | None = None,
    on_recognized: Callable[[str], None] | None = None,
) -> ScreenTextReadResult:
    """Prepare the region, run the configured engine, and parse its typed result."""
    if preprocess_png is None:
        preprocessing = str(params.get("preprocessing", "original"))

        def selected_preprocessor(png: bytes) -> bytes:
            return preprocess_screen_text_png(png, preprocessing)

    else:
        selected_preprocessor = preprocess_png
    try:
        prepared = prepare_screen_text(
            screenshot,
            params,
            database,
            resolve_value=resolve_value,
            image_names=image_names,
            preprocess_png=selected_preprocessor,
            pixel_scale=pixel_scale,
            match_image=match_image,
        )
    except ValueError as exc:
        raise ScreenTextReadingError(f"read_screen_text: {exc}") from exc
    try:
        result_type = ValueType(str(params.get("result_type", "text")).casefold())
        decimal_separator = DecimalSeparator(
            str(params.get("decimal_separator", "auto")).casefold()
        )
        duration_format = DurationFormat(
            str(params.get("duration_format", "minutes_seconds")).casefold()
        )
        value_match_mode = ValueMatchMode(
            str(params.get("value_match_mode", "exact")).casefold()
        )
        recognition_mode = OcrRecognitionMode(
            str(params.get("recognition_mode", "text_block")).casefold()
        )
    except ValueError as exc:
        raise ScreenTextReadingError(
            "read_screen_text: result type or recognition/parsing option is invalid"
        ) from exc
    try:
        if (
            recognition_mode is OcrRecognitionMode.SINGLE_VALUE
            and isinstance(engine, ConfigurableScreenTextOcrEngine)
        ):
            recognized_text = engine.recognize_png_with_options(
                prepared.crop_png,
                OcrRecognitionOptions(
                    page_segmentation_mode=7,
                    character_whitelist=_SINGLE_VALUE_WHITELISTS.get(result_type),
                ),
            )
        else:
            recognized_text = engine.recognize_png(prepared.crop_png)
    except OcrEngineError as exc:
        raise ScreenTextReadingError(f"read_screen_text: {exc}") from exc
    if on_recognized is not None:
        on_recognized(recognized_text)
    allow_empty = bool(resolve_value("allow_empty", False, ValueType.BOOLEAN))
    value = parse_screen_text(
        recognized_text,
        result_type,
        decimal_separator=decimal_separator,
        duration_format=duration_format,
        value_match_mode=value_match_mode,
        allow_empty=allow_empty,
    )
    return ScreenTextReadResult(prepared, recognized_text, value)
