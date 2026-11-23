from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from automadin.execution.context import FlowContext
from automadin.execution.flow_values import FlowInvocationValues
from automadin.execution.handlers.detection import handle_read_screen_text
from automadin.models.device import DeviceSession
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow, FlowCommand
from automadin.models.flow_value import FlowVariableDefinition
from automadin.models.typed_value import ValueType
from automadin.services.find_image import MatchResult
from automadin.services.flow_preflight import preflight_flow
from automadin.services.screen_text import (
    DecimalSeparator,
    DurationFormat,
    ScreenTextParseError,
    ValueMatchMode,
    parse_screen_text,
)


class _FakeOcrEngine:
    def __init__(self, recognize) -> None:
        self._recognize = recognize

    def recognize_png(self, cropped_png: bytes) -> str:
        return self._recognize(cropped_png)


def test_text_result_trims_outer_whitespace_but_preserves_lines() -> None:
    assert parse_screen_text("  total\n 42  ", ValueType.TEXT) == "total\n 42"


def test_text_result_can_explicitly_accept_empty_text() -> None:
    assert parse_screen_text("  ", ValueType.TEXT, allow_empty=True) == ""


def test_integer_result_requires_an_unambiguous_integer() -> None:
    assert parse_screen_text(" -0042 ", ValueType.INTEGER) == -42
    with pytest.raises(ScreenTextParseError, match="unambiguous integer"):
        parse_screen_text("42.0", ValueType.INTEGER)


@pytest.mark.parametrize(
    ("raw", "separator", "expected"),
    [
        ("12.5", DecimalSeparator.AUTO, 12.5),
        ("12,5", DecimalSeparator.AUTO, 12.5),
        ("12,5", DecimalSeparator.COMMA, 12.5),
        ("12.5", DecimalSeparator.DOT, 12.5),
    ],
)
def test_number_result_supports_explicit_and_unambiguous_decimal_modes(
    raw: str, separator: DecimalSeparator, expected: float
) -> None:
    assert parse_screen_text(raw, ValueType.NUMBER, decimal_separator=separator) == expected


def test_number_result_rejects_mixed_or_wrong_decimal_separator() -> None:
    with pytest.raises(ScreenTextParseError, match="unambiguous number"):
        parse_screen_text("1,234.5", ValueType.NUMBER)
    with pytest.raises(ScreenTextParseError, match="Comma"):
        parse_screen_text("12,5", ValueType.NUMBER, decimal_separator=DecimalSeparator.DOT)


def test_numeric_result_never_accepts_empty_text() -> None:
    with pytest.raises(ScreenTextParseError, match="cannot be empty"):
        parse_screen_text("", ValueType.NUMBER, allow_empty=True)


