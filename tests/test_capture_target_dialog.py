from __future__ import annotations

import copy
import inspect
import io
import os
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from automadin.engine.session import DeviceSessionService
from automadin.models.flow import FlowCommand
from automadin.services.database import DatabaseService
from automadin.services.find_image import MatchResult
from automadin.services.screen_capture import (
    CaptureCoordinateSpace,
    CaptureFrame,
    CaptureSource,
    CaptureSourceKind,
)
from automadin.ui.flow_editor.capture_target_dialog import (
    CaptureTargetDialog,
    CaptureTargetDraft,
)
from automadin.ui.flow_editor.editor_view import FlowEditorView, _load_command_types
from automadin.ui.flow_editor.param_editor import ParamEditorWidget
from automadin.ui.capture_controller import CaptureStartError


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _png_bytes(
    size: tuple[int, int] = (8, 6),
    color: tuple[int, int, int] = (40, 120, 200),
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _file_frame(size: tuple[int, int] = (8, 6)) -> CaptureFrame:
    return CaptureFrame(
        png_bytes=_png_bytes(size),
        width=size[0],
        height=size[1],
        source=CaptureSource(
            kind=CaptureSourceKind.FILE,
            identifier="screen.png",
            label="screen.png",
            coordinate_space=CaptureCoordinateSpace.OFFLINE_IMAGE,
        ),
    )


class _FakeCaptureController(QObject):
    busy_changed = Signal(bool)
    capture_succeeded = Signal(str, object)
    capture_failed = Signal(str, str)
    capture_cancelled = Signal(str)
    capture_finished = Signal(str)

    def __init__(self, *, busy: bool = False, active: str | None = None) -> None:
        super().__init__()
        self._busy = busy
        self.active = active
        self.cancelled: list[str] = []
        self.start_error: Exception | None = None

    @property
    def is_busy(self) -> bool:
        return self._busy

    def is_operation_active(self, operation_id: str | None) -> bool:
        return operation_id is not None and operation_id == self.active

    def cancel(self, operation_id: str | None = None) -> bool:
        if operation_id is None:
            raise AssertionError("The dialog must never issue cancel(None)")
        self.cancelled.append(operation_id)
        return True

    def capture_session(self, _session: object) -> str:
        if self.start_error is not None:
            raise self.start_error
        self._busy = True
        self.active = "capture"
        self.busy_changed.emit(True)
        return "capture"

    def import_file(self, _path: object) -> str:
        return self.capture_session(object())


class _Selector:
    def get_selected_session(self) -> object:
        return object()


def test_capture_target_dialog_stages_draft_without_writing_database(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "draft.db"))
    dialog = CaptureTargetDialog(
        database,
        supported_params={"threshold", "color_match", "color_tolerance"},
        current_params={"threshold": 0.84},
        suggested_group="Mobile",
    )
    try:
        dialog._capture_operation_id = "import"
        dialog._on_capture_succeeded("import", _file_frame())
        dialog._on_crop_selected(QRect(1, 2, 4, 3))
        dialog._name_edit.setText("Collect Reward")
        dialog._threshold_spin.setValue(0.91)
        dialog._color_match_check.setChecked(True)
        dialog._tolerance_spin.setValue(0.22)

        dialog.accept()

        assert dialog.result() == QDialog.DialogCode.Accepted
        assert database.list_images() == []
        assert dialog.draft is not None
        assert dialog.draft.name == "Mobile/Collect Reward"
        assert (dialog.draft.width, dialog.draft.height) == (4, 3)
        assert dialog.draft.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        assert dialog.draft.updates_dict() == {
            "threshold": 0.91,
            "color_match": True,
            "color_tolerance": 0.22,
        }
    finally:
        dialog.deleteLater()
        database.close()


