"""Capture privacy-safe documentation screenshots from a synthetic workspace.

Run this manually on Windows. The script creates a disposable database, renders
the real application UI, and writes only the two named PNG files under docs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.ui.main_window import MainWindow


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "assets" / "screenshots"


def _process(milliseconds: int = 250) -> None:
    QApplication.processEvents()
    QTest.qWait(milliseconds)
    QApplication.processEvents()


def _save(window: MainWindow, name: str) -> None:
    destination = OUTPUT / name
    if not window.grab().save(str(destination), "PNG"):
        raise RuntimeError(f"Could not save {destination}")


def _seed(database: DatabaseService) -> None:
    flow = AutomationFlow(
        name="Examples/Welcome check",
        commands=[
            FlowCommand(
                "section",
                params={"text": "Open and verify a public example page"},
                collapsed=False,
            ),
            FlowCommand(
                "navigate",
                params={"url": "https://example.com/"},
                comment="Use a public, account-free page in this example.",
                collapsed=False,
            ),
            FlowCommand(
                "assert_url_contains",
                params={"text": "example.com"},
                collapsed=False,
            ),
            FlowCommand("wait", params={"ms": 500}, collapsed=False),
        ],
    )
    database.save_flow(flow.name, flow)
    database.save_run_config(
        "Example status - Chromium",
        SavedRunConfig(
            name="Example status - Chromium",
            group_path="Daily checks",
            flow_name=flow.name,
            device_id="chromium-desktop",
            ask_for_values=False,
            is_pinned=True,
        ),
    )


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)

    with tempfile.TemporaryDirectory(prefix="automadin-docs-") as temp:
        os.environ["AUTOMADIN_DATA_DIR"] = temp
        database = DatabaseService(str(Path(temp) / "automadin.db"))
        _seed(database)
        window = MainWindow(database)
        window.resize(1600, 960)
        window.show()
        _process(900)

        flow = database.load_flow("Examples/Welcome check")
        if flow is None:
            raise RuntimeError("Synthetic flow was not saved")
        editor = window._flow_editor
        editor._flow = flow
        editor._reset_command_section_states()
        editor._rebuild_cards()
        editor._flow_is_saved = True
        editor._mark_clean()
        editor._update_saved_flow_actions()
        window._show_page("flows")
        _process(500)
        _save(window, "flow-editor.png")

        window._launcher.refresh_run_list()
        window._show_page("runs")
        _process(500)
        _save(window, "run-setups.png")

        window.prepare_for_system_shutdown()
        window.close()
        _process(500)
        database.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
