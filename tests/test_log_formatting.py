from __future__ import annotations

import unittest

from automadin.ui.log_formatting import (
    command_label,
    command_color,
    format_duration_ms,
    humanize_log_durations,
    message_color,
)
from automadin.ui.theme import CATEGORY_COLORS, COLORS


class LogFormattingTests(unittest.TestCase):
    def test_command_label_uses_readable_metadata_name(self) -> None:
        self.assertEqual(command_label("click_image"), "Click Image")
        self.assertEqual(command_label("unknown_command"), "Unknown Command")

    def test_command_color_uses_command_category(self) -> None:
        self.assertEqual(command_color("wait"), CATEGORY_COLORS["TIMING"])
        self.assertEqual(command_color("navigate_to"), CATEGORY_COLORS["BROWSER"])

    def test_message_colors_distinguish_common_execution_events(self) -> None:
        self.assertEqual(message_color("Waiting 500 ms"), COLORS["warning"])
        self.assertEqual(message_color("Opened new tab"), COLORS["success"])
        self.assertEqual(message_color("command — started"), COLORS["primary"])
        self.assertEqual(message_color("Execution failed"), COLORS["error"])
        self.assertEqual(message_color("Image condition 'found' met"), COLORS["success"])
        self.assertEqual(message_color("Image condition 'found' not met"), COLORS["error"])
        self.assertEqual(message_color("Hook probe matched=yes"), COLORS["success"])
        self.assertEqual(message_color("Hook probe matched=no"), COLORS["error"])

    def test_duration_formatting_uses_readable_units(self) -> None:
        cases = {
            0: "0 ms",
            250: "250 ms",
            999: "999 ms",
            1000: "1 sec",
            1500: "1.5 sec",
            15000: "15 sec",
            60000: "1 min",
            90000: "1 min 30 sec",
            3661000: "1 hr 1 min 1 sec",
        }
        for milliseconds, expected in cases.items():
            with self.subTest(milliseconds=milliseconds):
                self.assertEqual(format_duration_ms(milliseconds), expected)

    def test_log_lines_humanize_all_millisecond_literals(self) -> None:
        self.assertEqual(
            humanize_log_durations(
                "Waiting 15000ms (random); timeout=90000 ms; detection=12.5ms"
            ),
            "Waiting 15 sec (random); timeout=1 min 30 sec; detection=12.5 ms",
        )


if __name__ == "__main__":
    unittest.main()