def test_capture_target_dialog_keeps_actions_reachable_at_wide_and_compact_sizes(
    tmp_path: Path,
) -> None:
    app = _app()
    database = DatabaseService(str(tmp_path / "geometry.db"))
    dialog = CaptureTargetDialog(
        database,
        supported_params={"threshold", "color_match", "color_tolerance"},
    )
    try:
        dialog.show()
        dialog.resize(1280, 800)
        app.processEvents()
        assert dialog._splitter.orientation() == Qt.Orientation.Horizontal
        assert dialog._save_btn.isVisibleTo(dialog)
        assert dialog._cancel_btn.isVisibleTo(dialog)
        assert (
            dialog._splitter.widget(1).horizontalScrollBarPolicy()
            == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        dialog.resize(900, 650)
        app.processEvents()
        assert dialog._splitter.orientation() == Qt.Orientation.Horizontal
        assert dialog._save_btn.isVisibleTo(dialog)
        assert dialog._cancel_btn.isVisibleTo(dialog)

        dialog.resize(800, 600)
        app.processEvents()
        assert dialog._splitter.orientation() == Qt.Orientation.Vertical
        assert dialog._settings_scroll.viewport().height() >= 140
        assert dialog._save_btn.isVisibleTo(dialog)
        assert dialog._cancel_btn.isVisibleTo(dialog)
    finally:
        dialog.close()
        dialog.deleteLater()
        database.close()


def test_capture_target_dialog_drafts_reviewed_replacement_without_overwriting(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "replace-draft.db"))
    original = _png_bytes((3, 3), (180, 30, 20))
    database.save_image("IOM/Button", original, 3, 3)
    revision = database.image_revision("iom\\button")
    assert revision is not None
    dialog = CaptureTargetDialog(database, suggested_group="iom")
    try:
        dialog._capture_operation_id = "import"
        dialog._on_capture_succeeded("import", _file_frame())
        dialog._on_crop_selected(QRect(0, 0, 3, 3))
        dialog._name_edit.setText("button")

        with patch.object(
            dialog,
            "_review_collision",
            return_value=(revision.name, revision),
        ):
            dialog.accept()

        assert dialog.draft is not None
        assert dialog.draft.name == "IOM/Button"
        assert dialog.draft.replace_revision == revision
        assert database.load_image("IOM/Button") == original
    finally:
        dialog.deleteLater()
        database.close()


def test_capture_target_dialog_numeric_bounds_update_visible_crop(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "numeric-crop.db"))
    dialog = CaptureTargetDialog(database)
    try:
        dialog._capture_operation_id = "import"
        dialog._on_capture_succeeded("import", _file_frame((20, 12)))
        dialog._on_crop_selected(QRect(2, 3, 8, 5))

        dialog._x_spin.setValue(6)
        dialog._width_spin.setValue(7)

        assert dialog._selection_rect == QRect(6, 3, 7, 5)
        assert (
            dialog._screenshot_widget.get_crop_rect_original()
            == dialog._selection_rect
        )
    finally:
        dialog.deleteLater()
        database.close()


def test_capture_target_dialog_match_test_is_bounded_and_runs_off_ui_thread(
    tmp_path: Path,
) -> None:
    app = _app()
    database = DatabaseService(str(tmp_path / "match-test.db"))
    dialog = CaptureTargetDialog(
        database,
        supported_params={"threshold"},
    )
    try:
        dialog._capture_operation_id = "import"
        dialog._on_capture_succeeded("import", _file_frame((400, 300)))
        dialog._on_crop_selected(QRect(0, 0, 2, 2))

        started = time.monotonic()
        dialog._test_selection()
        dispatch_elapsed = time.monotonic() - started
        assert dispatch_elapsed < 0.5
        assert dialog._match_test_operation_id is not None

        deadline = time.monotonic() + 5.0
        while dialog._match_test_operation_id is not None:
            app.processEvents()
            if time.monotonic() >= deadline:
                raise AssertionError("Bounded background match test did not finish")
            time.sleep(0.005)

        assert dialog._match_tasks == {}
        assert (
            "At least 25 distinct matches"
            in dialog._test_result_label.text()
        )
        assert "consider a larger or more distinctive crop" in (
            dialog._test_result_label.text()
        )
        assert dialog._screenshot_widget.has_overlays
    finally:
        dialog.close()
        dialog.deleteLater()
        database.close()


def test_match_setting_change_invalidates_in_flight_result(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "stale-match.db"))
    dialog = CaptureTargetDialog(
        database,
        supported_params={"threshold"},
    )
    try:
        dialog._match_test_operation_id = "running"
        dialog._threshold_spin.setValue(0.77)

        assert dialog._match_test_stale
        assert dialog._test_result_label.text() == "Settings changed — test again."

        dialog._on_match_test_completed(
            "running",
            [MatchResult(1, 1, 2, 2, 0.99)],
            False,
        )

        assert dialog._match_test_operation_id is None
        assert dialog._test_result_label.text() == "Settings changed — test again."
    finally:
        dialog.deleteLater()
        database.close()


