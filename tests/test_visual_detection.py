import unittest
import sys
from types import ModuleType
from unittest.mock import patch

import numpy as np

sys.modules.setdefault("cv2", ModuleType("cv2"))

from automadin.models.visual_rule import VisualRule
from automadin.services import visual_detection


class VisualDetectionTests(unittest.TestCase):
    def test_visual_rule_serializes_multi_color_ranges(self) -> None:
        rule = VisualRule(
            name="test/multi",
            type="multi_color_region",
            color_ranges=[
                {"hue_min": 10, "hue_max": 20, "sat_min": 30, "sat_max": 40, "value_min": 50, "value_max": 60},
                {"hue_min": 80, "hue_max": 90, "sat_min": 100, "sat_max": 110, "value_min": 120, "value_max": 130},
            ],
        )

        loaded = VisualRule.from_dict(rule.to_dict())

        self.assertEqual(loaded.type, "multi_color_region")
        self.assertEqual(loaded.color_ranges, rule.color_ranges)

    def test_color_region_rule_uses_color_blob_detector(self) -> None:
        image = np.zeros((80, 80, 3), dtype=np.uint8)
        rule = VisualRule(
            name="test/green_region",
            type="color_region",
        )
        expected = [
            visual_detection.VisualBlob(
                center_x=10,
                center_y=12,
                area=50,
                roundness=0.8,
                rect=(1, 2, 20, 20),
            )
        ]

        with patch.object(
            visual_detection,
            "detect_color_blobs",
            return_value=expected,
        ) as detect_color_blobs:
            blobs = visual_detection.detect_blobs(image, rule)

        self.assertEqual(blobs, expected)
        detect_color_blobs.assert_called_once_with(image, rule)

    def test_multi_color_region_rule_uses_multi_color_detector(self) -> None:
        image = np.zeros((80, 80, 3), dtype=np.uint8)
        rule = VisualRule(
            name="test/multi",
            type="multi_color_region",
            color_ranges=[
                {"hue_min": 10, "hue_max": 20, "sat_min": 30, "sat_max": 40, "value_min": 50, "value_max": 60},
            ],
        )
        expected = [
            visual_detection.VisualBlob(
                center_x=30,
                center_y=32,
                area=75,
                roundness=0.7,
                rect=(20, 22, 18, 18),
            )
        ]

        with patch.object(
            visual_detection,
            "detect_multi_color_blobs",
            return_value=expected,
        ) as detect_multi_color_blobs:
            blobs = visual_detection.detect_blobs(image, rule)

        self.assertEqual(blobs, expected)
        detect_multi_color_blobs.assert_called_once_with(image, rule)

    def test_rule_crop_and_command_scope_intersect_in_surface_coordinates(self) -> None:
        image = np.zeros((2560, 800, 3), dtype=np.uint8)
        rule = VisualRule(
            name="TapTitans2/Chest Blob",
            type="multi_color_region",
            crop_left_percent=64.35,
            crop_right_percent=32.78,
            crop_top_percent=21.41,
            crop_bottom_percent=76.82,
        )
        local = visual_detection.VisualBlob(
            center_x=8,
            center_y=5,
            area=200,
            roundness=0.7,
            rect=(2, 1, 12, 8),
        )

        with patch.object(
            visual_detection,
            "detect_blobs",
            return_value=[local],
        ) as detect:
            blobs = visual_detection.detect_blobs_in_scope(
                image,
                rule,
                (128, 480, 668, 626),
            )

        scoped_image, scoped_rule = detect.call_args.args
        self.assertEqual(scoped_image.shape[:2], (46, 24))
        self.assertEqual(scoped_rule.crop_left_percent, 0.0)
        self.assertEqual(scoped_rule.crop_top_percent, 0.0)
        self.assertEqual(blobs[0].rect, (516, 549, 12, 8))
        self.assertEqual((blobs[0].center_x, blobs[0].center_y), (522, 553))


if __name__ == "__main__":
    unittest.main()
