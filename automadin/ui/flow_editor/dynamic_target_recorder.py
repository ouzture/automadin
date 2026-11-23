"""Short recording workflow that learns representative animated target poses."""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import monotonic
from typing import Any

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from automadin.models.resource_path import normalize_resource_path, resource_path_error
from automadin.services.dynamic_target import LearnedPoseSet, learn_pose_set
from automadin.services.screen_capture import CaptureFrame, capture_source_matches_session
from automadin.ui.capture_controller import (
    CaptureBusyError,
    CaptureOperationController,
    CaptureStartError,
)
from automadin.ui.theme import COLORS
from automadin.ui.widgets.interactive_screenshot import InteractiveScreenshotWidget


@dataclass(frozen=True, slots=True)
class RecordedPoseDraft:
    name: str
    png_bytes: bytes
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class DynamicTargetRecordingDraft:
    poses: tuple[RecordedPoseDraft, ...]
    parameter_updates: tuple[tuple[str, object], ...]
    source_surface_width: int
    source_surface_height: int
    source_coordinate_space: str

    def updates_dict(self) -> dict[str, object]:
        return dict(self.parameter_updates)


class DynamicTargetRecorderDialog(QDialog):
    """Record a bounded frame sequence and learn a compact target pose set."""

    RECORD_SECONDS = 3.0
    MAX_FRAMES = 30
    FRAME_INTERVAL_MS = 100

    def __init__(
        self,
        database: Any,
        *,
        device_selector: Any,
        capture_controller: CaptureOperationController,
        suggested_name: str = "Target",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Record Dynamic Target")
        self.setObjectName("dynamicTargetRecorderDialog")
        self.setModal(True)
        self.resize(980, 720)
        self.setMinimumSize(720, 560)
        self._database = database
        self._device_selector = device_selector
        self._capture_controller = capture_controller
        self._operation_id: str | None = None
        self._recording_deadline = 0.0
        self._capture_started_at = 0.0
        self._continue_recording = False
        self._frames: list[CaptureFrame] = []
        self._frame_index = 0
        self._frame_selections: dict[int, QRect] = {}
        self._reference_frame_index: int | None = None
        self._learned: LearnedPoseSet | None = None
        self._draft: DynamicTargetRecordingDraft | None = None
        self._closed = False
        self._playback_timer = QTimer(self)
        self._playback_timer.setInterval(150)
        self._playback_timer.timeout.connect(self._advance_playback)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("Teach Automadin the target while it moves")
        title.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 15pt; font-weight: 700;"
        )
        root.addWidget(title)
        self._instruction = QLabel(
            "Record a short animation, then draw one close rectangle around the "
            "clickable object. Exclude the carrier and background when possible."
        )
        self._instruction.setWordWrap(True)
        self._instruction.setStyleSheet(f"color: {COLORS['text_secondary']};")
        root.addWidget(self._instruction)

        self._canvas = InteractiveScreenshotWidget(enable_crop=True)
        self._canvas.setObjectName("dynamicTargetRecordingCanvas")
        self._canvas.set_crop_requires_shift(False)
        self._canvas.crop_selected.connect(self._on_crop_selected)
        self._canvas.setText("Connect a target and choose Record 3 seconds")
        root.addWidget(self._canvas, 1)

        frame_row = QHBoxLayout()
        self._previous_frame = QPushButton("Previous")
        self._previous_frame.setObjectName("dynamicTargetPreviousFrame")
        self._previous_frame.clicked.connect(lambda: self._show_frame(self._frame_index - 1))
        frame_row.addWidget(self._previous_frame)
        self._frame_label = QLabel("No frames")
        self._frame_label.setObjectName("dynamicTargetFrameLabel")
        self._frame_label.setMinimumWidth(110)
        frame_row.addWidget(self._frame_label)
        self._frame_slider = QSlider(Qt.Orientation.Horizontal)
        self._frame_slider.setObjectName("dynamicTargetFrameSlider")
        self._frame_slider.setRange(0, 0)
        self._frame_slider.valueChanged.connect(self._show_frame)
        frame_row.addWidget(self._frame_slider, 1)
        self._next_frame = QPushButton("Next")
        self._next_frame.setObjectName("dynamicTargetNextFrame")
        self._next_frame.clicked.connect(lambda: self._show_frame(self._frame_index + 1))
        frame_row.addWidget(self._next_frame)
        self._play_button = QPushButton("Play")
        self._play_button.setObjectName("dynamicTargetPlayFrames")
        self._play_button.setToolTip("Play the captured frames without changing them")
        self._play_button.clicked.connect(self._toggle_playback)
        frame_row.addWidget(self._play_button)
        self._clear_frame_mark = QPushButton("Clear frame mark")
        self._clear_frame_mark.setObjectName("dynamicTargetClearFrameMark")
        self._clear_frame_mark.setToolTip(
            "Remove the hand-drawn reference or correction from this frame"
        )
        self._clear_frame_mark.clicked.connect(self._clear_current_frame_mark)
        frame_row.addWidget(self._clear_frame_mark)
        root.addLayout(frame_row)

        review_row = QHBoxLayout()
        review_row.addWidget(QLabel("Target name"))
        self._name = QLineEdit(suggested_name)
        self._name.setObjectName("dynamicTargetName")
        self._name.setMaximumWidth(280)
        review_row.addWidget(self._name)
        review_row.addWidget(QLabel("Click behavior"))
        self._strategy = QComboBox()
        self._strategy.setObjectName("dynamicTargetStrategy")
        self._strategy.addItem("Click where it is", "latest")
        self._strategy.addItem("Click when it crosses the route centre", "intercept")
        self._strategy.addItem("Click ahead of it", "predict")
        self._strategy.setEnabled(False)
        review_row.addWidget(self._strategy)
        self._record_button = QPushButton("Record 3 seconds")
        self._record_button.setObjectName("dynamicTargetRecordButton")
        self._record_button.setProperty("cssClass", "primary")
        self._record_button.clicked.connect(self._start_recording)
        review_row.addWidget(self._record_button)
        self._learn_button = QPushButton("Learn selected target")
        self._learn_button.setObjectName("dynamicTargetLearnButton")
        self._learn_button.clicked.connect(self._learn_target)
        self._learn_button.setEnabled(False)
        review_row.addWidget(self._learn_button)
        review_row.addStretch()
        root.addLayout(review_row)

        self._poses = QListWidget()
        self._poses.setObjectName("dynamicTargetLearnedPoses")
        self._poses.setViewMode(QListWidget.ViewMode.IconMode)
        self._poses.setMovement(QListWidget.Movement.Static)
        self._poses.setIconSize(QPixmap(64, 64).size())
        self._poses.setMaximumHeight(116)
        self._poses.setVisible(False)
        self._poses.currentRowChanged.connect(self._on_pose_selected)
        root.addWidget(self._poses)

        pose_actions = QHBoxLayout()
        self._remove_pose_button = QPushButton("Remove selected pose")
        self._remove_pose_button.setObjectName("dynamicTargetRemovePoseButton")
        self._remove_pose_button.setToolTip(
            "Exclude this learned appearance from the command; the recorded frame remains"
        )
        self._remove_pose_button.clicked.connect(self._remove_selected_pose)
        self._remove_pose_button.setEnabled(False)
        pose_actions.addWidget(self._remove_pose_button)
        pose_actions.addStretch()
        root.addLayout(pose_actions)

        self._status = QLabel("No recording yet")
        self._status.setObjectName("dynamicTargetRecorderStatus")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {COLORS['text_secondary']};")
        root.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self._save_button.setText("Use learned target")
        self._save_button.setEnabled(False)
        buttons.accepted.connect(self._accept_draft)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        capture_controller.capture_succeeded.connect(self._on_capture_succeeded)
        capture_controller.capture_failed.connect(self._on_capture_failed)
        capture_controller.capture_cancelled.connect(self._on_capture_cancelled)
        capture_controller.capture_finished.connect(self._on_capture_finished)
        self._refresh_frame_controls()

    @property
    def draft(self) -> DynamicTargetRecordingDraft | None:
        return self._draft

    def _selected_session(self):
        getter = getattr(self._device_selector, "get_selected_session", None)
        return getter() if callable(getter) else None

    def _set_status(self, text: str, *, error: bool = False) -> None:
        color = COLORS["error"] if error else COLORS["text_secondary"]
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(text)

    def _start_recording(self) -> None:
        if self._operation_id is not None:
            return
        self._stop_playback()
        if self._selected_session() is None:
            self._set_status("Connect to a target before recording.", error=True)
            return
        self._frames.clear()
        self._frame_index = 0
        self._frame_selections.clear()
        self._reference_frame_index = None
        self._learned = None
        self._draft = None
        self._poses.clear()
        self._poses.setVisible(False)
        self._remove_pose_button.setEnabled(False)
        self._save_button.setEnabled(False)
        self._learn_button.setEnabled(False)
        self._learn_button.setText("Learn selected target")
        self._strategy.setEnabled(False)
        self._frame_slider.setRange(0, 0)
        self._canvas.clear_screenshot()
        self._refresh_frame_controls()
        self._recording_deadline = monotonic() + self.RECORD_SECONDS
        self._record_button.setEnabled(False)
        self._set_status("Recording the selected target…")
        self._capture_next()

    def _capture_next(self) -> None:
        if self._closed:
            return
        session = self._selected_session()
        if session is None:
            self._finish_recording("The selected target disconnected.", error=True)
            return
        try:
            self._capture_started_at = monotonic()
            self._operation_id = self._capture_controller.capture_session(session)
        except (CaptureBusyError, CaptureStartError) as exc:
            self._operation_id = None
            self._finish_recording(str(exc), error=True)

    def _on_capture_succeeded(self, operation_id: str, frame: object) -> None:
        if self._closed or operation_id != self._operation_id:
            return
        if not isinstance(frame, CaptureFrame):
            self._continue_recording = False
            self._set_status("Capture returned an invalid frame.", error=True)
            return
        session = self._selected_session()
        if session is None or not capture_source_matches_session(frame.source, session):
            self._continue_recording = False
            self._set_status("The selected target changed during recording.", error=True)
            return
        if self._frames and (
            frame.width != self._frames[0].width or frame.height != self._frames[0].height
        ):
            self._continue_recording = False
            self._set_status("The target size changed during recording. Try again.", error=True)
            return
        self._frames.append(frame)
        self._continue_recording = (
            monotonic() < self._recording_deadline
            and len(self._frames) < self.MAX_FRAMES
        )
        self._set_status(f"Recorded {len(self._frames)} frames…")

    def _on_capture_failed(self, operation_id: str, message: str) -> None:
        if operation_id != self._operation_id:
            return
        self._continue_recording = False
        self._set_status(message, error=True)

    def _on_capture_cancelled(self, operation_id: str) -> None:
        if operation_id != self._operation_id:
            return
        self._continue_recording = False
        self._set_status("Recording cancelled.")

    def _on_capture_finished(self, operation_id: str) -> None:
        if operation_id != self._operation_id:
            return
        self._operation_id = None
        if self._continue_recording:
            self._continue_recording = False
            elapsed_ms = (monotonic() - self._capture_started_at) * 1000.0
            delay_ms = max(0, round(self.FRAME_INTERVAL_MS - elapsed_ms))
            QTimer.singleShot(delay_ms, self._capture_next)
            return
        if self._frames:
            self._frame_slider.setRange(0, len(self._frames) - 1)
            self._show_frame(len(self._frames) // 2)
            self._finish_recording(
                f"Recorded {len(self._frames)} frames. Choose a clear frame, draw a "
                "close rectangle around the target, then learn it."
            )
        else:
            self._finish_recording(self._status.text(), error=True)

    def _finish_recording(self, message: str, *, error: bool = False) -> None:
        self._record_button.setEnabled(True)
        self._learn_button.setEnabled(bool(self._frames))
        self._set_status(message, error=error)

    def _show_first_frame(self) -> None:
        self._show_frame(0)

    def _show_frame(self, requested_index: int) -> None:
        if not self._frames:
            self._refresh_frame_controls()
            return
        index = max(0, min(int(requested_index), len(self._frames) - 1))
        self._frame_index = index
        pixmap = QPixmap()
        if not pixmap.loadFromData(self._frames[index].png_bytes):
            self._set_status("The recorded frame could not be displayed.", error=True)
            return
        self._canvas.set_screenshot_pixmap(pixmap)
        marked = self._frame_selections.get(index)
        if marked is None and self._learned is not None:
            tracked = self._learned.tracked_rects[index]
            marked = QRect(*tracked) if tracked is not None else None
        self._canvas.set_crop_selection_original(marked)
        self._frame_slider.blockSignals(True)
        self._frame_slider.setValue(index)
        self._frame_slider.blockSignals(False)
        self._refresh_frame_controls()

    def _refresh_frame_controls(self) -> None:
        count = len(self._frames)
        state = ""
        if self._frame_index in self._frame_selections:
            state = " · marked"
        elif (
            self._learned is not None
            and self._learned.tracked_rects[self._frame_index] is not None
        ):
            state = " · tracked"
        self._frame_label.setText(
            f"Frame {self._frame_index + 1} of {count}{state}"
            if count
            else "No frames"
        )
        self._frame_slider.setEnabled(count > 1)
        self._play_button.setEnabled(count > 1)
        self._previous_frame.setEnabled(count > 1 and self._frame_index > 0)
        self._next_frame.setEnabled(count > 1 and self._frame_index < count - 1)
        self._clear_frame_mark.setEnabled(self._frame_index in self._frame_selections)

    def _toggle_playback(self) -> None:
        if self._playback_timer.isActive():
            self._stop_playback()
            return
        if len(self._frames) < 2:
            return
        if self._frame_index >= len(self._frames) - 1:
            self._show_frame(0)
        self._play_button.setText("Pause")
        self._playback_timer.start()

    def _advance_playback(self) -> None:
        if self._frame_index >= len(self._frames) - 1:
            self._stop_playback()
            return
        self._show_frame(self._frame_index + 1)

    def _stop_playback(self) -> None:
        self._playback_timer.stop()
        self._play_button.setText("Play")

    def _on_crop_selected(self, rect: QRect) -> None:
        if not self._frames:
            return
        self._frame_selections[self._frame_index] = QRect(rect)
        self._refresh_frame_controls()
        if self._learned is not None:
            self._learn_button.setText("Relearn with corrections")
            self._set_status(
                f"Correction marked on frame {self._frame_index + 1}. Choose "
                "Relearn with corrections to update the poses."
            )

    def _clear_current_frame_mark(self) -> None:
        self._frame_selections.pop(self._frame_index, None)
        if self._reference_frame_index == self._frame_index:
            self._reference_frame_index = (
                min(self._frame_selections) if self._frame_selections else None
            )
        self._show_frame(self._frame_index)
        if self._learned is not None:
            self._learn_button.setText("Relearn with corrections")

    def _refresh_pose_list(self) -> None:
        learned = self._learned
        self._poses.clear()
        if learned is None:
            self._poses.setVisible(False)
            self._remove_pose_button.setEnabled(False)
            return
        for index, pose in enumerate(learned.poses, start=1):
            pixmap = QPixmap()
            pixmap.loadFromData(pose.png_bytes)
            item = QListWidgetItem(QIcon(pixmap), f"Pose {index}")
            item.setData(Qt.ItemDataRole.UserRole, pose.source_frame_index)
            item.setToolTip(
                f"Frame {pose.source_frame_index + 1}; tracking confidence "
                f"{pose.tracking_confidence:.2f}. Select to inspect."
            )
            self._poses.addItem(item)
        self._poses.setVisible(bool(learned.poses))
        self._remove_pose_button.setEnabled(False)

    def _on_pose_selected(self, row: int) -> None:
        learned = self._learned
        valid = learned is not None and 0 <= row < len(learned.poses)
        self._remove_pose_button.setEnabled(bool(valid and len(learned.poses) > 1))
        if valid:
            self._show_frame(learned.poses[row].source_frame_index)

    def _remove_selected_pose(self) -> None:
        learned = self._learned
        row = self._poses.currentRow()
        if learned is None or not 0 <= row < len(learned.poses):
            return
        if len(learned.poses) <= 1:
            self._set_status("Keep at least one learned pose.", error=True)
            return
        removed_frame = learned.poses[row].source_frame_index
        self._learned = replace(
            learned,
            poses=tuple(pose for index, pose in enumerate(learned.poses) if index != row),
        )
        self._refresh_pose_list()
        self._save_button.setEnabled(True)
        self._set_status(
            f"Removed the learned pose from frame {removed_frame + 1}. The recorded "
            "frame and observed movement route were kept."
        )

    def _learn_target(self) -> None:
        visible_rect = self._canvas.get_crop_rect_original()
        if not self._frame_selections and visible_rect is not None:
            self._frame_selections[self._frame_index] = QRect(visible_rect)
        if self._reference_frame_index not in self._frame_selections:
            self._reference_frame_index = (
                self._frame_index
                if self._frame_index in self._frame_selections
                else min(self._frame_selections)
                if self._frame_selections
                else None
            )
        reference_index = self._reference_frame_index
        rect = (
            self._frame_selections.get(reference_index)
            if reference_index is not None
            else None
        )
        if rect is None or rect.width() < 2 or rect.height() < 2:
            self._set_status("Draw a close rectangle around the target first.", error=True)
            return
        try:
            learned = learn_pose_set(
                [frame.png_bytes for frame in self._frames],
                (rect.x(), rect.y(), rect.width(), rect.height()),
                reference_frame_index=reference_index,
                correction_rects={
                    index: (mark.x(), mark.y(), mark.width(), mark.height())
                    for index, mark in self._frame_selections.items()
                },
            )
        except ValueError as exc:
            self._set_status(str(exc), error=True)
            return
        self._learned = learned
        self._refresh_pose_list()
        self._learn_button.setText("Relearn with corrections")
        minimum_coverage = 0.6
        sufficient_coverage = learned.coverage >= minimum_coverage
        self._save_button.setEnabled(True)
        self._strategy.setEnabled(True)
        percent = round(learned.coverage * 100)
        if sufficient_coverage:
            self._set_status(
                f"Learned {len(learned.poses)} representative poses and followed the "
                f"target in {percent}% of recorded frames."
            )
        else:
            self._set_status(
                f"Only {percent}% of the target's motion could be followed. Record "
                "again with a tighter target box and less background, or use this "
                "set and verify it with Test without clicking.",
                error=True,
            )

    def _available_group(self, requested_name: str, pose_count: int) -> str:
        clean = requested_name.strip() or "Target"
        base = normalize_resource_path(f"Dynamic Targets/{clean}")
        error = resource_path_error(base)
        if error:
            raise ValueError(error)
        suffix = 1
        candidate = base
        while any(
            self._database.image_revision(f"{candidate}/pose-{index:02d}") is not None
            for index in range(1, pose_count + 1)
        ):
            suffix += 1
            candidate = f"{base} {suffix}"
        return candidate

    def _accept_draft(self) -> None:
        learned = self._learned
        if learned is None or not self._frames:
            self._set_status("Record and learn the target before saving.", error=True)
            return
        try:
            group = self._available_group(self._name.text(), len(learned.poses))
        except ValueError as exc:
            self._set_status(str(exc), error=True)
            return
        poses = tuple(
            RecordedPoseDraft(
                f"{group}/pose-{index:02d}",
                pose.png_bytes,
                pose.width,
                pose.height,
            )
            for index, pose in enumerate(learned.poses, start=1)
        )
        left, top, right, bottom = learned.search_rect
        strategy = str(self._strategy.currentData() or "latest")
        first_pose = learned.poses[0]
        route_center_x = (left + right) / 2.0
        route_center_y = (top + bottom) / 2.0
        trigger_half_width = max(10.0, first_pose.width * 0.75)
        trigger_half_height = max(10.0, first_pose.height * 0.75)
        source = self._frames[0].source
        self._draft = DynamicTargetRecordingDraft(
            poses=poses,
            parameter_updates=(
                ("target_source", "images"),
                ("images", [pose.name for pose in poses]),
                ("search_region_mode", "fixed"),
                ("search_coordinate_unit", "pixels"),
                ("search_start_x", float(left)),
                ("search_start_y", float(top)),
                ("search_end_x", float(right)),
                ("search_end_y", float(bottom)),
                ("strategy", strategy),
                ("trigger_coordinate_unit", "pixels"),
                ("trigger_start_x", max(0.0, route_center_x - trigger_half_width)),
                ("trigger_start_y", max(0.0, route_center_y - trigger_half_height)),
                (
                    "trigger_end_x",
                    min(float(self._frames[0].width), route_center_x + trigger_half_width),
                ),
                (
                    "trigger_end_y",
                    min(float(self._frames[0].height), route_center_y + trigger_half_height),
                ),
            ),
            source_surface_width=self._frames[0].width,
            source_surface_height=self._frames[0].height,
            source_coordinate_space=source.coordinate_space.value,
        )
        super().accept()

    def reject(self) -> None:
        self._closed = True
        self._stop_playback()
        if self._operation_id is not None:
            self._capture_controller.cancel(self._operation_id)
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closed = True
        self._stop_playback()
        if self._operation_id is not None:
            self._capture_controller.cancel(self._operation_id)
        super().closeEvent(event)


__all__ = [
    "DynamicTargetRecorderDialog",
    "DynamicTargetRecordingDraft",
    "RecordedPoseDraft",
]