def test_capture_start_failure_restores_truthful_operation_status(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "start-failure.db"))
    controller = _FakeCaptureController()
    controller.start_error = CaptureStartError("Could not start capture.")
    dialog = CaptureTargetDialog(
        database,
        device_selector=_Selector(),
        capture_controller=controller,  # type: ignore[arg-type]
    )
    try:
        dialog._capture_current_screen()

        assert dialog._capture_operation_id is None
        assert dialog._operation_label.text() == "Error"
        assert "Could not start capture" in dialog._status_label.text()
        assert dialog._capture_btn.text() == "Capture Current Screen"
    finally:
        dialog.deleteLater()
        database.close()


def test_dialog_opened_during_foreign_capture_reports_busy_state(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "initial-busy.db"))
    controller = _FakeCaptureController(busy=True, active="foreign")
    dialog = CaptureTargetDialog(
        database,
        capture_controller=controller,  # type: ignore[arg-type]
    )
    try:
        assert dialog._operation_label.text() == "Busy"
        assert (
            dialog._status_label.text()
            == "Another screenshot operation is finishing."
        )
        assert not dialog._capture_btn.isEnabled()
        assert not dialog._import_btn.isEnabled()

        controller._busy = False
        controller.active = None
        controller.busy_changed.emit(False)

        assert dialog._operation_label.text() == "Ready"
        assert dialog._status_label.text() == (
            "Ready to capture or import a screenshot."
        )
    finally:
        dialog.deleteLater()
        database.close()


def test_capture_target_close_never_cancels_foreign_controller_operation(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "foreign.db"))
    controller = _FakeCaptureController(busy=True, active="vision-lab-operation")
    dialog = CaptureTargetDialog(
        database,
        capture_controller=controller,  # type: ignore[arg-type]
    )
    try:
        dialog.reject()
        assert controller.cancelled == []
        assert controller.active == "vision-lab-operation"
    finally:
        dialog.deleteLater()
        database.close()


def test_capture_target_close_cancels_only_its_non_null_operation(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "owned.db"))
    controller = _FakeCaptureController(busy=True, active="owned-operation")
    dialog = CaptureTargetDialog(
        database,
        capture_controller=controller,  # type: ignore[arg-type]
    )
    try:
        dialog._capture_operation_id = "owned-operation"
        dialog.reject()
        assert controller.cancelled == ["owned-operation"]
    finally:
        dialog.deleteLater()
        database.close()


def test_image_list_control_exposes_one_capture_target_action() -> None:
    _app()
    launched: list[str] = []
    widget = ParamEditorWidget(
        {"name": "images", "label": "Images", "type": "image_list"},
        [],
        lambda _name, _value: None,
        capture_target_launcher=launched.append,
    )
    try:
        button = widget.findChild(QPushButton, "captureTargetButton")
        assert button is not None
        assert button.text() == "Capture target..."

        button.click()

        assert launched == ["images"]
    finally:
        widget.deleteLater()


def test_dynamic_image_list_prefers_recording_over_single_capture() -> None:
    _app()
    recorded: list[str] = []
    widget = ParamEditorWidget(
        {
            "name": "images",
            "label": "Learned Poses",
            "type": "image_list",
            "record_dynamic_target": True,
        },
        [],
        lambda _name, _value: None,
        capture_target_launcher=lambda _name: None,
        dynamic_target_launcher=recorded.append,
    )
    try:
        record = widget.findChild(QPushButton, "recordDynamicTargetButton")
        assert record is not None
        assert widget.findChild(QPushButton, "captureTargetButton") is None
        record.click()
        assert recorded == ["images"]
    finally:
        widget.deleteLater()


def test_all_image_driven_commands_share_the_image_list_integration() -> None:
    command_types = _load_command_types()
    image_commands = {
        command_id
        for command_id, definition in command_types.items()
        if any(
            param.get("name") == "images"
            and param.get("type") == "image_list"
            for param in definition.get("params", [])
        )
    }

    assert image_commands == {
        "click_dynamic_target",
        "click_image",
        "find_image",
        "if_image",
        "read_screen_text",
        "while",
        "win_click_image",
        "win_drag_image",
        "win_if_image",
        "win_while",
    }


class _DraftDialog:
    next_draft: CaptureTargetDraft | None = None
    next_results: list[QDialog.DialogCode] = []
    last: _DraftDialog | None = None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        type(self).last = self
        self.draft = type(self).next_draft
        self.results = list(type(self).next_results)
        self.errors: list[str] = []
        self.deleted = False

    def exec(self) -> QDialog.DialogCode:
        return (
            self.results.pop(0)
            if self.results
            else QDialog.DialogCode.Rejected
        )

    def set_commit_error(self, message: str) -> None:
        self.errors.append(message)

    def deleteLater(self) -> None:
        self.deleted = True


