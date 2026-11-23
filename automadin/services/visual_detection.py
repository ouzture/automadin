from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from automadin.models.visual_rule import VisualRule
from automadin.services.opencv_loader import load_cv2


cv2 = load_cv2()

COLOR_RANGE_RULE_TYPES = {"color_blob", "color_region"}
MULTI_COLOR_RULE_TYPES = {"multi_color_region"}


@dataclass
class VisualBlob:
    center_x: int
    center_y: int
    area: float
    roundness: float
    rect: tuple[int, int, int, int]


def decode_screenshot(screenshot: bytes) -> np.ndarray:
    arr = np.frombuffer(screenshot, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("Could not decode screenshot")
    return image


def crop_rect(rule: VisualRule, width: int, height: int) -> tuple[int, int, int, int]:
    left = int(width * max(0.0, min(95.0, rule.crop_left_percent)) / 100.0)
    right = width - int(width * max(0.0, min(95.0, rule.crop_right_percent)) / 100.0)
    top = int(height * max(0.0, min(95.0, rule.crop_top_percent)) / 100.0)
    bottom = height - int(height * max(0.0, min(95.0, rule.crop_bottom_percent)) / 100.0)
    if right <= left:
        left, right = 0, width
    if bottom <= top:
        top, bottom = 0, height
    return left, top, right, bottom


def _rule_color_range(rule: VisualRule) -> dict[str, int]:
    return {
        "hue_min": rule.hue_min,
        "hue_max": rule.hue_max,
        "sat_min": rule.sat_min,
        "sat_max": rule.sat_max,
        "value_min": rule.value_min,
        "value_max": rule.value_max,
    }


def _mask_for_color_range(hsv: np.ndarray, color_range: dict[str, int]) -> np.ndarray:
    hue_min = int(color_range.get("hue_min", 0))
    hue_max = int(color_range.get("hue_max", 179))
    sat_min = int(color_range.get("sat_min", 0))
    sat_max = int(color_range.get("sat_max", 255))
    value_min = int(color_range.get("value_min", 0))
    value_max = int(color_range.get("value_max", 255))

    lower = np.array([hue_min, sat_min, value_min], dtype=np.uint8)
    upper = np.array([hue_max, sat_max, value_max], dtype=np.uint8)
    if hue_min <= hue_max:
        mask = cv2.inRange(hsv, lower, upper)
    else:
        lower_a = np.array([0, sat_min, value_min], dtype=np.uint8)
        upper_a = np.array([hue_max, sat_max, value_max], dtype=np.uint8)
        lower_b = np.array([hue_min, sat_min, value_min], dtype=np.uint8)
        upper_b = np.array([179, sat_max, value_max], dtype=np.uint8)
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, lower_a, upper_a),
            cv2.inRange(hsv, lower_b, upper_b),
        )
    return mask


def _detect_blobs_from_mask(
    mask: np.ndarray,
    rule: VisualRule,
    offset_x: int,
    offset_y: int,
) -> list[VisualBlob]:
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    blobs: list[VisualBlob] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < rule.min_area or area > rule.max_area:
            continue
        perimeter = float(cv2.arcLength(contour, True))
        roundness = 0.0 if perimeter <= 0 else float((4.0 * math.pi * area) / (perimeter * perimeter))
        if roundness < rule.min_roundness:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            continue
        cx = int(moments["m10"] / moments["m00"]) + offset_x
        cy = int(moments["m01"] / moments["m00"]) + offset_y
        x, y, w, h = cv2.boundingRect(contour)
        blobs.append(
            VisualBlob(
                center_x=cx,
                center_y=cy,
                area=area,
                roundness=roundness,
                rect=(x + offset_x, y + offset_y, w, h),
            )
        )
    return blobs


def detect_color_blobs(image: np.ndarray, rule: VisualRule) -> list[VisualBlob]:
    height, width = image.shape[:2]
    left, top, right, bottom = crop_rect(rule, width, height)
    crop = image[top:bottom, left:right]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = _mask_for_color_range(hsv, _rule_color_range(rule))
    return _detect_blobs_from_mask(mask, rule, left, top)


def detect_multi_color_blobs(image: np.ndarray, rule: VisualRule) -> list[VisualBlob]:
    height, width = image.shape[:2]
    left, top, right, bottom = crop_rect(rule, width, height)
    crop = image[top:bottom, left:right]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    color_ranges = rule.color_ranges or [_rule_color_range(rule)]
    combined_mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for color_range in color_ranges:
        combined_mask = cv2.bitwise_or(
            combined_mask,
            _mask_for_color_range(hsv, color_range),
        )
    return _detect_blobs_from_mask(combined_mask, rule, left, top)


def detect_blobs(image: np.ndarray, rule: VisualRule) -> list[VisualBlob]:
    if rule.type in COLOR_RANGE_RULE_TYPES:
        return detect_color_blobs(image, rule)
    if rule.type in MULTI_COLOR_RULE_TYPES:
        return detect_multi_color_blobs(image, rule)
    raise RuntimeError(f"Unsupported visual rule type: {rule.type}")


def detect_blobs_in_scope(
    image: np.ndarray,
    rule: VisualRule,
    scope: tuple[int, int, int, int],
) -> list[VisualBlob]:
    """Detect within the absolute intersection of a rule crop and command scope.

    Visual-rule crop percentages are calibrated against the complete captured
    surface.  A command search region is also expressed in that surface's
    coordinates.  Applying the rule crop to an already-cropped command image
    changes its meaning and can reduce the effective search to a few pixels.
    Resolve both scopes once, intersect them, and translate detections back to
    absolute surface coordinates.
    """
    height, width = image.shape[:2]
    rule_left, rule_top, rule_right, rule_bottom = crop_rect(rule, width, height)
    scope_left, scope_top, scope_right, scope_bottom = scope
    left = max(0, rule_left, int(scope_left))
    top = max(0, rule_top, int(scope_top))
    right = min(width, rule_right, int(scope_right))
    bottom = min(height, rule_bottom, int(scope_bottom))
    if right <= left or bottom <= top:
        return []

    scoped_rule = replace(
        rule,
        crop_top_percent=0.0,
        crop_bottom_percent=0.0,
        crop_left_percent=0.0,
        crop_right_percent=0.0,
    )
    blobs = detect_blobs(image[top:bottom, left:right], scoped_rule)
    return [
        VisualBlob(
            center_x=blob.center_x + left,
            center_y=blob.center_y + top,
            area=blob.area,
            roundness=blob.roundness,
            rect=(
                blob.rect[0] + left,
                blob.rect[1] + top,
                blob.rect[2],
                blob.rect[3],
            ),
        )
        for blob in blobs
    ]
