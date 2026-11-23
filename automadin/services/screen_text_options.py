"""Shared option contracts for screen-text commands and diagnostic tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from automadin.models.typed_value import ValueType


@dataclass(frozen=True, slots=True)
class ScreenTextChoice:
    value: str
    label: str
    description: str = ""


class DecimalSeparator(str, Enum):
    AUTO = "auto"
    DOT = "dot"
    COMMA = "comma"


class DurationFormat(str, Enum):
    MINUTES_SECONDS = "minutes_seconds"
    HOURS_MINUTES = "hours_minutes"
    HOURS_MINUTES_SECONDS = "hours_minutes_seconds"


class ValueMatchMode(str, Enum):
    EXACT = "exact"
    FIRST_MATCH = "first_match"


class OcrRecognitionMode(str, Enum):
    TEXT_BLOCK = "text_block"
    SINGLE_VALUE = "single_value"


SCREEN_TEXT_RESULT_TYPE_CHOICES = (
    ScreenTextChoice(ValueType.TEXT.value, "Text"),
    ScreenTextChoice(ValueType.INTEGER.value, "Whole number"),
    ScreenTextChoice(ValueType.NUMBER.value, "Decimal number"),
    ScreenTextChoice(ValueType.DURATION_MS.value, "Duration"),
)

SCREEN_TEXT_DECIMAL_SEPARATOR_CHOICES = (
    ScreenTextChoice(DecimalSeparator.AUTO.value, "Auto-detect"),
    ScreenTextChoice(DecimalSeparator.DOT.value, "Dot (.)"),
    ScreenTextChoice(DecimalSeparator.COMMA.value, "Comma (,)"),
)

SCREEN_TEXT_DURATION_FORMAT_CHOICES = (
    ScreenTextChoice(DurationFormat.MINUTES_SECONDS.value, "Minutes : seconds"),
    ScreenTextChoice(DurationFormat.HOURS_MINUTES.value, "Hours : minutes"),
    ScreenTextChoice(
        DurationFormat.HOURS_MINUTES_SECONDS.value,
        "Hours : minutes : seconds",
    ),
)

SCREEN_TEXT_RECOGNITION_MODE_CHOICES = (
    ScreenTextChoice(
        OcrRecognitionMode.TEXT_BLOCK.value,
        "Text block",
        "Recognize one or more lines of unrestricted text.",
    ),
    ScreenTextChoice(
        OcrRecognitionMode.SINGLE_VALUE.value,
        "Single value",
        "Treat the crop as one line and restrict typed numeric characters.",
    ),
)

SCREEN_TEXT_VALUE_MATCH_MODE_CHOICES = (
    ScreenTextChoice(
        ValueMatchMode.EXACT.value,
        "Exact output",
        "Require the complete OCR output to match the selected result type.",
    ),
    ScreenTextChoice(
        ValueMatchMode.FIRST_MATCH.value,
        "First matching value",
        "Extract the first valid typed value and ignore surrounding OCR noise.",
    ),
)


def choice_values(choices: tuple[ScreenTextChoice, ...]) -> list[str]:
    return [choice.value for choice in choices]


def choice_labels(choices: tuple[ScreenTextChoice, ...]) -> dict[str, str]:
    return {choice.value: choice.label for choice in choices}


def hydrate_screen_text_command_metadata(entries: object) -> None:
    """Inject shared choices into the Read Screen Text command definition."""
    if not isinstance(entries, list):
        return
    from automadin.services.screen_text_preprocessing import (
        SCREEN_TEXT_PREPROCESSING_PROFILES,
    )

    preprocessing_choices = tuple(
        ScreenTextChoice(profile.key, profile.label, profile.description)
        for profile in SCREEN_TEXT_PREPROCESSING_PROFILES
    )
    choice_contracts = {
        "result_type": (SCREEN_TEXT_RESULT_TYPE_CHOICES, ValueType.TEXT.value),
        "recognition_mode": (
            SCREEN_TEXT_RECOGNITION_MODE_CHOICES,
            OcrRecognitionMode.TEXT_BLOCK.value,
        ),
        "value_match_mode": (
            SCREEN_TEXT_VALUE_MATCH_MODE_CHOICES,
            ValueMatchMode.EXACT.value,
        ),
        "decimal_separator": (
            SCREEN_TEXT_DECIMAL_SEPARATOR_CHOICES,
            DecimalSeparator.AUTO.value,
        ),
        "duration_format": (
            SCREEN_TEXT_DURATION_FORMAT_CHOICES,
            DurationFormat.MINUTES_SECONDS.value,
        ),
        "preprocessing": (preprocessing_choices, "original"),
    }
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") != "read_screen_text":
            continue
        params = entry.get("params")
        if not isinstance(params, list):
            return
        for param in params:
            if not isinstance(param, dict):
                continue
            name = param.get("name")
            contract = choice_contracts.get(name)
            if contract is not None:
                choices, default = contract
                param["options"] = choice_values(choices)
                param["option_labels"] = choice_labels(choices)
                param["default"] = default
            if name == "variable":
                param["allowed_value_types"] = choice_values(
                    SCREEN_TEXT_RESULT_TYPE_CHOICES
                )
        return


__all__ = [
    "DecimalSeparator",
    "DurationFormat",
    "OcrRecognitionMode",
    "SCREEN_TEXT_DECIMAL_SEPARATOR_CHOICES",
    "SCREEN_TEXT_DURATION_FORMAT_CHOICES",
    "SCREEN_TEXT_RECOGNITION_MODE_CHOICES",
    "SCREEN_TEXT_RESULT_TYPE_CHOICES",
    "SCREEN_TEXT_VALUE_MATCH_MODE_CHOICES",
    "ScreenTextChoice",
    "ValueMatchMode",
    "choice_labels",
    "choice_values",
    "hydrate_screen_text_command_metadata",
]
