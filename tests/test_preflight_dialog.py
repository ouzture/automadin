from automadin.services.flow_preflight import (
    PreflightIssue,
    PreflightLocation,
    PreflightReport,
    PreflightSeverity,
)
from automadin.ui.preflight_dialog import _report_text


def test_report_text_puts_blocking_errors_before_notes() -> None:
    flow_location = PreflightLocation("Rewards", (0,), "abcd", "press_keycode")
    profile_location = PreflightLocation(
        "Rewards",
        (),
        "",
        "",
        scope="Run setup 'Rewards - Complete' · browser profile 'Personal'",
    )
    report = PreflightReport(
        (
            PreflightIssue(
                "LEGACY_PARAMETER",
                PreflightSeverity.INFO,
                "A compatibility alias is present.",
                flow_location,
            ),
            PreflightIssue(
                "PROFILE_IN_USE",
                PreflightSeverity.ERROR,
                "This browser profile is already in use.",
                profile_location,
            ),
        ),
        ("Rewards",),
    )

    text = _report_text(report, introduction="Cannot run.")

    assert text.index("ERROR ·") < text.index("INFO ·")
    assert "Run setup 'Rewards - Complete' · browser profile 'Personal'" in text
    assert "Rewards · commands[0] · press_keycode [abcd]" in text
