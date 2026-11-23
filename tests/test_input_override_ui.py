from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

from automadin.execution.flow_values import FlowRuntimeValueError
from automadin.execution.run_coordinator import RunPreparationError
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow, FlowCommand
from automadin.models.flow_value import FlowInputDefinition
from automadin.models.run_config import SavedRunConfig
from automadin.models.typed_value import ValueType
from automadin.ui.launcher.input_overrides import InputOverridesWidget
from automadin.ui.launcher.launcher_view import LauncherView
from automadin.services.database import DatabaseService
from automadin.ui.launcher.run_config_dialog import RunConfigDialog

_INPUT_ID = "10000000-0000-4000-8000-000000000001"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_optional_input_is_unchecked_and_emits_only_selected_override() -> None:
    _app()
    definition = FlowInputDefinition(
        "attempts",
        ValueType.INTEGER,
        2,
        id=_INPUT_ID,
    )
    widget = InputOverridesWidget()
    widget.set_inputs([definition])
    checkbox, editor = widget._rows[_INPUT_ID]

    assert not checkbox.isChecked()
    assert not editor.isEnabled()
    assert editor.text() == "2"
    assert widget.overrides() == {}

    checkbox.setChecked(True)
    editor.setText("4")
    assert widget.overrides() == {_INPUT_ID: 4}


def test_missing_required_input_is_selected_and_validated() -> None:
    _app()
    definition = FlowInputDefinition(
        "attempts",
        ValueType.INTEGER,
        id=_INPUT_ID,
    )
    widget = InputOverridesWidget()
    widget.set_inputs([definition])
    checkbox, editor = widget._rows[_INPUT_ID]

    assert checkbox.isChecked()
    assert not checkbox.isEnabled()
    with pytest.raises(FlowRuntimeValueError, match="requires an integer"):
        widget.overrides()

    editor.setText("0")
    assert widget.overrides() == {_INPUT_ID: 0}


def test_saved_falsey_value_is_baseline_and_stale_ids_are_rejected() -> None:
    _app()
    definition = FlowInputDefinition(
        "dry_run",
        ValueType.BOOLEAN,
        True,
        id=_INPUT_ID,
    )
    widget = InputOverridesWidget()
    widget.set_inputs([definition], {_INPUT_ID: False})
    checkbox, editor = widget._rows[_INPUT_ID]

    assert not checkbox.isChecked()
    assert editor.text() == "false"
    assert widget.overrides() == {}

    widget.set_inputs(
        [definition],
        {"ffffffff-ffff-4fff-8fff-ffffffffffff": False},
    )
    with pytest.raises(FlowRuntimeValueError, match="stale IDs"):
        widget.overrides()


def test_saved_mode_persists_checked_values_and_requires_missing_input() -> None:
    _app()
    required = FlowInputDefinition(
        "attempts",
        ValueType.INTEGER,
        id=_INPUT_ID,
    )
    widget = InputOverridesWidget(mode="saved")
    widget.set_inputs([required])
    checkbox, editor = widget._rows[_INPUT_ID]

    assert checkbox.isChecked()
    assert not checkbox.isEnabled()
    with pytest.raises(FlowRuntimeValueError, match="requires an integer"):
        widget.overrides()

    editor.setText("0")
    assert widget.overrides() == {_INPUT_ID: 0}

    widget.set_inputs([required], {_INPUT_ID: 3})
    existing_checkbox, _existing_editor = widget._rows[_INPUT_ID]
    existing_checkbox.setChecked(False)
    with pytest.raises(FlowRuntimeValueError, match="Required flow Inputs are missing"):
        widget.overrides()


def test_saved_mode_requires_explicit_stale_cleanup() -> None:
    _app()
    definition = FlowInputDefinition(
        "attempts",
        ValueType.INTEGER,
        2,
        id=_INPUT_ID,
    )
    stale_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    widget = InputOverridesWidget(mode="saved")
    widget.set_inputs([definition], {_INPUT_ID: 4, stale_id: 9})

    with pytest.raises(FlowRuntimeValueError, match="Remove stale overrides"):
        widget.overrides()

    cleanup = next(
        button
        for button in widget.findChildren(QPushButton)
        if button.text() == "Remove stale overrides"
    )
    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        cleanup.click()

    assert widget.overrides() == {_INPUT_ID: 4}


def test_manual_run_prompts_for_missing_required_inputs_then_reprepares() -> None:
    _app()
    flow = AutomationFlow(
        "Typed flow",
        [FlowCommand("comment")],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[FlowInputDefinition("attempts", ValueType.INTEGER, id=_INPUT_ID)],
    )
    prepared = SimpleNamespace(flow=flow, preflight=object())
    calls: list[object] = []

    class _Coordinator:
        def prepare(self, _config, **kwargs):
            calls.append(kwargs.get("transient_input_overrides"))
            if len(calls) == 1:
                raise RunPreparationError(
                    "Invalid Flow Inputs",
                    "Required flow Inputs are missing: attempts.",
                )
            return prepared

    class _AcceptedDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def exec() -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        @staticmethod
        def overrides() -> dict[str, int]:
            return {_INPUT_ID: 4}

    fake_view = SimpleNamespace(
        _coordinator=lambda: _Coordinator(),
        _db=SimpleNamespace(load_flow=lambda _name: flow),
    )
    with (
        patch(
            "automadin.ui.launcher.launcher_view.ManualInputOverridesDialog",
            _AcceptedDialog,
        ),
        patch(
            "automadin.ui.launcher.launcher_view.confirm_execution_preflight",
            return_value=False,
        ),
    ):
        LauncherView._start_config(  # type: ignore[arg-type]
            fake_view,
            SavedRunConfig("Setup", flow.name, "android:test"),
        )

    assert calls == [None, {_INPUT_ID: 4}]