@pytest.mark.parametrize(
    ("raw", "duration_format", "expected"),
    [
        ("3:15", DurationFormat.MINUTES_SECONDS, 195_000),
        ("03:15", DurationFormat.MINUTES_SECONDS, 195_000),
        ("3:15", DurationFormat.HOURS_MINUTES, 11_700_000),
        ("1:03:15", DurationFormat.HOURS_MINUTES_SECONDS, 3_795_000),
        ("2 min", DurationFormat.HOURS_MINUTES, 120_000),
    ],
)
def test_duration_result_uses_explicit_clock_format(
    raw: str,
    duration_format: DurationFormat,
    expected: int,
) -> None:
    assert (
        parse_screen_text(
            raw,
            ValueType.DURATION_MS,
            duration_format=duration_format,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("raw", "duration_format"),
    [
        ("3:75", DurationFormat.MINUTES_SECONDS),
        ("1:60:00", DurationFormat.HOURS_MINUTES_SECONDS),
        ("3:15", DurationFormat.HOURS_MINUTES_SECONDS),
        ("three minutes", DurationFormat.MINUTES_SECONDS),
    ],
)
def test_duration_result_rejects_wrong_clock_shape(
    raw: str,
    duration_format: DurationFormat,
) -> None:
    with pytest.raises(ScreenTextParseError, match="duration"):
        parse_screen_text(
            raw,
            ValueType.DURATION_MS,
            duration_format=duration_format,
        )


@pytest.mark.parametrize(
    ("raw", "result_type", "expected"),
    [
        ("noise\n0:00\nneee", ValueType.DURATION_MS, 0),
        ("score: 42\nnoise", ValueType.INTEGER, 42),
        ("value = 12,5 extra", ValueType.NUMBER, 12.5),
        ("first line\nsecond line", ValueType.TEXT, "first line"),
    ],
)
def test_first_match_extracts_typed_value_from_ocr_noise(
    raw: str,
    result_type: ValueType,
    expected: object,
) -> None:
    assert (
        parse_screen_text(
            raw,
            result_type,
            decimal_separator=DecimalSeparator.COMMA,
            value_match_mode=ValueMatchMode.FIRST_MATCH,
        )
        == expected
    )


def test_exact_match_still_rejects_surrounding_duration_noise() -> None:
    with pytest.raises(ScreenTextParseError, match="duration"):
        parse_screen_text("0:00\nneee", ValueType.DURATION_MS)


def test_unknown_decimal_mode_is_rejected() -> None:
    with pytest.raises(ScreenTextParseError, match="Auto, Dot, or Comma"):
        parse_screen_text("1.5", ValueType.NUMBER, decimal_separator="grouped")


def test_read_screen_text_crops_region_and_writes_typed_variable() -> None:
    variable_id = "30000000-0000-4000-8000-000000000001"
    flow = AutomationFlow(
        "Read text",
        [],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=[FlowVariableDefinition("amount", ValueType.NUMBER, 0, id=variable_id)],
    )
    values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
    seen_sizes: list[tuple[int, int]] = []

    def recognizer(cropped_png: bytes) -> str:
        with Image.open(BytesIO(cropped_png)) as cropped:
            seen_sizes.append(cropped.size)
        return "12,5"

    context = FlowContext(
        DeviceSession(data={"screen_text_ocr_engine": _FakeOcrEngine(recognizer)}),
        flow_values=values,
    )
    logs: list[str] = []
    context.listener = SimpleNamespace(on_log=logs.append)
    screenshot = BytesIO()
    Image.new("RGBA", (200, 100), "white").save(screenshot, format="PNG")
    command = FlowCommand(
        "read_screen_text",
        params={
            "coordinate_unit": "percent",
            "start_x": 10,
            "start_y": 20,
            "end_x": 60,
            "end_y": 80,
            "result_type": "number",
            "decimal_separator": "comma",
            "variable": variable_id,
            "log_values": True,
        },
    )

    with patch(
        "automadin.execution.handlers.detection._take_screenshot_for_device",
        return_value=screenshot.getvalue(),
    ):
        handle_read_screen_text(command, context)

    assert seen_sizes == [(100, 60)]
    assert context.get_flow_variable(variable_id) == 12.5
    assert logs == [
        'Read Screen Text: OCR raw="12,5"',
        "Read Screen Text: region=(20, 20)–(120, 80); parsed number=12.5; "
        "stored in Variable 'amount'",
    ]

    command.params["log_values"] = False
    logs.clear()
    with patch(
        "automadin.execution.handlers.detection._take_screenshot_for_device",
        return_value=screenshot.getvalue(),
    ):
        handle_read_screen_text(command, context)

    assert logs == [
        "Read Screen Text: region=(20, 20)–(120, 80); parsed number; "
        "value hidden; stored in Variable 'amount'"
    ]
    assert "12,5" not in logs[0]
    assert variable_id not in logs[0]


def test_read_screen_text_logs_raw_ocr_before_parse_failure() -> None:
    variable_id = "30000000-0000-4000-8000-000000000005"
    flow = AutomationFlow(
        "Read invalid number",
        [],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=[FlowVariableDefinition("amount", ValueType.NUMBER, 0, id=variable_id)],
    )
    context = FlowContext(
        DeviceSession(
            data={"screen_text_ocr_engine": _FakeOcrEngine(lambda _png: "I2,S")}
        ),
        flow_values=FlowInvocationValues(flow.flow_id, flow.value_catalog, {}),
    )
    logs: list[str] = []
    context.listener = SimpleNamespace(on_log=logs.append)
    screenshot = BytesIO()
    Image.new("RGBA", (20, 20), "white").save(screenshot, format="PNG")
    command = FlowCommand(
        "read_screen_text",
        params={
            "start_x": 0,
            "start_y": 0,
            "end_x": 20,
            "end_y": 20,
            "result_type": "number",
            "variable": variable_id,
            "log_values": True,
        },
    )

    with (
        patch(
            "automadin.execution.handlers.detection._take_screenshot_for_device",
            return_value=screenshot.getvalue(),
        ),
        pytest.raises(ScreenTextParseError),
    ):
        handle_read_screen_text(command, context)

    assert logs == ['Read Screen Text: OCR raw="I2,S"']


def test_read_screen_text_writes_explicit_clock_duration() -> None:
    variable_id = "30000000-0000-4000-8000-000000000007"
    flow = AutomationFlow(
        "Read clock duration",
        [],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=[
            FlowVariableDefinition(
                "swipe_duration",
                ValueType.DURATION_MS,
                0,
                id=variable_id,
            )
        ],
    )
    context = FlowContext(
        DeviceSession(
            data={"screen_text_ocr_engine": _FakeOcrEngine(lambda _png: "3:15")}
        ),
        flow_values=FlowInvocationValues(flow.flow_id, flow.value_catalog, {}),
    )
    screenshot = BytesIO()
    Image.new("RGBA", (20, 20), "white").save(screenshot, format="PNG")
    command = FlowCommand(
        "read_screen_text",
        params={
            "start_x": 0,
            "start_y": 0,
            "end_x": 20,
            "end_y": 20,
            "result_type": "duration_ms",
            "duration_format": "minutes_seconds",
            "variable": variable_id,
        },
    )

    with patch(
        "automadin.execution.handlers.detection._take_screenshot_for_device",
        return_value=screenshot.getvalue(),
    ):
        handle_read_screen_text(command, context)

    assert context.get_flow_variable(variable_id) == 195_000


def test_read_screen_text_uses_first_matched_image_as_region_anchor() -> None:
    variable_id = "30000000-0000-4000-8000-000000000003"
    flow = AutomationFlow(
        "Read anchored text",
        [],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=[FlowVariableDefinition("label", ValueType.TEXT, "", id=variable_id)],
    )
    values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
    seen_sizes: list[tuple[int, int]] = []

    def recognizer(cropped_png: bytes) -> str:
        with Image.open(BytesIO(cropped_png)) as cropped:
            seen_sizes.append(cropped.size)
        return "anchored"

    context = FlowContext(
        DeviceSession(data={"screen_text_ocr_engine": _FakeOcrEngine(recognizer)}),
        flow_values=values,
    )
    context._executor = SimpleNamespace(
        _db=SimpleNamespace(load_image=lambda _name: b"template")
    )
    screenshot = BytesIO()
    Image.new("RGBA", (200, 100), "white").save(screenshot, format="PNG")
    command = FlowCommand(
        "read_screen_text",
        params={
            "region_mode": "image_anchor",
            "images": ["marker"],
            "coordinate_unit": "pixels",
            "anchor": "top_right",
            "offset_x": 5,
            "offset_y": 2,
            "region_width": 50,
            "region_height": 20,
            "variable": variable_id,
        },
    )

    with (
        patch(
            "automadin.execution.handlers.detection._take_screenshot_for_device",
            return_value=screenshot.getvalue(),
        ),
        patch(
            "automadin.services.screen_text_reading.FindImageService.find",
            return_value=MatchResult(20, 20, 30, 10, 0.99),
        ),
    ):
        handle_read_screen_text(command, context)

    assert seen_sizes == [(50, 20)]
    assert context.get_flow_variable(variable_id) == "anchored"


def test_read_screen_text_requires_an_injected_ocr_engine() -> None:
    context = FlowContext(DeviceSession())
    command = FlowCommand(
        "read_screen_text",
        params={"start_x": 0, "start_y": 0, "end_x": 1, "end_y": 1},
    )
    with (
        patch(
            "automadin.execution.handlers.detection._take_screenshot_for_device",
            return_value=b"not-png",
        ),
        pytest.raises(RuntimeError, match="invalid"),
    ):
        handle_read_screen_text(command, context)


def test_read_screen_text_preflight_matches_result_type_to_target_variable() -> None:
    variable_id = "30000000-0000-4000-8000-000000000002"
    command = FlowCommand(
        "read_screen_text",
        params={
            "coordinate_unit": "percent",
            "start_x": 10,
            "start_y": 10,
            "end_x": 90,
            "end_y": 90,
            "result_type": "integer",
            "variable": variable_id,
        },
    )
    flow = AutomationFlow(
        "Read text preflight",
        [command],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=[FlowVariableDefinition("amount", ValueType.NUMBER, 0, id=variable_id)],
    )

    report = preflight_flow(flow)

    assert any(
        issue.code == "OUTPUT_VARIABLE_TYPE_MISMATCH" for issue in report.issues
    )


def test_read_screen_text_duration_matches_duration_variable() -> None:
    variable_id = "30000000-0000-4000-8000-000000000006"
    flow = AutomationFlow(
        "Read duration",
        [
            FlowCommand(
                "read_screen_text",
                params={
                    "start_x": 0,
                    "start_y": 0,
                    "end_x": 20,
                    "end_y": 20,
                    "result_type": "duration_ms",
                    "variable": variable_id,
                },
            )
        ],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=[
            FlowVariableDefinition(
                "remaining_time",
                ValueType.DURATION_MS,
                0,
                id=variable_id,
            )
        ],
    )

    report = preflight_flow(flow)

    assert not any(
        issue.code == "OUTPUT_VARIABLE_TYPE_MISMATCH" for issue in report.issues
    )


def test_image_anchored_preflight_requires_anchor_fields_not_fixed_coordinates() -> None:
    variable_id = "30000000-0000-4000-8000-000000000004"
    flow = AutomationFlow(
        "Read anchored preflight",
        [
            FlowCommand(
                "read_screen_text",
                params={
                    "region_mode": "image_anchor",
                    "images": ["marker"],
                    "region_width": 50,
                    "region_height": 20,
                    "variable": variable_id,
                },
            )
        ],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        variables=[FlowVariableDefinition("label", ValueType.TEXT, "", id=variable_id)],
    )

    report = preflight_flow(flow)

    assert not any(
        issue.code == "MISSING_PARAMETER"
        and issue.location.parameter in {"start_x", "start_y", "end_x", "end_y"}
        for issue in report.issues
    )
