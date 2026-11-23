from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt, QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QPushButton,
    QScrollArea,
)

from automadin.models.run_config import RunStatus
from automadin.ui.launcher.mini_run_window import (
    MiniRunWindow,
    _visible_window_geometry,
)
from automadin.ui.launcher.run_panel import RunPanelSnapshot
from automadin.ui.theme import COLORS
from automadin.ui.widgets.toggle_switch import ToggleSwitch


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_app_setting(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    def set_app_setting(self, key: str, value: str) -> None:
        self.values[key] = value


class _Launcher(QObject):
    retained_runs_changed = Signal()
    run_config_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.snapshots: tuple[RunPanelSnapshot, ...] = ()
        self.calls: list[tuple[str, object]] = []
        self.interaction_parents: list[object] = []

    def retained_run_snapshots(self) -> tuple[RunPanelSnapshot, ...]:
        return self.snapshots

    def run_setups(self) -> tuple[tuple[int, str, str], ...]:
        return (
            (7, "Root setup", ""),
            (42, "Daily rewards", "Games/Mobile"),
            (43, "Weekly rewards", "Games/Mobile"),
        )

    def start_run_config_by_id(
        self,
        config_id: int,
        *,
        interaction_parent: object | None = None,
    ) -> bool:
        self.calls.append(("start", config_id))
        self.interaction_parents.append(interaction_parent)
        return True

    def pause_or_resume_retained_run(self, run_id: str) -> None:
        self.calls.append(("pause", run_id))

    def stop_retained_run(self, run_id: str) -> None:
        self.calls.append(("stop", run_id))

    def restart_retained_run(
        self,
        run_id: str,
        *,
        interaction_parent: object | None = None,
    ) -> None:
        self.calls.append(("restart", run_id))
        self.interaction_parents.append(interaction_parent)

    def close_retained_run(self, run_id: str) -> None:
        self.calls.append(("close", run_id))


def _snapshot(
    status: RunStatus,
    *,
    pause: bool = False,
    stop: bool = False,
    restart: bool = False,
) -> RunPanelSnapshot:
    return RunPanelSnapshot(
        run_id="run-1",
        name="Daily rewards",
        target_name="Pixel 8",
        status=status,
        can_pause_resume=pause,
        can_stop=stop,
        can_restart=restart,
        close_pending=False,
    )


def test_mini_player_routes_live_controls_and_opens_main_runs_page() -> None:
    app = _app()
    settings = _Settings()
    launcher = _Launcher()
    launcher.snapshots = (_snapshot(RunStatus.RUNNING, pause=True, stop=True),)
    runs_page_opened: list[bool] = []
    window = MiniRunWindow(  # type: ignore[arg-type]
        settings,
        launcher,
        open_runs_page=lambda: runs_page_opened.append(True),
    )
    try:
        assert window.parent() is None
        window.findChild(QPushButton, "miniPause_run-1").click()
        window.findChild(QPushButton, "miniStop_run-1").click()
        window.findChild(QPushButton, "miniOpenAutomadin").click()
        window.findChild(QPushButton, "miniClose_run-1").click()

        assert window.findChild(QPushButton, "miniOpenAutomadin").text() == ""
        assert window.findChild(QPushButton, "miniPause_run-1").text() == "Ⅱ"
        assert window.findChild(QPushButton, "miniStop_run-1").text() == "■"
        status = window.findChild(QLabel, "miniStatus_run-1")
        assert status.text() == "R"
        assert COLORS["green"] in status.styleSheet()
        assert (
            window.findChild(QScrollArea, "miniRunsScroll").horizontalScrollBarPolicy()
            is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        assert window.findChild(QPushButton, "miniOpen_run-1") is None
        assert "Pixel 8" not in {
            label.text() for label in window.findChildren(QLabel)
        }
        assert launcher.calls == [
            ("pause", "run-1"),
            ("stop", "run-1"),
            ("close", "run-1"),
        ]
        assert runs_page_opened == [True]
    finally:
        window.deleteLater()
        app.processEvents()


def test_mini_player_refreshes_terminal_run_and_starts_setup() -> None:
    app = _app()
    settings = _Settings()
    launcher = _Launcher()
    launcher.snapshots = (_snapshot(RunStatus.COMPLETED, restart=True),)
    window = MiniRunWindow(  # type: ignore[arg-type]
        settings,
        launcher,
        open_runs_page=lambda: None,
    )
    try:
        assert window.minimumWidth() == 400
        assert window.minimumHeight() == 130
        picker = window.findChild(QComboBox, "miniSetupPicker")
        assert [picker.itemText(index).strip() for index in range(picker.count())] == [
            "Root setup",
            "▾ Games",
            "▾ Mobile",
            "Daily rewards",
            "Weekly rewards",
        ]
        assert not (picker.model().item(1).flags() & Qt.ItemFlag.ItemIsEnabled)
        assert not (picker.model().item(2).flags() & Qt.ItemFlag.ItemIsEnabled)
        window.findChild(QPushButton, "miniRestart_run-1").click()
        picker.setCurrentIndex(picker.findData(42))
        window.findChild(QPushButton, "miniSetupStart").click()
        assert launcher.calls == [("restart", "run-1"), ("start", 42)]
        assert launcher.interaction_parents == [window, window]

        launcher.snapshots = ()
        launcher.retained_runs_changed.emit()
        app.processEvents()
        assert window.findChild(QPushButton, "miniRestart_run-1") is None
    finally:
        window.deleteLater()
        app.processEvents()


def test_mini_player_persists_on_top_and_collapsed_state() -> None:
    app = _app()
    settings = _Settings()
    launcher = _Launcher()
    window = MiniRunWindow(  # type: ignore[arg-type]
        settings,
        launcher,
        open_runs_page=lambda: None,
    )
    try:
        on_top = window.findChild(ToggleSwitch)
        on_top.setChecked(True)
        setups = window.findChild(QObject, "miniSetupsToggle")
        setups.setChecked(True)

        assert settings.values["mini_run_player_on_top"] == "1"
        assert settings.values["mini_run_player_pinned_expanded"] == "1"
    finally:
        window.deleteLater()
        app.processEvents()


def test_mini_player_clamps_old_narrow_geometry_and_colors_error_status() -> None:
    app = _app()
    settings = _Settings()
    settings.values["mini_run_player_geometry"] = "[10, 10, 280, 220]"
    launcher = _Launcher()
    launcher.snapshots = (_snapshot(RunStatus.ERROR, restart=True),)
    window = MiniRunWindow(  # type: ignore[arg-type]
        settings,
        launcher,
        open_runs_page=lambda: None,
    )
    try:
        assert window.width() >= 400
        status = window.findChild(QLabel, "miniStatus_run-1")
        assert status.text() == "E"
        assert COLORS["red"] in status.styleSheet()
        assert window.findChild(QPushButton, "miniRestart_run-1").text() == "▶"
        assert window.findChild(QPushButton, "miniClose_run-1").text() == "×"
    finally:
        window.deleteLater()
        app.processEvents()


def test_visible_window_geometry_recovers_geometry_from_disconnected_screen() -> None:
    available = [QRect(0, 0, 1920, 1040), QRect(-1280, 0, 1280, 1024)]

    recovered = _visible_window_geometry(
        QRect(2581, 838, 400, 173),
        available,
    )

    assert recovered.size() == QRect(2581, 838, 400, 173).size()
    assert available[0].contains(recovered)


def test_mini_player_restores_offscreen_geometry_to_an_available_screen() -> None:
    app = _app()
    settings = _Settings()
    settings.values["mini_run_player_geometry"] = "[50000, 50000, 400, 173]"
    launcher = _Launcher()
    window = MiniRunWindow(  # type: ignore[arg-type]
        settings,
        launcher,
        open_runs_page=lambda: None,
    )
    try:
        assert any(
            screen.availableGeometry().contains(window.geometry())
            for screen in QGuiApplication.screens()
        )
    finally:
        window.deleteLater()
        app.processEvents()
