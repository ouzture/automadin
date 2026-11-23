from __future__ import annotations

from io import BytesIO
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PIL import Image
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication
import pytest

from automadin.services.database import (
    DatabaseService,
    ImageAlreadyExistsError,
    StoredImage,
)
from automadin.execution.handlers.visual import _hook_seed_observation
from automadin.execution.flow_values import RuntimeTypedValue
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType
from automadin.models.visual_rule import VisualRule
from automadin.services.dynamic_target import (
    TargetObservation,
    anchor_point,
    choose_associated,
    estimate_motion,
    learn_pose_set,
    predict_point,
)
from automadin.services.screen_capture import (
    CaptureCoordinateSpace,
    CaptureFrame,
    CaptureSource,
    CaptureSourceKind,
)
from automadin.ui.capture_controller import CaptureOperationController
from automadin.ui.flow_editor.dynamic_target_recorder import (
    DynamicTargetRecorderDialog,
)


def test_dynamic_target_image_detection_chooses_strongest_pose(monkeypatch) -> None:
    from automadin.execution.handlers import visual

    command = FlowCommand(
        "click_dynamic_target",
        params={"target_source": "images", "images": ["pose-a", "pose-b"]},
    )
    context = SimpleNamespace()
    monkeypatch.setattr(visual, "decode_screenshot", lambda _png: np.zeros((20, 30, 3)))
    monkeypatch.setattr(
        visual,
        "_resolve_image_names",
        lambda _cmd, _ctx: ["pose-a", "pose-b"],
    )
    observed: dict[str, object] = {}

    def find_any(*_args, **kwargs):
        observed.update(kwargs)
        return "pose-b", SimpleNamespace(x=4, y=5, width=6, height=7, confidence=0.96)

    monkeypatch.setattr(visual, "_find_any_image", find_any)

    candidates, surface = visual._dynamic_candidates(command, context, b"png", 1.5)

    assert observed["choose_best"] is True
    assert surface == (30, 20)
    assert candidates[0].identity == "pose-b"
    assert candidates[0].confidence == pytest.approx(0.96)


def _png(image: np.ndarray) -> bytes:
    output = BytesIO()
    Image.fromarray(image[:, :, ::-1]).save(output, format="PNG")
    return output.getvalue()


def _frame(x: int, *, tilted: bool = False) -> bytes:
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    target = np.zeros((16, 20, 3), dtype=np.uint8)
    target[2:14, 2:18] = (20, 190, 245)
    target[5:11, 7:13] = (220, 40, 30)
    if tilted:
        target[2:6, 14:18] = (0, 0, 0)
        target[10:14, 2:6] = (0, 0, 0)
    image[30:46, x : x + 20] = target
    return _png(image)


def test_visual_rule_detection_keeps_command_scope_and_results_absolute(
    monkeypatch,
) -> None:
    from automadin.execution.handlers import visual
    from automadin.services.visual_detection import VisualBlob

    rule = VisualRule(
        name="TapTitans2/Chest Blob",
        type="multi_color_region",
        crop_left_percent=64.35,
        crop_right_percent=32.78,
    )
    command = FlowCommand(
        "click_dynamic_target",
        params={
            "target_source": "visual_rule",
            "target_rule": rule.name,
            "search_region_mode": "fixed",
            "search_coordinate_unit": "pixels",
            "search_start_x": 128,
            "search_start_y": 480,
            "search_end_x": 668,
            "search_end_y": 626,
        },
    )
    context = SimpleNamespace(
        _executor=SimpleNamespace(
            _db=SimpleNamespace(load_visual_rule=lambda _name: rule)
        ),
        interpolate=lambda value: value,
    )
    observed: dict[str, object] = {}

    def detect(image, selected_rule, scope):
        observed.update(image=image, rule=selected_rule, scope=scope)
        return [VisualBlob(522, 542, 200, 0.7, (516, 538, 12, 8))]

    monkeypatch.setattr(visual, "detect_blobs_in_scope", detect)
    screenshot = _png(np.zeros((640, 800, 3), dtype=np.uint8))

    candidates, surface = visual._dynamic_candidates(command, context, screenshot, 1.5)

    assert observed["image"].shape[:2] == (640, 800)
    assert observed["scope"] == (128, 480, 668, 626)
    assert observed["rule"] is rule
    assert surface == (800, 640)
    assert (candidates[0].x, candidates[0].y) == (516, 538)


