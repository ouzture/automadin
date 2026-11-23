from __future__ import annotations

from automadin.execution.image_matching import format_image_match
from automadin.services.find_image import MatchResult


def test_color_match_diagnostic_reports_score_tolerance_and_location() -> None:
    message = format_image_match(
        "submit-button",
        MatchResult(10, 20, 30, 40, 0.96321, color_distance=0.14219),
        threshold=0.9,
        color_match=True,
        color_tolerance=0.3,
    )

    assert message == (
        "Image match: 'submit-button'; confidence=0.9632 >= threshold=0.9000; "
        "color distance=0.1422 <= tolerance=0.3000; "
        "bounds=(10, 20, 30x40); center=(25, 40)"
    )


def test_grayscale_match_diagnostic_marks_color_check_as_off() -> None:
    message = format_image_match(
        "submit-button",
        MatchResult(10, 20, 30, 40, 0.96321),
        threshold=0.9,
        color_match=False,
        color_tolerance=0.3,
    )

    assert "color check=off" in message
