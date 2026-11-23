"""OpenCV template matching service."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import numpy as np

from automadin.services.opencv_loader import load_cv2


cv2 = load_cv2()


@dataclass
class MatchResult:
    x: int
    y: int
    width: int
    height: int
    confidence: float
    color_distance: float | None = None

    @property
    def center_x(self) -> int:
        return self.x + self.width // 2

    @property
    def center_y(self) -> int:
        return self.y + self.height // 2


class FindImageService:
    """Template matching using OpenCV."""

    DEFAULT_MAX_BEST_CANDIDATES = 512
    DEFAULT_MAX_ALL_CANDIDATES = 5_000
    DEFAULT_MAX_ALL_MATCHES = 500
    COLOR_HISTOGRAM_BINS = 32
    COLOR_HISTOGRAM_SMOOTHING_PASSES = 3

    def find(
        self,
        screenshot: bytes,
        template: bytes,
        threshold: float = 0.90,
        color_match: bool = False,
        color_tolerance: float = 0.3,
    ) -> MatchResult | None:
        """Find a template image within a screenshot.

        Args:
            screenshot: PNG screenshot bytes.
            template: PNG template image bytes.
            threshold: Minimum confidence (0.0-1.0).
            color_match: If True, validate the matched region's foreground colors.
            color_tolerance: Max allowed foreground histogram distance (default 0.3).

        Returns:
            MatchResult if found above threshold (and color check passes), else None.
        """
        return self.find_best(
            screenshot,
            template,
            threshold=threshold,
            color_match=color_match,
            color_tolerance=color_tolerance,
            # A single flat or repetitive frame can contain hundreds of
            # thousands of overlapping threshold hits. Runtime commands must
            # stay responsive enough to take a fresh frame before their poll
            # deadline instead of exhaustively color-checking stale pixels.
            max_candidates=self.DEFAULT_MAX_BEST_CANDIDATES,
        )

    def find_best(
        self,
        screenshot: bytes,
        template: bytes,
        threshold: float = 0.90,
        color_match: bool = False,
        color_tolerance: float = 0.3,
        *,
        max_candidates: int | None = DEFAULT_MAX_BEST_CANDIDATES,
    ) -> MatchResult | None:
        """Return the best acceptable match with bounded candidate work.

        Grayscale matching itself stays inside OpenCV.  If color validation
        rejects the strongest point, at most ``max_candidates`` threshold hits
        are inspected without ever materializing every hit in the frame. Pass
        ``None`` only when exhaustive, memory-bounded color validation is
        explicitly required by a diagnostic caller.
        """
        if max_candidates is not None and max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        prepared = self._prepare_match(screenshot, template)
        if prepared is None:
            return None
        return self._find_best_prepared(
            prepared,
            threshold=threshold,
            color_match=color_match,
            color_tolerance=color_tolerance,
            max_candidates=max_candidates,
        )

    def find_scaled(
        self,
        screenshot: bytes,
        template: bytes,
        scales: Iterable[float],
        threshold: float = 0.90,
        color_match: bool = False,
        color_tolerance: float = 0.3,
    ) -> MatchResult | None:
        """Find the best match across a bounded set of template scales.

        The screenshot and source template are decoded only once. Duplicate
        output dimensions are skipped, which matters for small templates where
        several nearby factors can round to the same size.
        """
        screen_arr = self._decode(screenshot)
        source_template = self._decode(template)
        if screen_arr is None or source_template is None:
            return None
        source_height, source_width = source_template.shape[:2]
        seen_sizes: set[tuple[int, int]] = set()
        best: MatchResult | None = None
        for raw_scale in scales:
            try:
                scale = float(raw_scale)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(scale) or scale <= 0:
                continue
            width = max(1, round(source_width * scale))
            height = max(1, round(source_height * scale))
            size = (width, height)
            if size in seen_sizes:
                continue
            seen_sizes.add(size)
            if size == (source_width, source_height):
                candidate_template = source_template
            else:
                interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                candidate_template = cv2.resize(
                    source_template,
                    size,
                    interpolation=interpolation,
                )
            prepared = self._prepare_match_arrays(screen_arr, candidate_template)
            if prepared is None:
                continue
            match = self._find_best_prepared(
                prepared,
                threshold=threshold,
                color_match=color_match,
                color_tolerance=color_tolerance,
                max_candidates=self.DEFAULT_MAX_BEST_CANDIDATES,
            )
            if match is not None and (
                best is None or match.confidence > best.confidence
            ):
                best = match
        return best

    def _find_best_prepared(
        self,
        prepared: tuple[np.ndarray, np.ndarray, np.ndarray, int, int],
        *,
        threshold: float,
        color_match: bool,
        color_tolerance: float,
        max_candidates: int | None,
    ) -> MatchResult | None:
        screen_arr, tmpl_arr, result, width, height = prepared
        _, confidence, _, best_location = cv2.minMaxLoc(result)
        if not np.isfinite(confidence) or confidence < threshold:
            return None

        best_x, best_y = int(best_location[0]), int(best_location[1])
        if not color_match or self._color_matches(
            screen_arr,
            tmpl_arr,
            best_x,
            best_y,
            color_tolerance,
        ):
            return MatchResult(
                x=best_x,
                y=best_y,
                width=width,
                height=height,
                confidence=float(confidence),
                color_distance=(
                    self._color_distance(
                        screen_arr,
                        tmpl_arr,
                        best_x,
                        best_y,
                    )
                    if color_match
                    else None
                ),
            )

        acceptable: MatchResult | None = None
        remaining = None if max_candidates is None else max_candidates - 1
        for match_x, match_y in self._threshold_points(
            result,
            threshold,
            max_points=remaining,
            skip=(best_x, best_y),
        ):
            if self._color_matches(
                screen_arr,
                tmpl_arr,
                match_x,
                match_y,
                color_tolerance,
            ):
                match = MatchResult(
                    x=match_x,
                    y=match_y,
                    width=width,
                    height=height,
                    confidence=float(result[match_y, match_x]),
                    color_distance=self._color_distance(
                        screen_arr,
                        tmpl_arr,
                        match_x,
                        match_y,
                    ),
                )
                if (
                    acceptable is None
                    or match.confidence > acceptable.confidence
                ):
                    acceptable = match
        return acceptable

    def find_all(
        self,
        screenshot: bytes,
        template: bytes,
        threshold: float = 0.90,
        color_match: bool = False,
        color_tolerance: float = 0.3,
        *,
        max_candidates: int = DEFAULT_MAX_ALL_CANDIDATES,
        max_matches: int = DEFAULT_MAX_ALL_MATCHES,
    ) -> list[MatchResult]:
        """Find a bounded set of occurrences of a template in a screenshot."""
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        if max_matches <= 0:
            raise ValueError("max_matches must be positive")
        prepared = self._prepare_match(screenshot, template)
        if prepared is None:
            return []
        screen_arr, tmpl_arr, result, width, height = prepared
        _, best_confidence, _, best_location = cv2.minMaxLoc(result)
        if not np.isfinite(best_confidence) or best_confidence < threshold:
            return []
        best_point = (int(best_location[0]), int(best_location[1]))
        candidates: list[MatchResult] = []
        points = self._threshold_points(
            result,
            threshold,
            max_points=max_candidates - 1,
            skip=best_point,
        )
        for match_x, match_y in (best_point, *points):
            if color_match and not self._color_matches(
                screen_arr,
                tmpl_arr,
                match_x,
                match_y,
                color_tolerance,
            ):
                continue
            match = MatchResult(
                x=match_x,
                y=match_y,
                width=width,
                height=height,
                confidence=float(result[match_y, match_x]),
                color_distance=(
                    self._color_distance(
                        screen_arr,
                        tmpl_arr,
                        match_x,
                        match_y,
                    )
                    if color_match
                    else None
                ),
            )
            candidates.append(match)

        candidates.sort(key=lambda match: match.confidence, reverse=True)

        matches: list[MatchResult] = []
        for match in candidates:
            if not self._overlaps(match, matches):
                matches.append(match)
                if len(matches) >= max_matches:
                    break
        return matches

    def _prepare_match(
        self,
        screenshot: bytes,
        template: bytes,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int] | None:
        screen_arr = self._decode(screenshot)
        tmpl_arr = self._decode(template)
        if screen_arr is None or tmpl_arr is None:
            return None

        return self._prepare_match_arrays(screen_arr, tmpl_arr)

    @staticmethod
    def _prepare_match_arrays(
        screen_arr: np.ndarray,
        tmpl_arr: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int] | None:

        screen_gray = cv2.cvtColor(screen_arr, cv2.COLOR_BGR2GRAY)
        tmpl_gray = cv2.cvtColor(tmpl_arr, cv2.COLOR_BGR2GRAY)
        height, width = tmpl_gray.shape[:2]
        screen_height, screen_width = screen_gray.shape[:2]
        if height > screen_height or width > screen_width:
            return None
        result = cv2.matchTemplate(
            screen_gray,
            tmpl_gray,
            cv2.TM_CCOEFF_NORMED,
        )
        return screen_arr, tmpl_arr, result, width, height

    @staticmethod
    def _threshold_points(
        result: np.ndarray,
        threshold: float,
        *,
        max_points: int | None,
        skip: tuple[int, int] | None = None,
    ) -> Iterator[tuple[int, int]]:
        """Yield threshold hits with row-sized temporary allocations only."""
        if max_points is not None and max_points <= 0:
            return
        yielded = 0
        for match_y in range(result.shape[0]):
            row_locations = np.flatnonzero(result[match_y] >= threshold)
            for raw_x in row_locations:
                point = (int(raw_x), match_y)
                if point == skip:
                    continue
                yield point
                yielded += 1
                if max_points is not None and yielded >= max_points:
                    return

    @staticmethod
    def _color_matches(
        screen_bgr: np.ndarray,
        tmpl_bgr: np.ndarray,
        match_x: int,
        match_y: int,
        tolerance: float = 0.3,
    ) -> bool:
        """Validate a match with a spatially tolerant color comparison.

        Grayscale template matching has already validated shape and position.
        Color validation therefore compares distributions over informative
        pixels instead of requiring exact pixel alignment. This tolerates small
        renderer, scaling, animation, and anti-aliasing differences while still
        rejecting meaningful state changes such as a colored control becoming
        greyed out.

        Brightness and saturation use normalized one-dimensional earth-mover
        distance, so a small palette shift across a histogram-bin boundary has
        a small score. Hue uses a circularly smoothed histogram and
        Bhattacharyya distance so a localized recolor remains detectable.

        Args:
            tolerance: 0.0–1.0, default 0.3. Lower = stricter color requirement.
        """
        return (
            FindImageService._color_distance(
                screen_bgr,
                tmpl_bgr,
                match_x,
                match_y,
            )
            <= tolerance
        )

    @staticmethod
    def _color_distance(
        screen_bgr: np.ndarray,
        tmpl_bgr: np.ndarray,
        match_x: int,
        match_y: int,
    ) -> float:
        """Return the decisive 0.0-1.0 color distance for a candidate.

        The color check accepts a candidate only when every applicable HSV
        component is within tolerance, so the maximum component distance is
        the useful single diagnostic score. Lower values are closer.
        """
        th, tw = tmpl_bgr.shape[:2]
        if tw < 2 or th < 2:
            return 0.0  # too small to compare; retain the historic acceptance

        sh, sw = screen_bgr.shape[:2]
        region = screen_bgr[match_y:match_y + th, match_x:match_x + tw]
        if region.shape != tmpl_bgr.shape:
            return 0.0  # out of bounds edge case; retain historic acceptance

        tmpl_hsv = cv2.cvtColor(tmpl_bgr, cv2.COLOR_BGR2HSV)
        region_hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        mask = FindImageService._informative_mask(tmpl_bgr)

        value_distance = FindImageService._ordered_histogram_distance(
            tmpl_hsv,
            region_hsv,
            channel=2,
            mask=mask,
        )
        saturation_distance = FindImageService._ordered_histogram_distance(
            tmpl_hsv,
            region_hsv,
            channel=1,
            mask=mask,
        )
        hue_mask = np.where(
            (mask > 0)
            & (tmpl_hsv[:, :, 1] >= 20)
            & (region_hsv[:, :, 1] >= 20),
            255,
            0,
        ).astype(np.uint8)
        if cv2.countNonZero(hue_mask) < 12:
            return max(value_distance, saturation_distance)

        template_hue = FindImageService._normalized_histogram(
            tmpl_hsv,
            channel=0,
            mask=hue_mask,
            value_range=(0, 180),
        )
        region_hue = FindImageService._normalized_histogram(
            region_hsv,
            channel=0,
            mask=hue_mask,
            value_range=(0, 180),
        )
        template_hue = FindImageService._smooth_circular_histogram(template_hue)
        region_hue = FindImageService._smooth_circular_histogram(region_hue)
        hue_distance = float(
            cv2.compareHist(
                template_hue,
                region_hue,
                cv2.HISTCMP_BHATTACHARYYA,
            )
        )
        return max(value_distance, saturation_distance, hue_distance)

    @staticmethod
    def _normalized_histogram(
        image: np.ndarray,
        *,
        channel: int,
        mask: np.ndarray,
        value_range: tuple[int, int] = (0, 256),
    ) -> np.ndarray:
        histogram = cv2.calcHist(
            [image],
            [channel],
            mask,
            [FindImageService.COLOR_HISTOGRAM_BINS],
            list(value_range),
        ).reshape(-1).astype(np.float32)
        total = float(np.sum(histogram))
        if total > 0:
            histogram /= total
        return histogram

    @staticmethod
    def _ordered_histogram_distance(
        template: np.ndarray,
        region: np.ndarray,
        *,
        channel: int,
        mask: np.ndarray,
    ) -> float:
        """Return normalized earth-mover distance for an ordered HSV channel."""
        template_histogram = FindImageService._normalized_histogram(
            template,
            channel=channel,
            mask=mask,
        )
        region_histogram = FindImageService._normalized_histogram(
            region,
            channel=channel,
            mask=mask,
        )
        cumulative_difference = np.cumsum(template_histogram - region_histogram)
        max_transport = max(1, FindImageService.COLOR_HISTOGRAM_BINS - 1)
        return float(np.sum(np.abs(cumulative_difference)) / max_transport)

    @staticmethod
    def _smooth_circular_histogram(histogram: np.ndarray) -> np.ndarray:
        """Smooth neighbouring hue bins while preserving the circular boundary."""
        smoothed = histogram.astype(np.float32, copy=True)
        for _ in range(FindImageService.COLOR_HISTOGRAM_SMOOTHING_PASSES):
            smoothed = (
                np.roll(smoothed, 1) + 2.0 * smoothed + np.roll(smoothed, -1)
            ) / 4.0
        magnitude = float(np.linalg.norm(smoothed))
        if magnitude > 0:
            smoothed /= magnitude
        return smoothed.astype(np.float32, copy=False)

    @staticmethod
    def _informative_mask(tmpl_bgr: np.ndarray) -> np.ndarray:
        """Build a mask around foreground strokes/details in the template."""
        tmpl_gray = cv2.cvtColor(tmpl_bgr, cv2.COLOR_BGR2GRAY)
        background_gray = FindImageService._border_background_gray(tmpl_gray)

        deviation = np.abs(tmpl_gray.astype(np.int16) - background_gray).astype(np.uint8)
        edges = cv2.Canny(tmpl_gray, 40, 120)
        edges = cv2.dilate(edges, np.ones((3, 3), dtype=np.uint8), iterations=1)

        mask = np.where((deviation >= 12) | (edges > 0), 255, 0).astype(np.uint8)
        min_pixels = max(24, int(mask.size * 0.01))
        if int(cv2.countNonZero(mask)) < min_pixels:
            return np.full(tmpl_gray.shape, 255, dtype=np.uint8)
        return mask

    @staticmethod
    def _border_background_gray(gray: np.ndarray) -> float:
        """Estimate template background brightness from its border pixels."""
        if gray.size == 0:
            return 0.0

        top = gray[0, :]
        bottom = gray[-1, :]
        left = gray[:, 0]
        right = gray[:, -1]
        border = np.concatenate((top, bottom, left, right)).astype(np.float32)
        return float(np.median(border))

    @staticmethod
    def _decode(image_bytes: bytes) -> np.ndarray | None:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img

    @staticmethod
    def _overlaps(match: MatchResult, existing: list[MatchResult], min_dist: int = 10) -> bool:
        for m in existing:
            if abs(m.x - match.x) < min_dist and abs(m.y - match.y) < min_dist:
                return True
        return False
