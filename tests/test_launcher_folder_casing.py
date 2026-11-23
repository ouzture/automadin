from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from automadin.models.flow import AutomationFlow
from automadin.models.run_config import SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.ui.launcher.launcher_view import LauncherView


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_runs_tree_merges_legacy_group_case_using_workspace_label() -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = DatabaseService(str(Path(temp_dir) / "runs.db"))
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

        view = LauncherView(db)

        assert view._tree.topLevelItemCount() == 1
        group = view._tree.topLevelItem(0)
        assert group.text(0) == "IOM"
        assert group.childCount() == 4

        view.close()
        view.deleteLater()
        db.close()


def test_pinned_run_setup_remains_in_folder_and_search_returns_it_once() -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = DatabaseService(str(Path(temp_dir) / "runs.db"))
        db.save_flow("Mobile/Flow", AutomationFlow("Mobile/Flow"))
        pinned = SavedRunConfig(
            name="Daily Phone",
            group_path="Mobile",
            flow_name="Mobile/Flow",
            device_id="android:test",
            is_pinned=True,
        )
        db.save_run_config(pinned.name, pinned)
        db.save_run_config(
            "Other",
            SavedRunConfig(
                name="Other",
                flow_name="Mobile/Flow",
                device_id="android:other",
            ),
        )

        view = LauncherView(db)

        pinned_root = view._tree.topLevelItem(0)
        assert pinned_root.text(view._COL_NAME) == "Pinned"
        assert pinned_root.childCount() == 1
        assert pinned_root.child(0).text(view._COL_NAME) == "Daily Phone"
        mobile_root = next(
            view._tree.topLevelItem(index)
            for index in range(view._tree.topLevelItemCount())
            if view._tree.topLevelItem(index).text(view._COL_NAME) == "Mobile"
        )
        assert mobile_root.child(0).text(view._COL_NAME) == "Daily Phone"

        view._search.setText("daily phone")

        assert view._tree.topLevelItemCount() == 1
        result = view._tree.topLevelItem(0)
        assert result.text(view._COL_NAME) == "Daily Phone"
        assert result.text(view._COL_PIN) == "★"

        view._tree.setCurrentItem(result)
        view._toggle_selected_pin()
        assert not db.load_run_config("Daily Phone").is_pinned

        view.close()
        view.deleteLater()
        db.close()