def test_noninteractive_run_never_prompts_for_missing_input() -> None:
    _app()

    class _Coordinator:
        @staticmethod
        def prepare(_config, **_kwargs):
            raise RunPreparationError(
                "Invalid Flow Inputs",
                "Required flow Inputs are missing: attempts.",
            )

    fake_view = SimpleNamespace(_coordinator=lambda: _Coordinator())
    with (
        patch(
            "automadin.ui.launcher.launcher_view.ManualInputOverridesDialog"
        ) as dialog,
        patch.object(QMessageBox, "critical") as critical,
    ):
        LauncherView._start_config(  # type: ignore[arg-type]
            fake_view,
            SavedRunConfig("Setup", "Typed flow", "android:test"),
            prompt_for_parameters=False,
        )

    dialog.assert_not_called()
    assert critical.call_args.args[1] == "Invalid Flow Inputs"


def test_manual_run_does_not_prompt_for_stale_saved_override() -> None:
    _app()

    class _Coordinator:
        @staticmethod
        def prepare(_config, **_kwargs):
            raise RunPreparationError(
                "Invalid Flow Inputs",
                "Saved Input override 'stale-id' is stale or unknown.",
            )

    fake_view = SimpleNamespace(_coordinator=lambda: _Coordinator())
    with (
        patch(
            "automadin.ui.launcher.launcher_view.ManualInputOverridesDialog"
        ) as dialog,
        patch.object(QMessageBox, "critical") as critical,
    ):
        LauncherView._start_config(  # type: ignore[arg-type]
            fake_view,
            SavedRunConfig("Setup", "Typed flow", "android:test"),
        )

    dialog.assert_not_called()
    assert "stale or unknown" in critical.call_args.args[2]


def test_cancelled_input_prompt_does_not_reprepare_or_register() -> None:
    _app()
    flow = AutomationFlow(
        "Typed flow",
        [FlowCommand("comment")],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[FlowInputDefinition("attempts", ValueType.INTEGER, 2, id=_INPUT_ID)],
    )
    calls = 0

    class _Coordinator:
        def prepare(self, _config, **_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(flow=flow, preflight=object())

    class _CancelledDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def exec() -> QDialog.DialogCode:
            return QDialog.DialogCode.Rejected

    fake_view = SimpleNamespace(_coordinator=lambda: _Coordinator())
    with patch(
        "automadin.ui.launcher.launcher_view.ManualInputOverridesDialog",
        _CancelledDialog,
    ):
        LauncherView._start_config(  # type: ignore[arg-type]
            fake_view,
            SavedRunConfig("Setup", flow.name, "android:test"),
        )

    assert calls == 1


def test_run_setup_editor_round_trips_uuid_input_overrides(tmp_path: Path) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "saved-input-overrides.db"))
    flow = AutomationFlow(
        "Typed flow",
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[FlowInputDefinition("attempts", ValueType.INTEGER, 2, id=_INPUT_ID)],
    )
    database.save_flow(flow.name, flow)
    existing = SavedRunConfig(
        "Setup",
        flow.name,
        "android:test",
        input_overrides={_INPUT_ID: 3},
    )
    dialog = RunConfigDialog(database, existing=existing)
    checkbox, editor = dialog._input_overrides._rows[_INPUT_ID]

    assert not dialog._input_overrides.isHidden()
    assert not dialog._parameter_overrides.isVisible()
    assert checkbox.isChecked()
    editor.setText("5")

    updated = dialog.get_saved_run_config()
    assert updated.input_overrides == {_INPUT_ID: 5}
    assert updated.parameter_overrides == {}
    dialog.deleteLater()
    database.close()


def test_switching_run_setup_flow_does_not_leak_input_ids(tmp_path: Path) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "switch-input-overrides.db"))
    first = AutomationFlow(
        "First",
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[FlowInputDefinition("attempts", ValueType.INTEGER, 2, id=_INPUT_ID)],
    )
    second_input = FlowInputDefinition("account", ValueType.TEXT, "personal")
    second = AutomationFlow(
        "Second",
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[second_input],
    )
    database.save_flow(first.name, first)
    database.save_flow(second.name, second)
    existing = SavedRunConfig(
        "Setup",
        first.name,
        "android:test",
        input_overrides={_INPUT_ID: 3},
    )
    dialog = RunConfigDialog(database, existing=existing)
    dialog._flow_combo.setCurrentIndex(dialog._flow_combo.findData(second.name))

    assert _INPUT_ID not in dialog._input_overrides._rows
    assert second_input.id in dialog._input_overrides._rows
    assert dialog.get_saved_run_config().input_overrides == {}
    dialog.deleteLater()
    database.close()
