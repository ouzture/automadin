"""Compact mirrored controls for retained launcher runs."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QCloseEvent, QGuiApplication, QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from automadin.models.run_config import RunStatus
from automadin.services.database import DatabaseService
from automadin.ui.launcher.run_panel import RunPanelSnapshot
from automadin.ui.theme import COLORS
from automadin.ui.widgets.buttons import configure_button
from automadin.ui.widgets.toggle_switch import ToggleSwitch

_GEOMETRY_SETTING = "mini_run_player_geometry"
_ON_TOP_SETTING = "mini_run_player_on_top"
_SETUPS_EXPANDED_SETTING = "mini_run_player_pinned_expanded"

_STATUS_COLORS: dict[RunStatus, str] = {
    RunStatus.RUNNING: "success",
    RunStatus.PAUSED: "warning",
    RunStatus.PAUSING: "warning",
    RunStatus.STOPPING: "warning",
    RunStatus.REQUIRES_ATTENTION: "warning",
    RunStatus.STOPPED: "orange",
    RunStatus.ERROR: "error",
    RunStatus.COMPLETED: "primary",
    RunStatus.CLEANING_UP: "warning",
    RunStatus.CLEANUP_PENDING: "error",
}

_STATUS_LETTERS: dict[RunStatus, str] = {
    RunStatus.IDLE: "I",
    RunStatus.CONNECTING: "C",
    RunStatus.ACQUIRING_EMULATOR: "A",
    RunStatus.STARTING_EMULATOR: "S",
    RunStatus.WAITING_FOR_ANDROID: "W",
    RunStatus.CHECKING_ANDROID: "C",
    RunStatus.RUNNING: "R",
    RunStatus.PAUSING: "P",
    RunStatus.PAUSED: "P",
    RunStatus.STOPPING: "S",
    RunStatus.REQUIRES_ATTENTION: "A",
    RunStatus.STOPPED: "S",
    RunStatus.ERROR: "E",
    RunStatus.COMPLETED: "C",
    RunStatus.CLEANING_UP: "C",
    RunStatus.CLEANUP_PENDING: "P",
}

_STATUS_TEXT_COLORS: dict[RunStatus, str] = {
    RunStatus.IDLE: "text_tertiary",
    RunStatus.CONNECTING: "blue",
    RunStatus.ACQUIRING_EMULATOR: "cyan",
    RunStatus.STARTING_EMULATOR: "cyan",
    RunStatus.WAITING_FOR_ANDROID: "yellow",
    RunStatus.CHECKING_ANDROID: "yellow",
    RunStatus.RUNNING: "green",
    RunStatus.PAUSING: "yellow",
    RunStatus.PAUSED: "orange",
    RunStatus.STOPPING: "orange",
    RunStatus.REQUIRES_ATTENTION: "yellow",
    RunStatus.STOPPED: "orange",
    RunStatus.ERROR: "red",
    RunStatus.COMPLETED: "purple",
    RunStatus.CLEANING_UP: "yellow",
    RunStatus.CLEANUP_PENDING: "red",
}


def _visible_window_geometry(
    geometry: QRect,
    available_screens: Sequence[QRect],
) -> QRect:
    """Return *geometry* moved onto the best available screen.

    A saved tool-window position can become unreachable after a monitor is
    disconnected or the Windows display layout changes. Preserve the user's
    size and chosen screen when possible, but move a stranded window to the
    primary screen (the first geometry supplied by the caller).
    """
    if not available_screens:
        return QRect(geometry)

    def intersection_area(screen: QRect) -> int:
        intersection = geometry.intersected(screen)
        return max(0, intersection.width()) * max(0, intersection.height())

    target = max(available_screens, key=intersection_area)
    if intersection_area(target) == 0:
        target = available_screens[0]

    adjusted = QRect(geometry)
    if adjusted.width() <= target.width():
        adjusted.moveLeft(
            min(max(adjusted.left(), target.left()), target.right() - adjusted.width() + 1)
        )
    else:
        adjusted.moveLeft(target.left())
    if adjusted.height() <= target.height():
        adjusted.moveTop(
            min(max(adjusted.top(), target.top()), target.bottom() - adjusted.height() + 1)
        )
    else:
        adjusted.moveTop(target.top())
    return adjusted


class _MiniTitleBar(QFrame):
    """Compact draggable caption for the frameless mini-player window."""

    def __init__(self, owner: MiniRunWindow) -> None:
        super().__init__(owner)
        self._owner = owner
        self._drag_offset: QPoint | None = None
        self.setObjectName("miniTitleBar")
        self.setFixedHeight(30)
        self.setStyleSheet(
            f"#miniTitleBar {{ background: {COLORS['bg_elevated']}; "
            f"border-bottom: 1px solid {COLORS['border']}; }}"
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.LeftButton:
            handle = self._owner.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag_offset = (
                event.globalPosition().toPoint() - self._owner.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._owner.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class MiniRunWindow(QWidget):
    """Small always-available controller mirroring the Active Runs cards."""

    def __init__(
        self,
        database: DatabaseService,
        launcher: Any,
        *,
        open_runs_page: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        # Keep this as an independent native tool window. A Qt parent would make
        # Windows minimize it together with the main Automadin window.
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint,
        )
        self._application_owner = parent
        self._database = database
        self._launcher = launcher
        self._open_runs_page = open_runs_page
        self.setObjectName("miniRunWindow")
        self.setWindowTitle("Automadin Runs")
        self.setStyleSheet(
            f"#miniRunWindow {{ background: {COLORS['bg_darkest']}; "
            f"border: 1px solid {COLORS['border']}; }}"
        )
        self.setMinimumSize(400, 130)
        self.resize(440, 260)

        self._build_ui()
        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(14, 14)
        self._size_grip.raise_()
        self._restore_state()
        launcher.retained_runs_changed.connect(self.refresh_runs)
        launcher.run_config_changed.connect(self.refresh_run_setups)
        self.refresh_run_setups()
        self.refresh_runs()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(5)

        title_bar = _MiniTitleBar(self)
        header = QHBoxLayout(title_bar)
        header.setContentsMargins(7, 2, 4, 2)
        header.setSpacing(5)
        title = QLabel("Automadin Runs")
        title.setStyleSheet(f"font-weight: 650; color: {COLORS['text_primary']};")
        header.addWidget(title, 1)
        open_runs = QPushButton()
        open_runs.setObjectName("miniOpenAutomadin")
        open_runs.setToolTip("Show the main Automadin window on the Runs page")
        open_runs.setAccessibleName("Open Automadin Runs page")
        owner_icon = (
            self._application_owner.windowIcon()
            if self._application_owner is not None
            else self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        )
        open_runs.setIcon(owner_icon)
        open_runs.setIconSize(QSize(16, 16))
        configure_button(open_runs, appearance="outline", density="compact")
        open_runs.setFixedSize(26, 24)
        open_runs.clicked.connect(self._open_runs_page)
        header.addWidget(open_runs)
        on_top_label = QLabel("Top")
        on_top_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        header.addWidget(on_top_label)
        self._on_top_switch = ToggleSwitch()
        self._on_top_switch.setAccessibleName("Keep mini player on top")
        self._on_top_switch.setToolTip("Keep this controller above other windows")
        self._on_top_switch.toggled.connect(self._set_always_on_top)
        header.addWidget(self._on_top_switch)
        close_window = QToolButton()
        close_window.setObjectName("miniWindowClose")
        close_window.setText("×")
        close_window.setAccessibleName("Close mini player")
        close_window.setToolTip("Close mini player")
        close_window.setFixedSize(24, 24)
        close_window.clicked.connect(self.close)
        header.addWidget(close_window)
        root.addWidget(title_bar)

        setup_toolbar = QHBoxLayout()
        setup_toolbar.setSpacing(5)
        self._setups_toggle = QToolButton()
        self._setups_toggle.setObjectName("miniSetupsToggle")
        self._setups_toggle.setCheckable(True)
        self._setups_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._setups_toggle.toggled.connect(self._set_setups_expanded)
        setup_toolbar.addWidget(self._setups_toggle, 1)
        root.addLayout(setup_toolbar)

        self._setups_content = QFrame()
        self._setups_content.setObjectName("miniSetupsContent")
        self._setups_content.setStyleSheet(
            f"#miniSetupsContent {{ background: {COLORS['bg_surface']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 6px; }}"
        )
        setups_layout = QHBoxLayout(self._setups_content)
        setups_layout.setContentsMargins(8, 6, 8, 6)
        setups_layout.setSpacing(5)
        self._setups_combo = QComboBox()
        self._setups_combo.setObjectName("miniSetupPicker")
        self._setups_combo.setAccessibleName("Run Setup")
        self._setups_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._setups_combo.setMinimumContentsLength(8)
        setups_layout.addWidget(self._setups_combo, 1)
        self._setup_start = QPushButton("▶ Run")
        self._setup_start.setObjectName("miniSetupStart")
        configure_button(self._setup_start, semantic="success", density="compact")
        self._setup_start.clicked.connect(self._start_selected_setup)
        setups_layout.addWidget(self._setup_start)
        root.addWidget(self._setups_content)

        scroll = QScrollArea()
        scroll.setObjectName("miniRunsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._runs_container = QWidget()
        self._runs_layout = QVBoxLayout(self._runs_container)
        self._runs_layout.setContentsMargins(0, 0, 0, 0)
        self._runs_layout.setSpacing(1)
        self._runs_layout.addStretch()
        scroll.setWidget(self._runs_container)
        root.addWidget(scroll, 1)

    def show_player(self) -> None:
        self._ensure_visible_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()

    def refresh_run_setups(self) -> None:
        setups = self._launcher.run_setups()
        self._setups_toggle.setText(f"Run setups ({len(setups)})")
        selected_id = self._setups_combo.currentData()
        self._setups_combo.clear()
        if not setups:
            self._setups_combo.addItem("No Run Setups", None)
            self._setups_combo.setEnabled(False)
            self._setup_start.setEnabled(False)
            return
        self._setups_combo.setEnabled(True)
        self._setup_start.setEnabled(True)

        shown_folders: set[str] = set()
        first_setup_index = -1
        for config_id, name, group_path in setups:
            parts = [part for part in group_path.split("/") if part]
            for depth, part in enumerate(parts):
                folder_path = "/".join(parts[: depth + 1])
                if folder_path.casefold() in shown_folders:
                    continue
                shown_folders.add(folder_path.casefold())
                indent = "\u00a0\u00a0" * depth
                self._setups_combo.addItem(f"{indent}▾ {part}", None)
                folder_item = self._setups_combo.model().item(
                    self._setups_combo.count() - 1
                )
                if folder_item is not None:
                    folder_item.setEnabled(False)
            indent = "\u00a0\u00a0" * (len(parts) + (1 if parts else 0))
            self._setups_combo.addItem(f"{indent}{name}", config_id)
            if first_setup_index < 0:
                first_setup_index = self._setups_combo.count() - 1

        restored_index = self._setups_combo.findData(selected_id)
        self._setups_combo.setCurrentIndex(
            restored_index if restored_index >= 0 else first_setup_index
        )

    def refresh_runs(self) -> None:
        while self._runs_layout.count() > 1:
            item = self._runs_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        snapshots = self._launcher.retained_run_snapshots()
        if not snapshots:
            empty = QLabel("No retained runs")
            empty.setObjectName("miniRunsEmpty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {COLORS['text_tertiary']}; padding: 12px;")
            self._runs_layout.insertWidget(0, empty)
            return
        for snapshot in snapshots:
            self._runs_layout.insertWidget(
                max(0, self._runs_layout.count() - 1),
                self._run_card(snapshot),
            )

    def _run_card(self, snapshot: RunPanelSnapshot) -> QWidget:
        card = QFrame()
        card.setObjectName("miniRunCard")
        card.setProperty("runId", snapshot.run_id)
        card.setStyleSheet(
            f"QFrame#miniRunCard {{ background: {COLORS['bg_surface']}; "
            f"border-bottom: 1px solid {COLORS['border_light']}; }}"
        )
        layout = QHBoxLayout(card)
        layout.setContentsMargins(0, 2, 4, 2)
        layout.setSpacing(5)

        token = _STATUS_COLORS.get(snapshot.status, "text_secondary")
        rail = QFrame()
        rail.setObjectName("miniRunStatusRail")
        rail.setFixedWidth(4)
        rail.setStyleSheet(f"background: {COLORS[token]}; border: none;")
        rail.setToolTip(snapshot.status.value.title())
        layout.addWidget(rail)

        name = QLabel(snapshot.name)
        name.setToolTip(snapshot.name)
        name.setMinimumWidth(0)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        name.setStyleSheet(f"font-weight: 650; color: {COLORS['text_primary']};")
        layout.addWidget(name, 1)
        status = QLabel(_STATUS_LETTERS[snapshot.status])
        status.setObjectName(f"miniStatus_{snapshot.run_id}")
        status.setToolTip(snapshot.status.value.title())
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setFixedWidth(14)
        status_color = _STATUS_TEXT_COLORS[snapshot.status]
        status.setStyleSheet(
            f"color: {COLORS[status_color]}; font-size: 10pt; font-weight: 750;"
        )
        layout.addWidget(status)

        if snapshot.can_pause_resume:
            is_paused = snapshot.status is RunStatus.PAUSED
            pause = self._run_icon_button(
                object_name=f"miniPause_{snapshot.run_id}",
                glyph="▶" if is_paused else "Ⅱ",
                label="Resume" if is_paused else "Pause",
            )
            pause.clicked.connect(
                lambda _checked=False, identity=snapshot.run_id: self._launcher.pause_or_resume_retained_run(
                    identity
                )
            )
            layout.addWidget(pause)
        if snapshot.can_stop:
            stop = self._run_icon_button(
                object_name=f"miniStop_{snapshot.run_id}",
                glyph="■",
                label="Stop",
                danger=True,
            )
            stop.clicked.connect(
                lambda _checked=False, identity=snapshot.run_id: self._launcher.stop_retained_run(
                    identity
                )
            )
            layout.addWidget(stop)
        if snapshot.can_restart:
            restart = self._run_icon_button(
                object_name=f"miniRestart_{snapshot.run_id}",
                glyph="▶",
                label="Run again",
            )
            restart.clicked.connect(
                lambda _checked=False, identity=snapshot.run_id: self._launcher.restart_retained_run(
                    identity,
                    interaction_parent=self,
                )
            )
            layout.addWidget(restart)
        close = self._run_icon_button(
            object_name=f"miniClose_{snapshot.run_id}",
            glyph="×",
            label=(
                "Stopping before close"
                if snapshot.close_pending
                else f"Close run: {snapshot.name}"
            ),
            danger=True,
        )
        close.setEnabled(not snapshot.close_pending)
        close.clicked.connect(
            lambda _checked=False, identity=snapshot.run_id: self._launcher.close_retained_run(
                identity
            )
        )
        layout.addWidget(close)
        return card

    def _run_icon_button(
        self,
        *,
        object_name: str,
        glyph: str,
        label: str,
        danger: bool = False,
    ) -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        button.setText(glyph)
        button.setToolTip(label)
        button.setAccessibleName(label)
        configure_button(
            button,
            semantic="danger" if danger else None,
            appearance="outline",
            density="compact",
        )
        glyph_color = COLORS["red"] if danger else COLORS["text_primary"]
        button.setStyleSheet(
            f"QPushButton {{ color: {glyph_color}; font-size: 10pt; "
            "font-weight: 750; padding: 0; }}"
        )
        button.setFixedSize(28, 26)
        return button

    def _start_selected_setup(self) -> None:
        config_id = self._setups_combo.currentData()
        if not isinstance(config_id, int):
            return
        self._setup_start.setEnabled(False)
        try:
            self._launcher.start_run_config_by_id(
                config_id,
                interaction_parent=self,
            )
        finally:
            self._setup_start.setEnabled(True)

    def _set_setups_expanded(self, expanded: bool) -> None:
        self._setups_content.setVisible(expanded)
        self._setups_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._database.set_app_setting(
            _SETUPS_EXPANDED_SETTING, "1" if expanded else "0"
        )

    def _set_always_on_top(self, enabled: bool) -> None:
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        self._database.set_app_setting(_ON_TOP_SETTING, "1" if enabled else "0")
        if was_visible:
            self.show()
            self.raise_()

    def _restore_state(self) -> None:
        geometry = self._database.get_app_setting(_GEOMETRY_SETTING, "") or ""
        try:
            values = json.loads(geometry)
            if (
                isinstance(values, list)
                and len(values) == 4
                and all(isinstance(value, int) for value in values)
            ):
                self.setGeometry(*values)
        except (TypeError, ValueError):
            pass
        self._ensure_visible_on_screen()
        expanded = (
            self._database.get_app_setting(_SETUPS_EXPANDED_SETTING, "0") == "1"
        )
        self._setups_toggle.setChecked(expanded)
        self._set_setups_expanded(expanded)
        on_top = self._database.get_app_setting(_ON_TOP_SETTING, "0") == "1"
        self._on_top_switch.setChecked(on_top)
        self._set_always_on_top(on_top)

    def _ensure_visible_on_screen(self) -> None:
        primary = QGuiApplication.primaryScreen()
        screens = QGuiApplication.screens()
        ordered_screens = (
            ([primary] if primary is not None else [])
            + [screen for screen in screens if screen is not primary]
        )
        available = [screen.availableGeometry() for screen in ordered_screens]
        geometry = _visible_window_geometry(self.geometry(), available)
        if geometry != self.geometry():
            self.setGeometry(geometry)

    def persist_state(self) -> None:
        geometry = self.geometry()
        self._database.set_app_setting(
            _GEOMETRY_SETTING,
            json.dumps([geometry.x(), geometry.y(), geometry.width(), geometry.height()]),
        )

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_size_grip"):
            self._size_grip.move(
                self.width() - self._size_grip.width() - 1,
                self.height() - self._size_grip.height() - 1,
            )
            self._size_grip.raise_()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.persist_state()
        self.hide()
        event.ignore()
