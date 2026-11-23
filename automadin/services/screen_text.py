"""Deterministic conversion of OCR text into flow scalar values.

The OCR engine is intentionally not part of this module.  Engines return text;
this boundary applies the saved command's result type and decimal-separator
policy before a Variable is changed.
"""

from __future__ import annotations

import math
import re

from automadin.models.typed_value import (
    ValueCoercionError,
    ValueType,
    coerce_typed_value,
)
from automadin.services.screen_text_options import (
    DecimalSeparator,
    DurationFormat,
    ValueMatchMode,
)

_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)$")
_TWO_PART_CLOCK_PATTERN = re.compile(r"^(?P<first>\d+):(?P<second>\d{2})$")
_THREE_PART_CLOCK_PATTERN = re.compile(
    r"^(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})$"
)
_INTEGER_TOKEN_PATTERN = re.compile(r"(?<![\w.:,])[+-]?\d+(?![\w.:,])")
_NUMBER_TOKEN_PATTERN = re.compile(
    r"(?<![\w.:,])[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?![\w.:,])"
)
_DURATION_UNIT_TOKEN_PATTERN = re.compile(
    r"(?<![\w.])\+?(?:\d+(?:\.\d*)?|\.\d+)\s*"
    r"(?:ms|milliseconds?|s|sec|seconds?|m|min|minutes?)\b",
    re.IGNORECASE,
)


class ScreenTextParseError(ValueError):
    """OCR text cannot be represented by the requested result contract."""


def _normalized_text(raw_text: object, *, allow_empty: bool) -> str:
    if not isinstance(raw_text, str):
        raise ScreenTextParseError("Screen text recognition must return text.")
    text = raw_text.strip()
    if not text and not allow_empty:
        raise ScreenTextParseError("Screen text recognition found no text.")
    return text


def _parse_integer(text: str) -> int:
    if not _INTEGER_PATTERN.fullmatch(text):
        raise ScreenTextParseError(
            f"Recognized text '{text}' is not an unambiguous integer."
        )
    return int(text)


def _parse_number(text: str, separator: DecimalSeparator) -> float:
    if not _DECIMAL_PATTERN.fullmatch(text):
        raise ScreenTextParseError(
            f"Recognized text '{text}' is not an unambiguous number."
        )
    has_dot = "." in text
    has_comma = "," in text
    if has_dot and has_comma:
        raise ScreenTextParseError(
            "Recognized number uses both '.' and ','; choose Dot or Comma "
            "decimal parsing explicitly."
        )
    if separator is DecimalSeparator.DOT and has_comma:
        raise ScreenTextParseError("Comma decimal text requires Comma parsing.")
    if separator is DecimalSeparator.COMMA and has_dot:
        raise ScreenTextParseError("Dot decimal text requires Dot parsing.")
    normalized = text.replace(",", ".")
    try:
        result = float(normalized)
    except ValueError as exc:  # pragma: no cover - guarded by the regex
        raise ScreenTextParseError("Recognized text is not a number.") from exc
    if not math.isfinite(result):
        raise ScreenTextParseError("Recognized number must be finite.")
    return result


def _parse_duration(text: str, duration_format: DurationFormat) -> int:
    if ":" not in text:
        try:
            return int(
                coerce_typed_value(
                    ValueType.DURATION_MS,
                    text,
                    subject="Recognized text",
                )
            )
        except ValueCoercionError as exc:
            raise ScreenTextParseError(str(exc)) from exc

    if duration_format is DurationFormat.HOURS_MINUTES_SECONDS:
        match = _THREE_PART_CLOCK_PATTERN.fullmatch(text)
        label = "hours:minutes:seconds"
        if match is not None:
            hours = int(match.group("hours"))
            minutes = int(match.group("minutes"))
            seconds = int(match.group("seconds"))
            valid = minutes < 60 and seconds < 60
            milliseconds = ((hours * 60 + minutes) * 60 + seconds) * 1_000
        else:
            valid = False
            milliseconds = 0
    else:
        match = _TWO_PART_CLOCK_PATTERN.fullmatch(text)
        label = (
            "minutes:seconds"
            if duration_format is DurationFormat.MINUTES_SECONDS
            else "hours:minutes"
        )
        if match is not None:
            first = int(match.group("first"))
            second = int(match.group("second"))
            valid = second < 60
            milliseconds = (
                (first * 60 + second) * 1_000
                if duration_format is DurationFormat.MINUTES_SECONDS
                else (first * 60 + second) * 60_000
            )
        else:
            valid = False
            milliseconds = 0
    if not valid:
        raise ScreenTextParseError(
            f"Recognized text '{text}' is not a valid {label} duration."
        )
    try:
        return int(
            coerce_typed_value(
                ValueType.DURATION_MS,
                milliseconds,
                subject="Recognized duration",
            )
        )
    except ValueCoercionError as exc:
        raise ScreenTextParseError(str(exc)) from exc


