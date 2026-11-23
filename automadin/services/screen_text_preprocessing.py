"""Deterministic OCR crop transforms shared by commands and Vision Lab."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps


@dataclass(frozen=True, slots=True)
class ScreenTextPreprocessingProfile:
    key: str
    label: str
    description: str


SCREEN_TEXT_PREPROCESSING_PROFILES = (
    ScreenTextPreprocessingProfile(
        "original", "Original", "Use the captured pixels unchanged."
    ),
    ScreenTextPreprocessingProfile(
        "grayscale", "Grayscale", "Remove colour while preserving the captured size."
    ),
    ScreenTextPreprocessingProfile(
        "contrast",
        "Grayscale + contrast",
        "Convert to grayscale and stretch its contrast.",
    ),
    ScreenTextPreprocessingProfile(
        "upscale_contrast",
        "2× grayscale + contrast",
        "Stretch contrast, then enlarge the crop 2× for small text.",
    ),
    ScreenTextPreprocessingProfile(
        "bright_color_text",
        "4× bright color text",
        "Isolate bright saturated text and enlarge it without blurring pixel fonts.",
    ),
)

_PROFILE_KEYS = frozenset(profile.key for profile in SCREEN_TEXT_PREPROCESSING_PROFILES)


def preprocess_screen_text_png(png: bytes, profile: str = "original") -> bytes:
    """Return a PNG transformed by one named, deterministic OCR profile.

    ``original`` returns the exact input bytes. Transformed profiles use Pillow
    only and are deterministic in both command execution and Vision Lab.
    """

    key = str(profile).casefold()
    if key not in _PROFILE_KEYS:
        choices = ", ".join(sorted(_PROFILE_KEYS))
        raise ValueError(f"Unknown OCR preprocessing profile: {key!r}. Choose {choices}.")
    if key == "original":
        return png

    try:
        with Image.open(BytesIO(png)) as source:
            if key == "bright_color_text":
                rgb = source.convert("RGB")
                hsv = rgb.convert("HSV")
                image = Image.new("L", hsv.size)
                source_pixels = hsv.load()
                output_pixels = image.load()
                for y in range(hsv.height):
                    for x in range(hsv.width):
                        _hue, saturation, value = source_pixels[x, y]
                        output_pixels[x, y] = (
                            255 if saturation >= 80 and value >= 170 else 0
                        )
                image = image.resize(
                    (image.width * 4, image.height * 4),
                    Image.Resampling.NEAREST,
                )
            else:
                image = source.convert("L")
            if key in {"contrast", "upscale_contrast"}:
                image = ImageOps.autocontrast(image)
            if key == "upscale_contrast":
                image = image.resize(
                    (image.width * 2, image.height * 2),
                    Image.Resampling.LANCZOS,
                )
            output = BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
    except Exception as exc:
        raise ValueError("OCR preprocessing requires a valid PNG image.") from exc
