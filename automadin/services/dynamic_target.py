"""Detector-independent tracking math for animated and moving targets."""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median

import numpy as np

from automadin.services.opencv_loader import load_cv2


cv2 = load_cv2()


@dataclass(frozen=True, slots=True)
class TargetObservation:
    """One detector result in absolute surface coordinates."""

    timestamp: float
    x: int
    y: int
    width: int
    height: int
    confidence: float
    identity: str = ""

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2.0

    @property
    def area(self) -> int:
        return max(1, self.width * self.height)


@dataclass(frozen=True, slots=True)
class MotionEstimate:
    velocity_x: float
    velocity_y: float
    speed: float
    residual_px: float


@dataclass(frozen=True, slots=True)
class LearnedPose:
    png_bytes: bytes
    width: int
    height: int
    source_rect: tuple[int, int, int, int]
    tracking_confidence: float
    source_frame_index: int


@dataclass(frozen=True, slots=True)
class LearnedPoseSet:
    poses: tuple[LearnedPose, ...]
    search_rect: tuple[int, int, int, int]
    tracked_frames: int
    total_frames: int
    tracked_rects: tuple[tuple[int, int, int, int] | None, ...]

    @property
    def coverage(self) -> float:
        return self.tracked_frames / self.total_frames if self.total_frames else 0.0


def _decode_png(png_bytes: bytes) -> np.ndarray | None:
    return cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)


def _encode_png(image: np.ndarray) -> bytes:
    encoded, payload = cv2.imencode(".png", image)
    if not encoded:
        raise ValueError("A learned target pose could not be encoded.")
    return payload.tobytes()