def _draft(name: str = "Mobile/New Target") -> CaptureTargetDraft:
    return CaptureTargetDraft(
        name=name,
        png_bytes=_png_bytes((4, 3)),
        width=4,
        height=3,
        source_label="screen.png",
        parameter_updates=(("threshold", 0.92),),
    )


def test_flow_editor_guarded_commit_saves_once_appends_once_and_marks_dirty(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "editor-success.db"))
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        capture_controller=object(),  # type: ignore[arg-type]
    )
    command = FlowCommand(
        "if_image",
        params={"images": ["Mobile/Existing"], "threshold": 0.80},
    )
    editor._flow.commands = [command]
    editor._mark_clean()
    created: list[tuple[str, str]] = []
    editor.resource_saved.connect(
        lambda kind, name: created.append((kind, name))
    )
    _DraftDialog.next_draft = _draft()
    _DraftDialog.next_results = [QDialog.DialogCode.Accepted]

    try:
        with patch(
            "automadin.ui.flow_editor.capture_target_dialog.CaptureTargetDialog",
            _DraftDialog,
        ):
            editor._launch_capture_target(command, "images")

        assert command.params["images"] == [
            "Mobile/Existing",
            "Mobile/New Target",
        ]
        assert command.params["threshold"] == 0.92
        assert editor.has_unsaved_changes
        assert database.load_image("Mobile/New Target") == _draft().png_bytes
        assert created == [("image", "Mobile/New Target")]
        assert _DraftDialog.last is not None and _DraftDialog.last.deleted
    finally:
        editor.deleteLater()
        database.close()


def test_flow_editor_save_failure_restores_exact_flow_and_reopens_draft(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "editor-failure.db"))
    editor = FlowEditorView(
        database,
        DeviceSessionService(),
        capture_controller=object(),  # type: ignore[arg-type]
    )
    command = FlowCommand(
        "if_image",
        params={
            "images": ["Mobile/Existing"],
            "threshold": 0.80,
            "nested": {"kept": [1, 2]},
        },
    )
    editor._flow.commands = [command]
    editor._mark_clean()
    before = copy.deepcopy(command.params)
    _DraftDialog.next_draft = _draft()
    _DraftDialog.next_results = [
        QDialog.DialogCode.Accepted,
        QDialog.DialogCode.Rejected,
    ]

    try:
        with (
            patch(
                "automadin.ui.flow_editor.capture_target_dialog.CaptureTargetDialog",
                _DraftDialog,
            ),
            patch.object(
                database,
                "create_image",
                side_effect=RuntimeError("SENTINEL_PRIVATE_DATABASE_DETAIL"),
            ),
        ):
            editor._launch_capture_target(command, "images")

        assert command.params == before
        assert not editor.has_unsaved_changes
        assert database.list_images() == []
        assert _DraftDialog.last is not None
        assert _DraftDialog.last.deleted
        assert _DraftDialog.last.errors == [
            "The image could not be saved. Your flow and existing images "
            "were not changed. Try again."
        ]
        assert "SENTINEL" not in _DraftDialog.last.errors[0]
    finally:
        editor.deleteLater()
        database.close()


def test_equivalent_image_identity_is_repaired_without_reordering() -> None:
    assert FlowEditorView._append_canonical_image(
        ["First", "iom\\Button", "Last", "IOM/Button"],
        "IOM/Button",
    ) == ["First", "IOM/Button", "Last"]


def test_open_document_image_usage_includes_nested_unsaved_commands(
    tmp_path: Path,
) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "open-usage.db"))
    editor = FlowEditorView(database, DeviceSessionService())
    editor._flow.commands = [
        FlowCommand(
            "if_image",
            params={"images": ["IOM/Parent"]},
            children=[
                FlowCommand(
                    "legacy",
                    params={"image_name": "IOM/Unsaved Child"},
                )
            ],
        )
    ]
    try:
        assert editor._current_document_image_names() == {
            "IOM/Parent",
            "IOM/Unsaved Child",
        }
    finally:
        editor.deleteLater()
        database.close()


def test_flow_editor_capture_controller_dependency_is_keyword_only() -> None:
    parameters = inspect.signature(FlowEditorView.__init__).parameters

    assert parameters["parent"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert (
        parameters["capture_controller"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