def _first_clock_match(text: str, duration_format: DurationFormat) -> str | None:
    if duration_format is DurationFormat.HOURS_MINUTES_SECONDS:
        pattern = re.compile(
            r"(?<![\d:])\d+:[0-5]\d:[0-5]\d(?![\d:])"
        )
    else:
        pattern = re.compile(r"(?<![\d:])\d+:[0-5]\d(?![\d:])")
    match = pattern.search(text)
    return match.group(0) if match is not None else None


def _first_typed_match(
    text: str,
    result_type: ValueType,
    duration_format: DurationFormat,
) -> str:
    if result_type is ValueType.TEXT:
        for line in text.splitlines():
            candidate = line.strip()
            if candidate:
                return candidate
    elif result_type is ValueType.INTEGER:
        match = _INTEGER_TOKEN_PATTERN.search(text)
        if match is not None:
            return match.group(0)
    elif result_type is ValueType.NUMBER:
        match = _NUMBER_TOKEN_PATTERN.search(text)
        if match is not None:
            return match.group(0)
    elif result_type is ValueType.DURATION_MS:
        candidates: list[tuple[int, str]] = []
        clock = _first_clock_match(text, duration_format)
        if clock is not None:
            candidates.append((text.find(clock), clock))
        unit_match = _DURATION_UNIT_TOKEN_PATTERN.search(text)
        if unit_match is not None:
            candidates.append((unit_match.start(), unit_match.group(0)))
        bare_match = re.search(r"(?<![\w.:,])\d+(?![\w.:,])", text)
        if bare_match is not None:
            candidates.append((bare_match.start(), bare_match.group(0)))
        if candidates:
            return min(candidates, key=lambda item: item[0])[1]
    raise ScreenTextParseError(
        f"Recognized text contains no valid {result_type.value} value."
    )


def parse_screen_text(
    raw_text: object,
    result_type: ValueType,
    *,
    decimal_separator: DecimalSeparator | str = DecimalSeparator.AUTO,
    duration_format: DurationFormat | str = DurationFormat.MINUTES_SECONDS,
    value_match_mode: ValueMatchMode | str = ValueMatchMode.EXACT,
    allow_empty: bool = False,
) -> str | int | float:
    """Normalize OCR text and convert it to the requested flow scalar type."""
    try:
        separator = (
            decimal_separator
            if isinstance(decimal_separator, DecimalSeparator)
            else DecimalSeparator(str(decimal_separator).casefold())
        )
    except ValueError as exc:
        raise ScreenTextParseError(
            "Decimal parsing must be Auto, Dot, or Comma."
        ) from exc
    try:
        clock_format = (
            duration_format
            if isinstance(duration_format, DurationFormat)
            else DurationFormat(str(duration_format).casefold())
        )
    except ValueError as exc:
        raise ScreenTextParseError(
            "Duration format must be Minutes:seconds, Hours:minutes, or "
            "Hours:minutes:seconds."
        ) from exc
    try:
        match_mode = (
            value_match_mode
            if isinstance(value_match_mode, ValueMatchMode)
            else ValueMatchMode(str(value_match_mode).casefold())
        )
    except ValueError as exc:
        raise ScreenTextParseError(
            "Value matching must be Exact output or First matching value."
        ) from exc

    text = _normalized_text(raw_text, allow_empty=allow_empty)
    if match_mode is ValueMatchMode.FIRST_MATCH and text:
        text = _first_typed_match(text, result_type, clock_format)
    if result_type is ValueType.TEXT:
        return text
    if not text:
        raise ScreenTextParseError(
            f"Screen text result type '{result_type.value}' cannot be empty."
        )
    if result_type is ValueType.INTEGER:
        return _parse_integer(text)
    if result_type is ValueType.NUMBER:
        return _parse_number(text, separator)
    if result_type is ValueType.DURATION_MS:
        return _parse_duration(text, clock_format)
    raise ScreenTextParseError(
        f"Screen text result type '{result_type.value}' is not supported."
    )


__all__ = [
    "DecimalSeparator",
    "DurationFormat",
    "ScreenTextParseError",
    "ValueMatchMode",
    "parse_screen_text",
]