def learn_pose_set(
    frame_pngs: list[bytes],
    initial_rect: tuple[int, int, int, int],
    *,
    reference_frame_index: int = 0,
    correction_rects: dict[int, tuple[int, int, int, int]] | None = None,
    max_poses: int = 12,
    minimum_tracking_confidence: float = 0.45,
    minimum_pose_difference: float = 0.055,
) -> LearnedPoseSet:
    """Track from user-marked frames in both directions and retain distinct poses."""
    if not frame_pngs:
        raise ValueError("Record at least one frame before learning a target.")
    images = [_decode_png(payload) for payload in frame_pngs]
    if any(image is None for image in images):
        raise ValueError("The target recording contains an invalid frame.")
    decoded = [image for image in images if image is not None]
    first_height, first_width = decoded[0].shape[:2]
    if any(image.shape[:2] != (first_height, first_width) for image in decoded):
        raise ValueError("All target recording frames must have the same size.")
    frame_count = len(decoded)
    reference_index = int(reference_frame_index)
    if not 0 <= reference_index < frame_count:
        raise ValueError("The selected reference frame is outside the recording.")

    def validate_rect(
        frame_index: int,
        raw_rect: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int]:
        if not 0 <= frame_index < frame_count:
            raise ValueError("A corrected frame is outside the recording.")
        x, y, width, height = (int(value) for value in raw_rect)
        if width < 2 or height < 2 or x < 0 or y < 0:
            raise ValueError("Draw a close rectangle around the target.")
        if x + width > first_width or y + height > first_height:
            raise ValueError("The target rectangle must stay inside the recording.")
        return x, y, width, height

    anchors = {
        reference_index: validate_rect(reference_index, initial_rect),
    }
    for raw_index, raw_rect in (correction_rects or {}).items():
        index = int(raw_index)
        anchors[index] = validate_rect(index, raw_rect)

    TrackedFrame = tuple[np.ndarray, tuple[int, int, int, int], float]
    tracked_by_frame: list[TrackedFrame | None] = [None] * frame_count

    def anchored(frame_index: int) -> TrackedFrame:
        rect = anchors[frame_index]
        x, y, width, height = rect
        return decoded[frame_index][y : y + height, x : x + width].copy(), rect, 1.0

    for frame_index in anchors:
        tracked_by_frame[frame_index] = anchored(frame_index)

    def track_adjacent(
        frame_index: int,
        previous: TrackedFrame,
    ) -> TrackedFrame | None:
        previous_patch, previous_rect, _previous_confidence = previous
        image = decoded[frame_index]
        image_height, image_width = image.shape[:2]
        previous_x, previous_y, previous_width, previous_height = previous_rect
        padding_x = max(24, round(previous_width * 1.75))
        padding_y = max(24, round(previous_height * 1.75))
        left = max(0, previous_x - padding_x)
        top = max(0, previous_y - padding_y)
        right = min(image_width, previous_x + previous_width + padding_x)
        bottom = min(image_height, previous_y + previous_height + padding_y)
        search = image[top:bottom, left:right]
        if (
            search.shape[1] < previous_patch.shape[1]
            or search.shape[0] < previous_patch.shape[0]
        ):
            return None
        result = cv2.matchTemplate(
            cv2.cvtColor(search, cv2.COLOR_BGR2GRAY),
            cv2.cvtColor(previous_patch, cv2.COLOR_BGR2GRAY),
            cv2.TM_CCOEFF_NORMED,
        )
        _minimum, confidence, _minimum_location, location = cv2.minMaxLoc(result)
        if not math.isfinite(confidence) or confidence < minimum_tracking_confidence:
            return None
        next_x = left + int(location[0])
        next_y = top + int(location[1])
        patch = image[
            next_y : next_y + previous_height,
            next_x : next_x + previous_width,
        ].copy()
        if patch.shape[:2] != (previous_height, previous_width):
            return None
        return (
            patch,
            (next_x, next_y, previous_width, previous_height),
            float(confidence),
        )

    def fill_from(start_index: int, indices: range) -> None:
        previous = tracked_by_frame[start_index]
        if previous is None:  # pragma: no cover - anchors are always populated
            return
        for frame_index in indices:
            if frame_index in anchors:
                previous = tracked_by_frame[frame_index]
                if previous is None:  # pragma: no cover - defensive
                    return
                continue
            candidate = track_adjacent(frame_index, previous)
            if candidate is None:
                return
            if tracked_by_frame[frame_index] is None:
                tracked_by_frame[frame_index] = candidate
            previous = candidate

    anchor_indices = sorted(anchors)
    first_anchor = anchor_indices[0]
    fill_from(first_anchor, range(first_anchor - 1, -1, -1))
    for left_anchor, right_anchor in zip(
        anchor_indices[:-1],
        anchor_indices[1:],
        strict=True,
    ):
        fill_from(left_anchor, range(left_anchor + 1, right_anchor))
        fill_from(right_anchor, range(right_anchor - 1, left_anchor, -1))
    last_anchor = anchor_indices[-1]
    fill_from(last_anchor, range(last_anchor + 1, frame_count))

    tracked = [
        (frame_index, item)
        for frame_index, item in enumerate(tracked_by_frame)
        if item is not None
    ]

    fingerprints: list[np.ndarray] = []
    learned: list[LearnedPose] = []
    for frame_index, (patch, rect, confidence) in tracked:
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        fingerprint = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(
            np.float32
        )
        if fingerprints:
            nearest_difference = min(
                float(np.mean(np.abs(fingerprint - existing)) / 255.0)
                for existing in fingerprints
            )
            if nearest_difference < minimum_pose_difference:
                continue
        fingerprints.append(fingerprint)
        learned.append(
            LearnedPose(
                _encode_png(patch),
                rect[2],
                rect[3],
                rect,
                confidence,
                frame_index,
            )
        )
        if len(learned) >= max(1, int(max_poses)):
            break

    if not learned:
        raise ValueError("Automadin could not learn a clear target pose.")
    left = min(rect[0] for _index, (_patch, rect, _confidence) in tracked)
    top = min(rect[1] for _index, (_patch, rect, _confidence) in tracked)
    right = max(rect[0] + rect[2] for _index, (_patch, rect, _confidence) in tracked)
    bottom = max(rect[1] + rect[3] for _index, (_patch, rect, _confidence) in tracked)
    widths = [rect[2] for _index, (_patch, rect, _confidence) in tracked]
    heights = [rect[3] for _index, (_patch, rect, _confidence) in tracked]
    width = round(median(widths))
    height = round(median(heights))
    route_padding_x = max(12, width // 2)
    route_padding_y = max(12, height // 2)
    search_rect = (
        max(0, left - route_padding_x),
        max(0, top - route_padding_y),
        min(first_width, right + route_padding_x),
        min(first_height, bottom + route_padding_y),
    )
    return LearnedPoseSet(
        poses=tuple(learned),
        search_rect=search_rect,
        tracked_frames=len(tracked),
        total_frames=len(decoded),
        tracked_rects=tuple(
            item[1] if item is not None else None for item in tracked_by_frame
        ),
    )


def choose_initial(candidates: list[TargetObservation]) -> TargetObservation | None:
    """Prefer confidence, then visible area, for deterministic acquisition."""
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.confidence, item.area))


