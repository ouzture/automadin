from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QDoubleSpinBox

from automadin.models.device import BrowserDevice, BrowserDeviceConfig, BrowserEngine
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow
from automadin.models.flow_value import FlowInputDefinition
from automadin.models.typed_value import ValueType
from automadin.models.run_config import SavedRunConfig
from automadin.models.visual_rule import VisualRule
from automadin.services.database import DatabaseService
from automadin.ui.flow_editor.param_editor import ParamEditorWidget
from automadin.ui.launcher.run_config_dialog import RunConfigDialog
from automadin.ui.visual_rule_dialog import VisualRuleDialog

_INPUT_ID = "10000000-0000-4000-8000-000000000001"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _FlowDb:
    @staticmethod
    def list_flows() -> list[str]:
        return ["Mobile/Daily/Login", "Website/Checkout/Guest"]


def test_blank_flow_reference_stays_blank_until_user_selects_one() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {"name": "flow_name", "label": "Flow", "type": "flow_reference"},
        "",
        lambda name, value: changes.append((name, value)),
        db=_FlowDb(),
    )

    combo = widget.findChild(QComboBox)
    assert combo is not None
    assert combo.currentData() == ""
    assert changes == []


def test_color_tolerance_editor_uses_normalized_range() -> None:
    _app()
    widget = ParamEditorWidget(
        {
            "name": "color_tolerance",
            "label": "Color Tolerance",
            "type": "double",
            "default": 0.3,
        },
        None,
        lambda _name, _value: None,
    )

    spin = widget.findChild(QDoubleSpinBox)
    assert spin is not None
    assert spin.minimum() == 0.0
    assert spin.maximum() == 1.0
    assert spin.value() == 0.3


def test_run_setup_reuses_shared_folder_casing_and_full_flow_identity() -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = DatabaseService(str(Path(temp_dir) / "run-setup.db"))
        db.save_flow("Mobile/Existing/Login", AutomationFlow("Mobile/Existing/Login"))
        browser_target = BrowserDevice(
            BrowserDeviceConfig(engine=BrowserEngine.CHROMIUM)
        )
        dialog = RunConfigDialog(db, current_device_id=browser_target.id)
        dialog._name_edit.setText("Daily login")
        dialog._group_combo.setCurrentText("mobile/New")

        dialog._on_accept()
        config = dialog.get_saved_run_config()

        assert dialog.result() == QDialog.DialogCode.Accepted
        assert config.group_path == "Mobile/New"
        assert config.flow_name == "Mobile/Existing/Login"
        dialog.deleteLater()
        db.close()


def test_run_setup_editor_repairs_legacy_iom_case_without_bouncing_back() -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = DatabaseService(str(Path(temp_dir) / "run-setup.db"))
        db.save_flow("iom/click_fire_bomb", AutomationFlow("iom/click_fire_bomb"))
        for name, group in (
            ("Fire Bomb", "IOM"),
            ("Fire Bomb Slow", "IOM"),
            ("Google Play", "IOM"),
            ("Phone", "iom"),
        ):
            config = SavedRunConfig(
                name=name,
                group_path=group,
                flow_name="iom/click_fire_bomb",
                device_id="android:test",
            )
            db.save_run_config(name, config)
            payload = config.to_dict()
            payload["group_path"] = group
            db._conn.execute(
                "UPDATE run_configs SET data = ? WHERE name = ?",
                (json.dumps(payload), name),
            )
        db._conn.commit()

        existing = db.load_run_config("Phone")
        dialog = RunConfigDialog(db, existing=existing)

        assert dialog._group_combo.currentText() == "IOM"
        assert dialog._group_combo.currentIndex() == dialog._group_combo.findText("IOM")
        updated = dialog.get_saved_run_config()
        assert updated.group_path == "IOM"
        assert db.update_run_config("Phone", updated)
        assert db.load_run_config("Phone").group_path == "IOM"

        dialog.deleteLater()
        db.close()


def test_visual_rule_editor_selects_saved_folder_item() -> None:
    _app()
    dialog = VisualRuleDialog(
        existing=VisualRule(name="TapTitans2/Chest Blob"),
        known_paths=["TapTitans2/Chest Blob"],
        known_folders=["TapTitans2"],
    )
    try:
        assert dialog._group_combo.currentText() == "TapTitans2"
        assert dialog._group_combo.currentIndex() == dialog._group_combo.findText(
            "TapTitans2"
        )
        assert dialog.get_rule().name == "TapTitans2/Chest Blob"
    finally:
        dialog.deleteLater()
        _app().processEvents()


def test_editing_run_setup_preserves_saved_variables() -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = DatabaseService(str(Path(temp_dir) / "run-setup.db"))
        db.save_flow(
            "Variable flow",
            AutomationFlow(
                "Variable flow",
                value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
                inputs=[
                    FlowInputDefinition(
                        "attempts",
                        ValueType.INTEGER,
                        2,
                        id=_INPUT_ID,
                    )
                ],
            ),
        )
        existing = SavedRunConfig(
            name="Variable run",
            flow_name="Variable flow",
            device_id="android-device",
            variables={"account": "work", "attempts": 3},
            input_overrides={_INPUT_ID: 4},
        )

        dialog = RunConfigDialog(db, existing=existing)
        updated = dialog.get_saved_run_config()

        assert updated.variables == {"account": "work", "attempts": 3}
        assert updated.variables is not existing.variables
        assert updated.input_overrides == {_INPUT_ID: 4}
        assert updated.input_overrides is not existing.input_overrides
        dialog.deleteLater()
        db.close()
