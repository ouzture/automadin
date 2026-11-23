from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _clean_color_range(data: dict[str, Any]) -> dict[str, int]:
    return {
        "hue_min": int(data.get("hue_min", 0)),
        "hue_max": int(data.get("hue_max", 179)),
        "sat_min": int(data.get("sat_min", 0)),
        "sat_max": int(data.get("sat_max", 255)),
        "value_min": int(data.get("value_min", 0)),
        "value_max": int(data.get("value_max", 255)),
    }


@dataclass
class VisualRule:
    """Reusable visual detector definition used by smart flow commands."""

    name: str
    type: str = "color_blob"
    hue_min: int = 0
    hue_max: int = 179
    sat_min: int = 0
    sat_max: int = 255
    value_min: int = 0
    value_max: int = 255
    min_area: int = 20
    max_area: int = 999999
    min_roundness: float = 0.0
    crop_top_percent: float = 0.0
    crop_bottom_percent: float = 0.0
    crop_left_percent: float = 0.0
    crop_right_percent: float = 0.0
    color_ranges: list[dict[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "hue_min": self.hue_min,
            "hue_max": self.hue_max,
            "sat_min": self.sat_min,
            "sat_max": self.sat_max,
            "value_min": self.value_min,
            "value_max": self.value_max,
            "min_area": self.min_area,
            "max_area": self.max_area,
            "min_roundness": self.min_roundness,
            "crop_top_percent": self.crop_top_percent,
            "crop_bottom_percent": self.crop_bottom_percent,
            "crop_left_percent": self.crop_left_percent,
            "crop_right_percent": self.crop_right_percent,
            "color_ranges": [_clean_color_range(item) for item in self.color_ranges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VisualRule:
        raw_ranges = data.get("color_ranges", [])
        color_ranges = []
        if isinstance(raw_ranges, list):
            color_ranges = [
                _clean_color_range(item)
                for item in raw_ranges
                if isinstance(item, dict)
            ]
        return cls(
            name=str(data.get("name", "")).strip(),
            type=str(data.get("type", "color_blob")),
            hue_min=int(data.get("hue_min", 0)),
            hue_max=int(data.get("hue_max", 179)),
            sat_min=int(data.get("sat_min", 0)),
            sat_max=int(data.get("sat_max", 255)),
            value_min=int(data.get("value_min", 0)),
            value_max=int(data.get("value_max", 255)),
            min_area=int(data.get("min_area", 20)),
            max_area=int(data.get("max_area", 999999)),
            min_roundness=float(data.get("min_roundness", 0.0)),
            crop_top_percent=float(data.get("crop_top_percent", 0.0)),
            crop_bottom_percent=float(data.get("crop_bottom_percent", 0.0)),
            crop_left_percent=float(data.get("crop_left_percent", 0.0)),
            crop_right_percent=float(data.get("crop_right_percent", 0.0)),
            color_ranges=color_ranges,
        )
