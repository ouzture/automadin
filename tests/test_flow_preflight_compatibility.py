from __future__ import annotations

from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.services.flow_preflight import (
    PreflightMode,
    PreflightSeverity,
    TargetKind,
    preflight_flow,
)


class FakeImageDatabase:
    def __init__(self, *images: str) -> None:
        self._images = images

    def list_images(self) -> list[dict[str, str]]:
        return [{"name": image} for image in self._images]


def _report(
    command_id: str,
    params: dict[str, object],
    *,
    enabled: bool = True,
    db: object | None = None,
):
    command = FlowCommand(command_id, params=params, enabled=enabled)
    return preflight_flow(
        AutomationFlow("compatibility", [command]),
        db=db,
    )


def _assert_runtime_compatible(report) -> None:
    assert not report.errors, report.format_text()
    assert not [issue for issue in report.issues if issue.code == "UNKNOWN_PARAMETER"], (
        report.format_text()
    )


def test_runtime_compatibility_alias_matrix_is_accepted_by_preflight() -> None:
    cases = (
        ("win_key", {"key": "Escape"}, None),
        ("press_keycode", {"keycode": 66}, None),
        ("for_each", {"items": "alpha,beta,gamma"}, None),
        ("for_each", {"items": "alpha\nbeta\ngamma"}, None),
        (
            "swipe",
            {
                "startXPercent": 0.1,
                "startYPercent": 0.2,
                "endXPercent": 0.8,
                "endYPercent": 0.9,
                "duration": 450,
            },
            None,
        ),
        ("click_position", {"x": 10, "y": 20, "clickType": "double"}, None),
        ("type_text", {"text": "hello", "minDelay": 5, "maxDelay": 15}, None),
        ("type_text", {"text": "hello", "delay_ms": 10}, None),
        (
            "input_fill",
            {
                "selector": "#query",
                "value": "hello",
                "minDelay": 5,
                "maxDelay": 15,
            },
            None,
        ),
        ("input_fill", {"selector": "#query", "value": ""}, None),
        ("random_wait", {"minMs": 1000, "maxMs": 2000}, None),
        ("win_type", {"text": "hello", "delay_ms": 10}, None),
        (
            "win_if_image",
            {"image_name": "known", "clickIfFound": True},
            FakeImageDatabase("known"),
        ),
        ("find_image", {"image_name": "known"}, FakeImageDatabase("known")),
    )
    for command_id, params, database in cases:
        report = _report(command_id, params, db=database)
        try:
            _assert_runtime_compatible(report)
        except AssertionError as exc:
            raise AssertionError(f"Compatibility case failed: {command_id}") from exc


def test_empty_canonical_for_each_values_are_not_masked_by_legacy_items() -> None:
    report = _report(
        "for_each",
        {"values": [], "items": "alpha,beta"},
    )

    assert [
        issue
        for issue in report.errors
        if issue.code == "EMPTY_IMAGE_LIST" or issue.code == "MISSING_PARAMETER"
    ], report.format_text()


def test_android_type_text_warns_that_character_delays_are_ignored() -> None:
    report = preflight_flow(
        AutomationFlow(
            "compatibility",
            [
                FlowCommand(
                    "type_text",
                    params={"text": "hello", "min_delay": 10, "max_delay": 20},
                )
            ],
        ),
        target=TargetKind.ANDROID,
        mode=PreflightMode.EXECUTION,
    )

    issues = [issue for issue in report.warnings if issue.code == "PARAMETER_NOT_APPLICABLE"]
    assert len(issues) == 1, report.format_text()
    assert report.can_execute


def test_unknown_parameter_typo_is_an_error_at_the_parameter() -> None:
    report = _report("wait", {"ms": 25, "mss": 25})

    issues = [
        issue
        for issue in report.issues
        if issue.code == "UNKNOWN_PARAMETER" and issue.location.parameter == "mss"
    ]
    assert len(issues) == 1, report.format_text()
    assert issues[0].severity is PreflightSeverity.ERROR
    assert not report.can_execute


def test_unknown_parameter_on_disabled_command_is_informational() -> None:
    report = _report("wait", {"ms": 25, "mss": 25}, enabled=False)

    issues = [
        issue
        for issue in report.issues
        if issue.code == "UNKNOWN_PARAMETER" and issue.location.parameter == "mss"
    ]
    assert len(issues) == 1, report.format_text()
    assert issues[0].severity is PreflightSeverity.INFO
    assert report.can_execute


def test_defaultish_unknown_parameter_is_reported_without_blocking() -> None:
    report = _report("wait", {"ms": 25, "pauseMs": 0})

    issues = [
        issue
        for issue in report.issues
        if issue.code == "UNKNOWN_PARAMETER" and issue.location.parameter == "pauseMs"
    ]
    assert len(issues) == 1, report.format_text()
    assert issues[0].severity is PreflightSeverity.INFO
    assert report.can_execute


def test_image_matching_values_must_stay_in_normalized_range() -> None:
    report = _report(
        "find_image",
        {"image_name": "known", "threshold": 1.1},
        db=FakeImageDatabase("known"),
    )

    assert [
        issue
        for issue in report.errors
        if issue.code == "INVALID_PARAMETER_VALUE" and issue.location.parameter == "threshold"
    ], report.format_text()
