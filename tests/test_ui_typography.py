from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget

from automadin.engine.session import DeviceSessionService
from automadin.execution.context import FlowContext
from automadin.execution.executor import FlowExecutor
from automadin.execution.handlers.registry import load_command_types
from automadin.execution.runner import FlowRunWorker
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import RunConfig, RunStatus
from automadin.services.database import DatabaseService
from automadin.ui.flow_editor.capture_target_dialog import CaptureTargetDialog
from automadin.ui.flow_editor.editor_view import FlowEditorView
from automadin.ui.flow_editor.palette import CommandPaletteDialog
from automadin.ui.flow_editor.param_editor import PriorityImageItemDelegate
from automadin.ui.flow_runner.runner_view import FlowRunnerView
from automadin.ui.launcher.launcher_view import LauncherView
from automadin.ui.launcher.run_panel import RunPanel
from automadin.ui.theme import get_stylesheet


_REPO_ROOT = Path(__file__).resolve().parents[1]
_TYPOGRAPHY_DIRS = (
    _REPO_ROOT / "automadin" / "ui" / "flow_editor",
    _REPO_ROOT / "automadin" / "ui" / "flow_runner",
    _REPO_ROOT / "automadin" / "ui" / "launcher",
)


def test_target_surfaces_have_no_explicit_user_text_below_ten_points() -> None:
    patterns = (
        re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)pt", re.IGNORECASE),
        re.compile(r"setPointSizeF?\(\s*(\d+(?:\.\d+)?)\s*\)"),
        re.compile(r"QFont\([^\n]*,\s*(\d+(?:\.\d+)?)\s*\)"),
    )
    violations: list[str] = []
    for directory in _TYPOGRAPHY_DIRS:
        for path in directory.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for pattern in patterns:
                for match in pattern.finditer(source):
                    size = float(match.group(1))
                    if size < 10.0:
                        line = source.count("\n", 0, match.start()) + 1
                        violations.append(f"{path.relative_to(_REPO_ROOT)}:{line} = {size}pt")

    assert violations == [], "Explicit fonts below 10pt:\n" + "\n".join(violations)


def test_priority_image_names_do_not_shrink_below_ten_points(
    application: QApplication,
) -> None:
    fitted = PriorityImageItemDelegate._fit_text_font(
        QFont(),
        "Deeply/Nested/Folder/Very Long Image Asset Name",
        QRect(0, 0, 110, 35),
    )

    assert fitted.pointSizeF() >= 10.0


def _assert_visible_single_line_text_has_height(root: QWidget) -> None:
    widgets = [*root.findChildren(QLabel), *root.findChildren(QAbstractButton)]
    for widget in widgets:
        if not widget.isVisibleTo(root):
            continue
        text = widget.text().strip()
        if not text:
            continue
        available = widget.contentsRect().height()
        required = QFontMetrics(widget.font()).height()
        assert available + 1 >= required, (
            f"{widget.__class__.__name__} text {text!r} is vertically clipped: "
            f"{available}px available, {required}px required"
        )


def _build_typography_surface(
    surface_name: str,
    database: DatabaseService,
    session_service: DeviceSessionService,
) -> QWidget:
    if surface_name == "editor":
        editor = FlowEditorView(database, session_service)
        editor._flow = AutomationFlow(
            "Readability smoke",
            [FlowCommand("if_time_elapsed", children=[], else_children=[])],
        )
        editor._rebuild_cards()
        return editor
    if surface_name == "palette":
        return CommandPaletteDialog(load_command_types())
    if surface_name == "runner":
        return FlowRunnerView(database, session_service)
    if surface_name == "launcher":
        return LauncherView(database, session_service)
    if surface_name == "run_panel":
        context = FlowContext(
            DeviceSession("typography-run", AndroidDevice("emulator-5554"))
        )
        worker = FlowRunWorker(
            FlowExecutor(),
            AutomationFlow("Typography run", []),
            context,
            RunConfig(),
        )
        worker._set_status(RunStatus.RUNNING)
        return RunPanel("Typography run", "typography-run", worker)
    if surface_name == "capture_target":
        return CaptureTargetDialog(
            database,
            supported_params={"threshold", "color_match", "color_tolerance"},
        )
    raise AssertionError(f"Unknown typography surface: {surface_name}")


@pytest.mark.parametrize(
    "surface_name",
    ["editor", "palette", "runner", "launcher", "run_panel", "capture_target"],
)
def test_target_surface_constructs_without_text_clipping(
    surface_name: str,
    tmp_path: Path,
    application: QApplication,
) -> None:
    app = application
    app.setStyleSheet(get_stylesheet())
    scale = float(os.environ.get("AUTOMADIN_TEST_DESKTOP_SCALE", "1.0"))
    width = round(1920 / scale)
    height = round(1080 / scale)
    database = DatabaseService(str(tmp_path / f"{surface_name}.db"))
    session_service = DeviceSessionService()

    surface = _build_typography_surface(surface_name, database, session_service)

    try:
        for candidate_width in (min(width, 640), width):
            surface.resize(candidate_width, height)
            surface.show()
            app.processEvents()
            _assert_visible_single_line_text_has_height(surface)
    finally:
        surface.close()
        surface.deleteLater()
        app.processEvents()
        session_service.close_all()
        database.close()
