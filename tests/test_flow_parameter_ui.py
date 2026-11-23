from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from automadin.engine.session import DeviceSessionService
from automadin.models.flow import AutomationFlow
from automadin.models.flow_parameter import (
    FlowParameterDefinition,
    FlowParameterError,
    FlowParameterType,
)
from automadin.models.run_config import SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.ui.flow_editor.editor_view import FlowEditorView
from automadin.ui.launcher.parameter_overrides import ParameterOverridesWidget
from automadin.ui.launcher.run_config_dialog import RunConfigDialog
from automadin.ui.launcher.launcher_view import LauncherView


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_parameter_override_widget_returns_only_checked_typed_values() -> None:
    _app()
    widget = ParameterOverridesWidget()
    widget.set_parameters(
        [
            FlowParameterDefinition("attempts", FlowParameterType.INTEGER, 2),
            FlowParameterDefinition("dry_run", FlowParameterType.BOOLEAN, False),
        ],
        {"attempts": "3"},
    )

    attempts_checkbox, attempts_editor = widget._rows["attempts"]
    dry_run_checkbox, dry_run_editor = widget._rows["dry_run"]
    assert attempts_checkbox.isChecked()
    assert not dry_run_checkbox.isChecked()
    attempts_editor.setText("4")
    dry_run_checkbox.setChecked(True)
    dry_run_editor.setText("true")

    assert widget.overrides() == {"attempts": 4, "dry_run": True}


def test_parameter_override_widget_rejects_stale_saved_names() -> None:
    _app()
    widget = ParameterOverridesWidget()
    widget.set_parameters(
        [FlowParameterDefinition("current", FlowParameterType.TEXT, "")],
        {"removed": "old"},
    )

    with pytest.raises(FlowParameterError, match="removed"):
        widget.overrides()


def test_run_setup_dialog_round_trips_parameter_overrides(tmp_path: Path) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "run-parameter-ui.db"))
    flow = AutomationFlow(
        "Parameterized",
        parameters=[
            FlowParameterDefinition("attempts", FlowParameterType.INTEGER, 2)
        ],
    )
    database.save_flow(flow.name, flow)
    existing = SavedRunConfig(
        name="Setup",
        flow_name=flow.name,
        device_id="android:test",
        parameter_overrides={"attempts": 3},
    )

    dialog = RunConfigDialog(database, existing=existing)
    _checkbox, editor = dialog._parameter_overrides._rows["attempts"]
    editor.setText("5")

    assert dialog.get_saved_run_config().parameter_overrides == {"attempts": 5}
    dialog.deleteLater()
    database.close()


def test_flow_editor_parameter_change_marks_document_dirty(tmp_path: Path) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "flow-parameter-editor.db"))
    editor = FlowEditorView(database, DeviceSessionService())
    editor._flow = AutomationFlow("Legacy")
    editor._mark_clean()
    definition = FlowParameterDefinition(
        "account",
        FlowParameterType.TEXT,
        "personal",
    )

    class _AcceptedDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def exec() -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        @staticmethod
        def parameters() -> list[FlowParameterDefinition]:
            return [definition]

    with patch(
        "automadin.ui.flow_editor.editor_view.FlowParametersDialog",
        _AcceptedDialog,
    ):
        editor._edit_flow_parameters()

    assert editor._flow.parameters == [definition]
    assert editor.has_unsaved_changes
    assert editor._flow_parameters_btn in editor._run_step_flow_mutation_controls
    editor.deleteLater()
    database.close()


def test_manual_launch_reprepares_with_transient_parameter_values() -> None:
    definition = FlowParameterDefinition(
        "attempts",
        FlowParameterType.INTEGER,
        2,
    )
    prepared = SimpleNamespace(
        flow=AutomationFlow("Parameterized", parameters=[definition]),
        parameter_values={"attempts": 3},
        preflight=object(),
    )
    calls: list[object] = []

    class _Coordinator:
        def prepare(self, _config, **kwargs):
            calls.append(kwargs.get("transient_parameter_overrides"))
            return prepared

    class _AcceptedDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def exec() -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        @staticmethod
        def overrides() -> dict[str, int]:
            return {"attempts": 4}

    fake_view = SimpleNamespace(_coordinator=lambda: _Coordinator())
    with (
        patch(
            "automadin.ui.launcher.launcher_view.ManualParameterOverridesDialog",
            _AcceptedDialog,
        ),
        patch(
            "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
            return_value=False,
        ),
    ):
        LauncherView._start_config(  # type: ignore[arg-type]
            fake_view,
            SavedRunConfig("Setup", "Parameterized", "android:test"),
        )

    assert calls == [None, {"attempts": 4}]


def test_manual_launch_can_use_saved_values_without_prompting() -> None:
    prepared = SimpleNamespace(
        flow=AutomationFlow(
            "Parameterized",
            parameters=[
                FlowParameterDefinition("attempts", FlowParameterType.INTEGER, 2)
            ],
        ),
        parameter_values={"attempts": 2},
        preflight=object(),
    )
    calls: list[object] = []

    class _Coordinator:
        def prepare(self, _config, **kwargs):
            calls.append(kwargs.get("transient_parameter_overrides"))
            return prepared

    fake_view = SimpleNamespace(_coordinator=lambda: _Coordinator())
    with (
        patch(
            "automadin.ui.launcher.launcher_view.ManualParameterOverridesDialog"
        ) as dialog,
        patch(
            "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
            return_value=False,
        ),
    ):
        LauncherView._start_config(  # type: ignore[arg-type]
            fake_view,
            SavedRunConfig(
                "Setup",
                "Parameterized",
                "android:test",
                ask_for_values=False,
            ),
        )

    dialog.assert_not_called()
    assert calls == [None]


def test_schedule_run_now_uses_saved_values_without_manual_prompt() -> None:
    config = SavedRunConfig("Setup", "Parameterized", "android:test")
    starts: list[tuple[SavedRunConfig, bool]] = []
    fake_view = SimpleNamespace(
        _db=SimpleNamespace(load_run_config_by_id=lambda _run_id: config),
        _start_config=lambda saved, *, prompt_for_parameters: starts.append(
            (saved, prompt_for_parameters)
        ),
    )

    LauncherView._run_config_by_id(fake_view, 7)  # type: ignore[arg-type]

    assert starts == [(config, False)]