def test_pose_learning_tracks_motion_and_keeps_distinct_animation() -> None:
    learned = learn_pose_set(
        [_frame(10), _frame(16), _frame(22, tilted=True), _frame(28, tilted=True)],
        (10, 30, 20, 16),
        minimum_pose_difference=0.01,
    )

    assert learned.tracked_frames == 4
    assert learned.total_frames == 4
    assert learned.coverage == 1.0
    assert len(learned.poses) >= 2
    assert learned.search_rect[0] <= 10
    assert learned.search_rect[2] >= 48


def test_pose_learning_can_start_from_middle_and_track_both_directions() -> None:
    learned = learn_pose_set(
        [_frame(10), _frame(16), _frame(22, tilted=True), _frame(28, tilted=True)],
        (22, 30, 20, 16),
        reference_frame_index=2,
        minimum_pose_difference=0.01,
    )

    assert learned.coverage == 1.0
    assert learned.tracked_rects[0] is not None
    assert learned.tracked_rects[3] is not None
    assert {pose.source_frame_index for pose in learned.poses}.issubset({0, 1, 2, 3})


def test_pose_learning_uses_correction_anchor_after_tracking_gap() -> None:
    learned = learn_pose_set(
        [_frame(10), _frame(80, tilted=True), _frame(86, tilted=True)],
        (10, 30, 20, 16),
        correction_rects={1: (80, 30, 20, 16)},
        minimum_pose_difference=0.01,
    )

    assert learned.coverage == 1.0
    assert learned.tracked_rects[1] == (80, 30, 20, 16)
    assert learned.tracked_rects[2] is not None


def test_association_rejects_nearer_object_with_implausible_size_change() -> None:
    previous = TargetObservation(0.0, 10, 10, 20, 20, 0.9, "pose-1")
    wrong = TargetObservation(0.1, 12, 12, 2, 2, 0.99, "pose-1")
    correct = TargetObservation(0.1, 24, 10, 20, 20, 0.8, "pose-2")

    assert choose_associated(
        [wrong, correct], previous, max_movement_px=40
    ) == correct


def test_motion_fit_and_prediction_use_all_observations_and_cap_distance() -> None:
    observations = [
        TargetObservation(0.0, 0, 0, 10, 10, 0.9),
        TargetObservation(0.1, 10, 1, 10, 10, 0.9),
        TargetObservation(0.2, 20, 0, 10, 10, 0.9),
        TargetObservation(0.3, 30, 1, 10, 10, 0.9),
    ]

    motion = estimate_motion(observations)

    assert motion is not None
    assert 95 <= motion.velocity_x <= 105
    assert motion.residual_px < 1
    predicted = predict_point(
        observations[-1],
        motion,
        lead_time_ms=1000,
        max_distance_px=20,
    )
    assert 54 <= predicted[0] <= 56


def test_target_anchor_uses_relative_offsets_for_different_pose_sizes() -> None:
    small = TargetObservation(0, 10, 20, 20, 10, 0.9)
    large = TargetObservation(0, 10, 20, 40, 20, 0.9)

    assert anchor_point(small, "center", offset_x_percent=25) == (25, 25)
    assert anchor_point(large, "center", offset_x_percent=25) == (40, 30)


def test_pose_batch_creation_is_atomic_on_name_collision(tmp_path) -> None:
    database = DatabaseService(str(tmp_path / "poses.db"))
    try:
        database.create_image("Taken", _frame(10), 120, 80)
        with np.testing.assert_raises(ImageAlreadyExistsError):
            database.create_images(
                [
                    StoredImage("New Pose", _frame(10), 120, 80),
                    StoredImage("Taken", _frame(16), 120, 80),
                ]
            )
        assert database.load_image("New Pose") is None
        assert database.load_image("Taken") is not None
    finally:
        database.close()