def choose_associated(
    candidates: list[TargetObservation],
    previous: TargetObservation,
    *,
    max_movement_px: float,
) -> TargetObservation | None:
    """Associate by movement and size instead of nearest-centre alone."""
    maximum = max(1.0, float(max_movement_px))
    scored: list[tuple[float, TargetObservation]] = []
    for candidate in candidates:
        distance = math.hypot(
            candidate.center_x - previous.center_x,
            candidate.center_y - previous.center_y,
        )
        if distance > maximum:
            continue
        size_ratio = candidate.area / previous.area
        if not 0.35 <= size_ratio <= 2.85:
            continue
        size_penalty = abs(math.log(size_ratio))
        identity_penalty = (
            0.0
            if not previous.identity
            or not candidate.identity
            or previous.identity == candidate.identity
            else 0.08
        )
        score = (
            distance / maximum
            + size_penalty * 0.35
            + identity_penalty
            - min(1.0, max(0.0, candidate.confidence)) * 0.15
        )
        scored.append((score, candidate))
    return min(scored, key=lambda item: item[0])[1] if scored else None


def estimate_motion(observations: list[TargetObservation]) -> MotionEstimate | None:
    """Fit position against time and report robust residual noise."""
    if len(observations) < 2:
        return None
    origin = observations[0].timestamp
    times = [item.timestamp - origin for item in observations]
    mean_time = sum(times) / len(times)
    denominator = sum((value - mean_time) ** 2 for value in times)
    if denominator <= 1e-9:
        return None

    def slope(values: list[float]) -> tuple[float, float]:
        mean_value = sum(values) / len(values)
        velocity = sum(
            (time_value - mean_time) * (value - mean_value)
            for time_value, value in zip(times, values, strict=True)
        ) / denominator
        intercept = mean_value - velocity * mean_time
        return velocity, intercept

    velocity_x, intercept_x = slope([item.center_x for item in observations])
    velocity_y, intercept_y = slope([item.center_y for item in observations])
    residuals = [
        math.hypot(
            item.center_x - (intercept_x + velocity_x * time_value),
            item.center_y - (intercept_y + velocity_y * time_value),
        )
        for item, time_value in zip(observations, times, strict=True)
    ]
    return MotionEstimate(
        velocity_x=velocity_x,
        velocity_y=velocity_y,
        speed=math.hypot(velocity_x, velocity_y),
        residual_px=float(median(residuals)),
    )


def anchor_point(
    observation: TargetObservation,
    anchor: str,
    *,
    offset_x_percent: float = 0.0,
    offset_y_percent: float = 0.0,
) -> tuple[float, float]:
    anchors = {
        "top_left": (0.0, 0.0),
        "top": (0.5, 0.0),
        "top_right": (1.0, 0.0),
        "left": (0.0, 0.5),
        "center": (0.5, 0.5),
        "right": (1.0, 0.5),
        "bottom_left": (0.0, 1.0),
        "bottom": (0.5, 1.0),
        "bottom_right": (1.0, 1.0),
    }
    relative_x, relative_y = anchors.get(anchor, anchors["center"])
    return (
        observation.x
        + observation.width * (relative_x + offset_x_percent / 100.0),
        observation.y
        + observation.height * (relative_y + offset_y_percent / 100.0),
    )


def point_in_box(
    point: tuple[float, float],
    box: tuple[float, float, float, float],
) -> bool:
    left, top, right, bottom = box
    return left <= point[0] <= right and top <= point[1] <= bottom


def predict_point(
    observation: TargetObservation,
    motion: MotionEstimate,
    *,
    lead_time_ms: float,
    max_distance_px: float,
) -> tuple[float, float]:
    lead_seconds = max(0.0, float(lead_time_ms)) / 1000.0
    delta_x = motion.velocity_x * lead_seconds
    delta_y = motion.velocity_y * lead_seconds
    distance = math.hypot(delta_x, delta_y)
    maximum = max(0.0, float(max_distance_px))
    if maximum and distance > maximum:
        scale = maximum / distance
        delta_x *= scale
        delta_y *= scale
    return observation.center_x + delta_x, observation.center_y + delta_y


__all__ = [
    "MotionEstimate",
    "LearnedPose",
    "LearnedPoseSet",
    "TargetObservation",
    "anchor_point",
    "choose_associated",
    "choose_initial",
    "estimate_motion",
    "learn_pose_set",
    "point_in_box",
    "predict_point",
]
