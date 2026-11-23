from __future__ import annotations

from PySide6.QtWidgets import QApplication

from automadin.models.flow import AutomationFlow
from automadin.models.resolution_scaling import (
    ResolutionScalingMode,
    ResolutionScalingOverride,
    ResolutionScalingSettings,
)
from automadin.models.run_config import RunConfig, SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.ui.flow_editor.resolution_scaling_dialog import (
    ResolutionScalingDialog,
)
from automadin.ui.launcher.run_config_dialog import RunConfigDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_flow_scaling_dialog_builds_adaptive_settings() -> None:
    _app()
    dialog = ResolutionScalingDialog(ResolutionScalingSettings())
    try:
        dialog._enabled.setChecked(True)
        dialog._reference_width.setValue(1280)
        dialog._reference_height.setValue(720)

        settings = dialog.settings()

        assert settings.mode is ResolutionScalingMode.ADAPTIVE
        assert settings.reference_size == (1280, 720)
    finally:
        dialog.deleteLater()


def test_run_setup_restores_resolution_scaling_override(tmp_path) -> None:
    _app()
    database = DatabaseService(str(tmp_path / "scaling-ui.db"))
    database.save_flow("Flow", AutomationFlow("Flow"))
    saved = SavedRunConfig(
        "Run",
        "Flow",
        "android:device",
        config=RunConfig(
            resolution_scaling_override=ResolutionScalingOverride.CUSTOM,
            custom_scale_percent=125,
        ),
    )
    dialog = RunConfigDialog(database, existing=saved)
    try:
        restored = dialog.get_saved_run_config().config
        assert restored.resolution_scaling_override is ResolutionScalingOverride.CUSTOM
        assert restored.custom_scale_percent == 125
    finally:
        dialog.deleteLater()
        database.close()