def test_recorder_turns_recorded_frames_into_one_compact_command_draft(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    database = DatabaseService(str(tmp_path / "recorder.db"))
    controller = CaptureOperationController(SimpleNamespace())
    source = CaptureSource(
        CaptureSourceKind.ANDROID,
        "device",
        "Android device",
        CaptureCoordinateSpace.ANDROID_DISPLAY,
        session_id="session",
        session_token=1,
    )
    frames = [
        CaptureFrame(payload, 120, 80, source)
        for payload in (_frame(10), _frame(16), _frame(22, tilted=True))
    ]
    dialog = DynamicTargetRecorderDialog(
        database,
        device_selector=SimpleNamespace(get_selected_session=lambda: None),
        capture_controller=controller,
        suggested_name="Chest",
    )
    try:
        dialog._frames = frames
        dialog._show_first_frame()
        dialog._canvas.set_crop_selection_original(QRect(10, 30, 20, 16))
        dialog._learn_target()

        assert dialog._learned is not None
        assert dialog._save_button.isEnabled()
        assert dialog._poses.count() >= 1

        dialog._accept_draft()
        draft = dialog.draft
        assert draft is not None
        updates = draft.updates_dict()
        assert updates["target_source"] == "images"
        assert updates["search_region_mode"] == "fixed"
        assert len(updates["images"]) == len(draft.poses)
        assert all(name.startswith("Dynamic Targets/Chest/") for name in updates["images"])
    finally:
        dialog.deleteLater()
        controller.shutdown()
        database.close()
        app.processEvents()


def test_recorder_scrubs_corrects_and_removes_pose_without_shrinking_route(
    tmp_path,
) -> None:
    app = QApplication.instance() or QApplication([])
    database = DatabaseService(str(tmp_path / "review.db"))
    controller = CaptureOperationController(SimpleNamespace())
    source = CaptureSource(
        CaptureSourceKind.ANDROID,
        "device",
        "Android device",
        CaptureCoordinateSpace.ANDROID_DISPLAY,
        session_id="session",
        session_token=1,
    )
    dialog = DynamicTargetRecorderDialog(
        database,
        device_selector=SimpleNamespace(get_selected_session=lambda: None),
        capture_controller=controller,
        suggested_name="Chest",
    )
    try:
        dialog._frames = [
            CaptureFrame(payload, 120, 80, source)
            for payload in (
                _frame(10),
                _frame(16),
                _frame(22, tilted=True),
                _frame(28, tilted=True),
            )
        ]
        dialog._frame_slider.setRange(0, 3)
        dialog._show_frame(2)
        dialog._canvas.set_crop_selection_original(QRect(22, 30, 20, 16))
        dialog._on_crop_selected(QRect(22, 30, 20, 16))
        dialog._learn_target()

        assert dialog._reference_frame_index == 2
        assert dialog._learned is not None
        assert dialog._learned.coverage == 1.0
        assert "Frame 3 of 4" in dialog._frame_label.text()

        dialog._show_frame(0)
        dialog._toggle_playback()
        assert dialog._playback_timer.isActive()
        dialog._advance_playback()
        assert dialog._frame_index == 1
        dialog._stop_playback()
        assert not dialog._playback_timer.isActive()

        original_route = dialog._learned.search_rect
        original_count = len(dialog._learned.poses)
        assert original_count >= 2
        dialog._poses.setCurrentRow(0)
        dialog._remove_selected_pose()

        assert dialog._learned.search_rect == original_route
        assert len(dialog._learned.poses) == original_count - 1
        assert dialog._poses.count() == original_count - 1

        dialog._show_frame(1)
        dialog._on_crop_selected(QRect(16, 30, 20, 16))
        assert 1 in dialog._frame_selections
        assert "marked" in dialog._frame_label.text()
        dialog._clear_current_frame_mark()
        assert 1 not in dialog._frame_selections
    finally:
        dialog.deleteLater()
        controller.shutdown()
        database.close()
        app.processEvents()


def test_dynamic_target_can_reuse_matching_hook_evidence() -> None:
    builtins = {
        "_matched_image": RuntimeTypedValue(ValueType.TEXT, "Chest/left"),
        "_match_x": RuntimeTypedValue(ValueType.INTEGER, 50),
        "_match_y": RuntimeTypedValue(ValueType.INTEGER, 40),
        "_match_confidence": RuntimeTypedValue(ValueType.NUMBER, 0.91),
    }
    values = SimpleNamespace(current_builtin=lambda name: builtins.get(name))
    asset = StoredImage("Chest/left", b"pose", 20, 10)
    context = SimpleNamespace(
        flow_values=values,
        interpolate=lambda value: value,
        _executor=SimpleNamespace(
            _db=SimpleNamespace(load_image_asset=lambda _name: asset)
        ),
    )

    observation = _hook_seed_observation(
        FlowCommand(
            "click_dynamic_target",
            params={"target_source": "images", "images": ["Chest/left"]},
        ),
        context,
        1.25,
    )

    assert observation is not None
    assert (observation.x, observation.y, observation.width, observation.height) == (
        40,
        35,
        20,
        10,
    )
    assert observation.confidence == 0.91
