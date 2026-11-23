import unittest

from automadin.execution.handlers.visual import _choose_target
from automadin.services.visual_detection import VisualBlob


class ClickBestTargetTests(unittest.TestCase):
    def test_bonus_scoring_with_none_fallback_returns_no_target_when_no_bonus_found(self) -> None:
        targets = [
            VisualBlob(center_x=25, center_y=30, area=100, roundness=0.8, rect=(20, 25, 10, 10)),
        ]

        target, bonus, distance = _choose_target(
            targets=targets,
            bonuses=[],
            source=(10, 10),
            scoring_mode="closest_to_bonus_path",
            fallback_mode="none",
            path_tolerance=30.0,
        )

        self.assertIsNone(target)
        self.assertIsNone(bonus)
        self.assertEqual(distance, 0.0)

    def test_bonus_scoring_with_none_fallback_returns_no_target_when_bonus_misses_path(self) -> None:
        targets = [
            VisualBlob(center_x=100, center_y=10, area=100, roundness=0.8, rect=(95, 5, 10, 10)),
        ]
        bonuses = [
            VisualBlob(center_x=50, center_y=100, area=20, roundness=0.7, rect=(48, 98, 4, 4)),
        ]

        target, bonus, distance = _choose_target(
            targets=targets,
            bonuses=bonuses,
            source=(0, 10),
            scoring_mode="closest_to_bonus_path",
            fallback_mode="none",
            path_tolerance=10.0,
        )

        self.assertIsNone(target)
        self.assertIsNone(bonus)
        self.assertEqual(distance, 0.0)


if __name__ == "__main__":
    unittest.main()
