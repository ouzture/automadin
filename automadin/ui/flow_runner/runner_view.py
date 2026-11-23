"""Flow Runner view — select a flow, configure run settings, and execute with live logs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QComboBox,
    QPushButton,
    QSpinBox,  # noqa: F401 — unused but kept for reference
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.session import DeviceSessionService
from automadin.execution.build_executor import build_executor
from automadin.execution.context import FlowContext
from automadin.execution.attention import AttentionRequest, AttentionResolution
from automadin.execution.executor import FlowExecutor
from automadin.execution.flow_values import FlowRuntimeValueError
from automadin.execution.debug_context import build_debug_context
from automadin.execution.runner import BrowserAwareRunWorker, FlowRunWorker, RunnerService
from automadin.execution.log_granularity import LogGranularity
from automadin.models.flow import (
    AutomationFlow,
    FlowCommand,
)
from automadin.models.resource_path import resource_display_label, resource_sort_key
from automadin.models.run_config import RunConfig, RunMode
from automadin.services.database import DatabaseService
from automadin.services.device_operation_leases import DeviceOperationConflictError
from automadin.services.flow_preflight import (
    PreflightMode,
    preflight_flow,
    target_kind_for_device,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.flow_editor.param_editor import NoScrollComboBox, NoScrollSpinBox
from automadin.ui.attention_dialog import prompt_for_attention
from automadin.ui.execution_error_details import ExecutionErrorBar
from automadin.ui.log_formatting import append_semantic_log
from automadin.ui.preflight_dialog import confirm_execution_preflight
from automadin.ui.theme import COLORS
from automadin.ui.widgets.buttons import configure_button, set_button_semantic
from automadin.ui.widgets.combo_boxes import configure_combo_box
from automadin.ui.widgets.toggle_switch import ToggleSwitch
from automadin.ui.widgets.interactive_screenshot import InteractiveScreenshotWidget

logger = logging.getLogger(__name__)

_LOG_ACTION_BUTTON_WIDTH = 64

# Commands that produce a detected image location (show match-circle overlay)
_DETECTION_CMDS = frozenset({
    "find_image", "win_find_image", "win_click_image", "win_if_image",
    "click_image", "if_image", "if_found", "click_if_found", "click_best_target",
})
# Commands that click at a fixed coordinate (show crosshair overlay)
_CLICK_CMDS = frozenset({
    "win_click", "win_right_click", "win_double_click",
    "click_position", "adb_click", "adb_tap",
})


def _build_executor(db=None) -> FlowExecutor:
    """Create a FlowExecutor with all handlers registered (delegates to shared module)."""
    return build_executor(db=db)


def _build_debug_context(flow: AutomationFlow, session: object) -> FlowContext:
    """Backward-compatible alias for the shared interactive context builder."""
    return build_debug_context(flow, session)


class FlowRunnerView(QWidget):
    """Run automation flows with live log output and run controls."""

    def __init__(
        self,
        db: DatabaseService,
        session_service: DeviceSessionService,
        device_selector=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._session_service = session_service
        self._device_selector = device_selector  # shared from MainWindow
        self._runner_service = RunnerService(default_source="debugger")
        self._executor = _build_executor(db=db)

        self._current_run_id: str | None = None
        self._current_worker: FlowRunWorker | None = None
        self._run_paused: bool = False  # tracks whether user has paused the run
        self._cleanup_pending = False

        # Flow tree state
        self._step_item_map: dict[str, QTreeWidgetItem] = {}
        self._step_cmd_map: dict[str, FlowCommand] = {}
        self._last_highlighted: QTreeWidgetItem | None = None

        # Last screenshot step tracking
        self._last_screenshot_step_id: str = ""

        self._build_ui()

    @property
    def runner_service(self) -> RunnerService:
        return self._runner_service

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Outer splitter: left controls | right content
        outer_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel: flow selection + run config + controls
        left_panel = self._build_left_panel()
        outer_splitter.addWidget(left_panel)

        # Right: vertical splitter holding [tree+screenshot | log]
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 8, 8, 8)
        right_layout.setSpacing(4)

        # Top row: flow tree + screenshot (horizontal splitter)
        top_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Flow command tree panel
        tree_panel = self._build_flow_tree_panel()
        top_splitter.addWidget(tree_panel)

        # Screenshot preview panel
        screenshot_panel = self._build_screenshot_panel()
        top_splitter.addWidget(screenshot_panel)

        top_splitter.setSizes([400, 600])

        # Vertical splitter: top row | log
        vert_splitter = QSplitter(Qt.Orientation.Vertical)
        vert_splitter.addWidget(top_splitter)

        # Log panel
        log_panel = self._build_log_panel()
        vert_splitter.addWidget(log_panel)

        vert_splitter.setSizes([400, 500])
        right_layout.addWidget(vert_splitter, 1)

        outer_splitter.addWidget(right_widget)
        outer_splitter.setSizes([320, 980])

        main_layout.addWidget(outer_splitter, 1)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # ── Flow selection ──
        flow_group = QGroupBox("Flow")
        flow_layout = QVBoxLayout(flow_group)

        self._flow_combo = NoScrollComboBox()
        self._refresh_flow_list()
        flow_layout.addWidget(self._flow_combo)

        refresh_btn = QPushButton("Refresh List")
        refresh_btn.clicked.connect(self._refresh_flow_list)
        flow_layout.addWidget(refresh_btn)

        layout.addWidget(flow_group)

        # ── Run mode ──
        mode_group = QGroupBox("Run Mode")
        mode_layout = QVBoxLayout(mode_group)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._mode_combo = NoScrollComboBox()
        self._mode_combo.addItem("Run Once", RunMode.ONCE)
        self._mode_combo.addItem("Run N Times", RunMode.COUNT)
        self._mode_combo.addItem("Run for Duration", RunMode.DURATION)
        self._mode_combo.addItem("Run Continuously", RunMode.CONTINUOUS)
        self._mode_combo.addItem("Run Until Error", RunMode.UNTIL_ERROR)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo)
        mode_layout.addLayout(mode_row)

        # Count spinner (for COUNT mode)
        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Count:"))
        self._count_spin = NoScrollSpinBox()
        self._count_spin.setMinimum(1)
        self._count_spin.setMaximum(99999)
        self._count_spin.setValue(1)
        count_row.addWidget(self._count_spin)
        self._count_widget = QWidget()
        self._count_widget.setLayout(count_row)
        self._count_widget.setVisible(False)
        mode_layout.addWidget(self._count_widget)

        # Duration spinner (for DURATION mode)
        dur_row = QHBoxLayout()
        dur_row.addWidget(QLabel("Duration (s):"))
        self._duration_spin = NoScrollSpinBox()
        self._duration_spin.setMinimum(1)
        self._duration_spin.setMaximum(86400)
        self._duration_spin.setValue(60)
        dur_row.addWidget(self._duration_spin)
        self._duration_widget = QWidget()
        self._duration_widget.setLayout(dur_row)
        self._duration_widget.setVisible(False)
        mode_layout.addWidget(self._duration_widget)

        # Delay between runs
        delay_row = QHBoxLayout()
        delay_row.addWidget(QLabel("Delay (ms):"))
        self._delay_spin = NoScrollSpinBox()
        self._delay_spin.setMinimum(0)
        self._delay_spin.setMaximum(60000)
        self._delay_spin.setValue(0)
        self._delay_spin.setSingleStep(100)
        delay_row.addWidget(self._delay_spin)
        mode_layout.addLayout(delay_row)

        layout.addWidget(mode_group)

        # ── Controls ──
        controls_group = QGroupBox("Controls")
        controls_layout = QVBoxLayout(controls_group)

        # Run / Pause / Stop buttons
        btn_row = QHBoxLayout()

        self._run_btn = QPushButton("Run")
        configure_button(self._run_btn, semantic="success")
        self._run_btn.clicked.connect(self._on_run_clicked)
        btn_row.addWidget(self._run_btn)

        self._pause_btn = QPushButton("Pause")
        configure_button(self._pause_btn, semantic="warning")
        self._pause_btn.clicked.connect(self._pause_run)
        self._pause_btn.setEnabled(False)
        btn_row.addWidget(self._pause_btn)

        self._stop_btn = QPushButton("Stop")
        configure_button(self._stop_btn, semantic="danger")
        self._stop_btn.clicked.connect(self._stop_run)
        self._stop_btn.setEnabled(False)
        btn_row.addWidget(self._stop_btn)

        controls_layout.addLayout(btn_row)

        # Next Step button (for step-through debugging)
        self._next_step_btn = QPushButton("Next Step")
        configure_button(self._next_step_btn, semantic="primary")
        self._next_step_btn.setEnabled(True)  # always enabled; starts run if none active
        self._next_step_btn.setToolTip("Step-through: execute the next command, then pause (also starts a flow if none is running)")
        self._next_step_btn.clicked.connect(self._step_once)
        controls_layout.addWidget(self._next_step_btn)

        # Status display
        self._status_label = QLabel("Status: Idle")
        self._status_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; padding: 4px;"
        )
        controls_layout.addWidget(self._status_label)

        # Elapsed time
        self._elapsed_label = QLabel("Elapsed: --")
        self._elapsed_label.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; padding: 2px;"
        )
        controls_layout.addWidget(self._elapsed_label)

        layout.addWidget(controls_group)

        layout.addStretch()
        return panel

    def _build_flow_tree_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(2)

        header = QHBoxLayout()
        header.addWidget(QLabel("Flow Preview"))
        layout.addLayout(header)

        self._flow_tree = QTreeWidget()
        self._flow_tree.setHeaderLabels(["Command", "Step ID"])
        self._flow_tree.setColumnWidth(0, 220)
        self._flow_tree.setMinimumWidth(200)
        self._flow_tree.setStyleSheet(
            "QTreeWidget { font-size: 10pt; }"
        )
        layout.addWidget(self._flow_tree, 1)

        return panel

    def _build_screenshot_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 0, 0, 0)
        layout.setSpacing(2)

        header = QHBoxLayout()
        header.addWidget(QLabel("Screenshot Preview"))
        header.addStretch()
        layout.addLayout(header)

        self._screenshot_widget = InteractiveScreenshotWidget(enable_crop=False)
        self._screenshot_widget.setMinimumSize(200, 150)
        self._screenshot_widget.setText("No screenshot yet")
        layout.addWidget(self._screenshot_widget, 1)

        # Info bar below the screenshot
        self._screenshot_info_label = QLabel("")
        self._screenshot_info_label.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; padding: 2px;"
        )
        layout.addWidget(self._screenshot_info_label)

        return panel

    def _build_log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)

        # Header with log-follow and clear controls
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(QLabel("Execution Log"))
        header.addStretch()
        tail_label = QLabel("Tail")
        tail_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt;"
        )
        header.addWidget(tail_label)
        self._tail_log_btn = ToggleSwitch()
        self._tail_log_btn.setChecked(True)
        self._tail_log_btn.setAccessibleName("Tail execution log")
        self._tail_log_btn.setToolTip(
            "Follow new log lines. Turn off to keep your current scroll position."
        )
        header.addWidget(self._tail_log_btn)
        granularity_label = QLabel("Level")
        granularity_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt;"
        )
        header.addWidget(granularity_label)
        self._log_granularity_combo = QComboBox()
        self._log_granularity_combo.addItem("Normal", LogGranularity.NORMAL)
        self._log_granularity_combo.addItem("Detailed", LogGranularity.DETAILED)
        self._log_granularity_combo.addItem("Trace", LogGranularity.TRACE)
        configure_combo_box(
            self._log_granularity_combo,
            density="compact",
            minimum_contents_length=len("Detailed"),
        )
        self._log_granularity_combo.setAccessibleName("Execution log detail level")
        self._log_granularity_combo.setToolTip(
            "Normal shows outcomes; Detailed adds summaries and timing; "
            "Trace adds every poll and iteration. Changes apply to new lines."
        )
        header.addWidget(self._log_granularity_combo)
        clear_btn = QPushButton("Clear")
        configure_button(
            clear_btn,
            appearance="outline",
            density="compact",
            minimum_width=_LOG_ACTION_BUTTON_WIDTH,
        )
        clear_btn.clicked.connect(self._clear_log)
        self._clear_log_btn = clear_btn
        header.addWidget(clear_btn)
        layout.addLayout(header)

        # Log text area
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setStyleSheet(
            f"QTextEdit {{ "
            f"  background-color: {COLORS['bg_darkest']}; "
            f"  color: {COLORS['text_primary']}; "
            f"  font-family: 'Cascadia Code', 'Consolas', 'Courier New', monospace; "
            f"  font-size: 11pt; "
            f"  border: 1px solid {COLORS['border']}; "
            f"  border-radius: 6px; "
            f"  padding: 8px; "
            f"}}"
        )
        layout.addWidget(self._log_view, 1)

        self._error_bar = ExecutionErrorBar()
        layout.addWidget(self._error_bar)

        # Step info bar
        self._step_label = QLabel("")
        self._step_label.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; padding: 2px 4px;"
        )
        layout.addWidget(self._step_label)

        return panel

    # ── Flow tree ─────────────────────────────────────────────────────

    def _build_flow_tree(self, flow: AutomationFlow) -> None:
        """Populate the flow preview tree from a flow's command list."""
        self._step_item_map = {}
        self._step_cmd_map = {}
        self._last_highlighted = None
        self._flow_tree.clear()
        self._populate_tree(self._flow_tree.invisibleRootItem(), flow.commands)
        self._flow_tree.expandAll()
        self._index_commands(flow.commands)

    def _populate_tree(
        self, parent: QTreeWidgetItem, commands: list[FlowCommand]
    ) -> None:
        for cmd in commands:
            label = cmd.params.get("comment") or cmd.id
            if not cmd.enabled:
                label = f"[disabled] {label}"
            item = QTreeWidgetItem(parent, [label, cmd.step_id[:8]])
            if not cmd.enabled:
                item.setForeground(0, QBrush(QColor(COLORS["text_tertiary"])))
                item.setForeground(1, QBrush(QColor(COLORS["text_tertiary"])))
            self._step_item_map[cmd.step_id] = item
            self._populate_tree(item, cmd.children)
            if cmd.else_children:
                else_item = QTreeWidgetItem(item, ["[else]", ""])
                self._populate_tree(else_item, cmd.else_children)

    def _index_commands(self, commands: list[FlowCommand]) -> None:
        """Build flat step_id → FlowCommand map for overlay param lookup."""
        for cmd in commands:
            self._step_cmd_map[cmd.step_id] = cmd
            self._index_commands(cmd.children)
            self._index_commands(cmd.else_children)

    def _highlight_step(self, step_id: str) -> None:
        if step_id not in self._step_item_map:
            return
        item = self._step_item_map[step_id]
        if self._last_highlighted is not None:
            self._last_highlighted.setBackground(0, QBrush())
            self._last_highlighted.setBackground(1, QBrush())
        highlight = QBrush(QColor(COLORS["accent"] + "55"))
        item.setBackground(0, highlight)
        item.setBackground(1, highlight)
        self._flow_tree.scrollToItem(item)
        self._last_highlighted = item

    def _clear_highlight(self) -> None:
        if self._last_highlighted is not None:
            self._last_highlighted.setBackground(0, QBrush())
            self._last_highlighted.setBackground(1, QBrush())
            self._last_highlighted = None

    # ── Flow list ─────────────────────────────────────────────────────

    def _refresh_flow_list(self) -> None:
        current = self._flow_combo.currentData()
        self._flow_combo.clear()
        flows = self._db.list_flows()
        if not flows:
            self._flow_combo.addItem("(no saved flows)")
        else:
            folder_index = self._db.workspace_folder_index()
            for name in sorted(flows, key=resource_sort_key):
                display_path = folder_index.display_path(name)
                self._flow_combo.addItem(
                    resource_display_label(display_path),
                    name,
                )
                index = self._flow_combo.count() - 1
                self._flow_combo.setItemData(
                    index,
                    display_path,
                    Qt.ItemDataRole.ToolTipRole,
                )
            if current:
                index = self._flow_combo.findData(current)
                if index >= 0:
                    self._flow_combo.setCurrentIndex(index)

    def refresh_flow_list(self) -> None:
        """Public slot used after a flow is renamed, imported, or deleted."""
        self._refresh_flow_list()

    # ── Mode toggle ───────────────────────────────────────────────────

    def _on_mode_changed(self, _index: int) -> None:
        mode: RunMode = self._mode_combo.currentData()
        self._count_widget.setVisible(mode == RunMode.COUNT)
        self._duration_widget.setVisible(mode == RunMode.DURATION)

    # ── Run controls ──────────────────────────────────────────────────

    def _start_run(self, step_through: bool = False) -> None:
        # Validate flow selection
        flow_name = self._flow_combo.currentData()
        if not flow_name:
            QMessageBox.warning(self, "No Flow", "Please save a flow first, then select it.")
            return

        # Validate device session
        session = self._device_selector.get_selected_session()
        if session is None:
            QMessageBox.warning(
                self, "No Device", "Please connect a device first."
            )
            return

        # Load flow
        flow = self._db.load_flow(flow_name)
        if flow is None:
            QMessageBox.critical(
                self, "Error", f"Could not load flow '{flow_name}'."
            )
            return

        if not flow.commands:
            QMessageBox.warning(self, "Empty Flow", "The selected flow has no commands.")
            return

        report = preflight_flow(
            flow,
            db=self._db,
            root_name=flow_name,
            target=target_kind_for_device(session.device),
            mode=PreflightMode.EXECUTION,
        )
        if not confirm_execution_preflight(self, report):
            return

        # Build flow tree
        self._build_flow_tree(flow)

        # Build run config
        mode: RunMode = self._mode_combo.currentData()
        config = RunConfig(
            mode=mode,
            count=self._count_spin.value(),
            duration_seconds=self._duration_spin.value(),
            delay_between_runs_ms=self._delay_spin.value(),
        )

        # Build context.  Version-1 flows require an isolated invocation
        # value state, just as they do when launched through a saved run.
        try:
            ctx = _build_debug_context(flow, session)
        except FlowRuntimeValueError as exc:
            QMessageBox.warning(
                self,
                "Flow Inputs Required",
                "Flow Debugger can use declared Input defaults only. "
                "Set defaults for the missing Inputs, or run it from a saved "
                f"launch configuration with input values.\n\n{exc}",
            )
            return
        if step_through:
            ctx.enable_step_through()

        # Prepare the worker.  Connect its signals before start so a fast
        # ownership conflict or startup failure cannot disappear.
        run_id = uuid.uuid4().hex[:12]

        # BrowserAwareRunWorker submits work to the already-connected
        # Playwright owner thread.  Other device types execute on this worker.
        from automadin.models.device import BrowserDevice

        try:
            if isinstance(session.device, BrowserDevice):
                worker = BrowserAwareRunWorker(
                    self._executor, flow, ctx, config
                )
            else:
                worker = FlowRunWorker(self._executor, flow, ctx, config)
            if step_through:
                # Keep the worker state and execution gate aligned so the first
                # Next Step request can actually resume a pre-paused run.
                worker.request_pause()
            self._runner_service.register_run(run_id, worker)
            worker.reserve_device_operation()
        except DeviceOperationConflictError as exc:
            logger.info("Flow debugger device conflict: %s", exc)
            self._runner_service.remove_run(run_id)
            QMessageBox.information(
                self,
                "Device Already in Use",
                f"This device is already being used by {exc.current.label}. "
                "Stop that operation and try again.",
            )
            return
        except BaseException:
            if "worker" in locals():
                registered = self._runner_service.get_worker(run_id) is worker
                if registered:
                    self._runner_service.remove_run(run_id)
                else:
                    worker.release_reserved_device_operation()
            logger.exception("Could not prepare flow runner worker")
            QMessageBox.critical(
                self,
                "Run Could Not Start",
                "The flow worker could not be prepared. Review Diagnostics for "
                "details, then try again.",
            )
            return

        self._error_bar.clear_report()
        self._current_run_id = run_id
        self._current_worker = worker
        worker.enable_attention()

        # Connect signals
        worker.log_message.connect(self._on_log)
        worker.detailed_log_message.connect(self._on_detailed_log)
        worker.trace_log_message.connect(self._on_trace_log)
        worker.step_started.connect(self._on_step_started)
        worker.step_ended.connect(self._on_step_ended)
        worker.status_changed.connect(self._on_status_changed)
        worker.error_occurred.connect(self._on_error)
        worker.error_details_available.connect(self._error_bar.show_report)
        worker.completed.connect(self._on_completed)
        worker.finished.connect(self._on_worker_finished)
        worker.screenshot_updated.connect(self._on_screenshot_updated)
        worker.screenshot_pre_step.connect(self._on_screenshot_pre_step)
        worker.screenshot_unavailable.connect(self._on_screenshot_unavailable)
        worker.attention_required.connect(self._on_attention_required)

        # Update UI state
        self._run_btn.setEnabled(False)
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        self._flow_combo.setEnabled(False)
        self._mode_combo.setEnabled(False)

        if step_through:
            # Start pre-paused — show as paused immediately
            self._run_paused = True
            self._pause_btn.setText("Resume")
            set_button_semantic(self._pause_btn, "success")
            self._status_label.setText("Status: Paused")
            self._next_step_btn.setEnabled(True)
        else:
            self._run_paused = False
            self._pause_btn.setText("Pause")
            set_button_semantic(self._pause_btn, "warning")
            self._next_step_btn.setEnabled(False)

        self._log(f"Started flow '{flow_name}' (run: {run_id})", "info")
        self._start_elapsed_timer()
        try:
            worker.start()
        except BaseException:
            logger.exception("Could not start flow runner worker")
            if qthread_start_may_have_succeeded(worker):
                # Startup is ambiguous once Qt has launched the native thread.
                # Keep the worker registered and the device reserved until its
                # exact finished signal performs the normal cleanup.
                self._stop_run()
                QMessageBox.critical(
                    self,
                    "Run Startup Problem",
                    "The flow worker reported a startup problem after beginning. "
                    "Automadin is stopping it safely; review Diagnostics before "
                    "trying again.",
                )
                return
            if not self._detach_failed_start(run_id):
                # Keep the failed startup visible and owned by RunnerService.
                QMessageBox.critical(
                    self,
                    "Run Cleanup Pending",
                    "The flow worker did not start, but its device cleanup is "
                    "still pending. Automadin kept this run visible so cleanup "
                    "can be retried safely.",
                )
                return
            QMessageBox.critical(
                self,
                "Run Could Not Start",
                "The flow worker could not be started. Review Diagnostics for "
                "details, then try again.",
            )

    def _detach_failed_start(self, run_id: str) -> bool:
        """Detach a definitely-unstarted run only after cleanup is confirmed."""
        if not self._runner_service.remove_run(run_id):
            if self._current_run_id == run_id:
                self._show_cleanup_pending()
            return False
        if self._current_run_id == run_id:
            self._current_run_id = None
            self._current_worker = None
            self._set_idle_controls()
        return True

    def _on_run_clicked(self, _checked: bool = False) -> None:
        if self._cleanup_pending:
            self._retry_cleanup()
            return
        self._start_run()

    def _retry_cleanup(self) -> bool:
        """Retry non-blocking removal of the exact worker still shown by the UI."""
        run_id = self._current_run_id
        worker = self._current_worker
        if run_id is None or worker is None:
            return False
        if self._runner_service.get_worker(run_id) is not worker:
            logger.error(
                "Refusing flow debugger cleanup because run ownership changed: %s",
                run_id,
            )
            self._show_cleanup_pending()
            return False
        try:
            removed = self._runner_service.remove_run(run_id)
        except BaseException:
            logger.exception("Could not clean up finished flow debugger run")
            removed = False
        if not removed:
            self._show_cleanup_pending()
            return False
        if self._current_run_id == run_id and self._current_worker is worker:
            self._current_run_id = None
            self._current_worker = None
            self._set_idle_controls()
        return True

    def _pause_run(self) -> None:
        if self._current_run_id is None:
            return
        worker = self._runner_service.get_worker(self._current_run_id)
        if worker is None:
            return

        if not self._run_paused:
            self._runner_service.pause_run(self._current_run_id)
            self._run_paused = True
            self._pause_btn.setText("Resume")
            set_button_semantic(self._pause_btn, "success")
            self._status_label.setText("Status: Paused")
            self._next_step_btn.setEnabled(True)
            self._log("Paused", "warn")
        else:
            # When resuming at full speed, disable step_through
            if worker.ctx._step_through:
                worker.ctx.disable_step_through()
            self._runner_service.resume_run(self._current_run_id)
            self._run_paused = False
            self._pause_btn.setText("Pause")
            set_button_semantic(self._pause_btn, "warning")
            self._status_label.setText("Status: Running")
            self._next_step_btn.setEnabled(False)
            self._log("Resumed", "info")

    def _stop_run(self) -> None:
        if self._current_run_id is None:
            return
        self._runner_service.stop_run(self._current_run_id, wait_ms=0)
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setText("Stopping…")
        self._next_step_btn.setEnabled(False)
        self._status_label.setText("Status: Stopping…")
        self._status_label.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 10pt; "
            "font-weight: bold; padding: 4px;"
        )
        self._log("Stop requested", "warn")

    def _step_once(self) -> None:
        """Enable step-through and resume for exactly one command.
        If no run is active, starts the selected flow in step-through mode.
        """
        if self._current_run_id is None:
            self._start_run(step_through=True)
            return
        worker = self._runner_service.get_worker(self._current_run_id)
        if worker is None:
            return
        # enable_step_through sets the flag; resume_run unblocks wait_if_paused()
        worker.ctx.enable_step_through()
        self._runner_service.resume_run(self._current_run_id)
        # UI stays in paused state; executor re-pauses after each command automatically

    def _set_idle_controls(self) -> None:
        """Show idle controls after worker ownership has been released."""
        self._cleanup_pending = False
        self._run_btn.setText("Run")
        self._run_btn.setEnabled(True)
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setText("Stop")
        self._next_step_btn.setEnabled(True)  # keep enabled so Next Step can start next run
        self._flow_combo.setEnabled(True)
        self._mode_combo.setEnabled(True)
        self._run_paused = False
        self._pause_btn.setText("Pause")
        set_button_semantic(self._pause_btn, "warning")
        self._stop_elapsed_timer()
        self._clear_highlight()

    def _show_cleanup_pending(self) -> None:
        """Keep exact worker ownership visible until a removal retry succeeds."""
        self._cleanup_pending = True
        self._run_btn.setText("Retry Cleanup")
        self._run_btn.setEnabled(True)
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setText("Stop")
        self._next_step_btn.setEnabled(False)
        self._flow_combo.setEnabled(False)
        self._mode_combo.setEnabled(False)
        self._run_paused = False
        self._pause_btn.setText("Pause")
        set_button_semantic(self._pause_btn, "warning")
        self._stop_elapsed_timer()
        self._clear_highlight()
        self._status_label.setText("Status: Cleanup Pending")
        self._status_label.setStyleSheet(
            f"color: {COLORS['yellow']}; font-size: 10pt; "
            "font-weight: bold; padding: 4px;"
        )

    def _reset_controls(self) -> bool:
        """Release the exact finished run before returning the UI to idle."""
        if self._current_run_id is not None and self._current_worker is not None:
            return self._retry_cleanup()
        self._current_run_id = None
        self._current_worker = None
        self._set_idle_controls()
        return True

    # ── Signal handlers ───────────────────────────────────────────────

    def _on_log(self, message: str) -> None:
        self._log(message, "info")

    def _log_granularity(self) -> LogGranularity:
        return LogGranularity(self._log_granularity_combo.currentData())

    def _on_detailed_log(self, message: str) -> None:
        if self._log_granularity() >= LogGranularity.DETAILED:
            self._log(message, "detailed")

    def _on_trace_log(self, message: str) -> None:
        if self._log_granularity() >= LogGranularity.TRACE:
            self._log(message, "trace")

    def _on_step_started(self, command_id: str, step_id: str) -> None:
        self._step_label.setText(f"▶ {command_id}  [{step_id[:8]}]")
        self._last_screenshot_step_id = step_id
        self._highlight_step(step_id)
        self._log(f"→ {command_id}", "step")

    def _on_step_ended(self, command_id: str, step_id: str) -> None:
        self._step_label.setText("")

    def _on_status_changed(self, status_value: str) -> None:
        status_colors = {
            "running": COLORS["green"],
            "paused": COLORS["yellow"],
            "stopped": COLORS["text_secondary"],
            "error": COLORS["red"],
            "completed": COLORS["blue"],
        }
        color = status_colors.get(status_value, COLORS["text_secondary"])
        self._status_label.setText(f"Status: {status_value.capitalize()}")
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: 10pt; font-weight: bold; padding: 4px;"
        )

    def _on_error(self, message: str) -> None:
        self._log(f"ERROR: {message}", "error")

    def _on_attention_required(self, request: object) -> None:
        if not isinstance(request, AttentionRequest):
            return
        if self.sender() is not self._current_worker:
            request.resolve(AttentionResolution.STOP)
            return
        self._log(f"ACTION REQUIRED: {request.message}", "warning")
        resolution = prompt_for_attention(self, request)
        request.resolve(resolution)
        if resolution is AttentionResolution.STOP:
            self._stop_run()

    def _on_completed(self, message: str) -> None:
        self._log(f"── {message} ──", "complete")

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not self._current_worker:
            return
        self._reset_controls()

    def _on_screenshot_pre_step(self, screenshot_bytes: bytes) -> None:
        """Pre-execution screenshot: show orange overlay of what's about to happen."""
        pixmap = QPixmap()
        pixmap.loadFromData(screenshot_bytes)
        if pixmap.isNull():
            logger.warning("Flow debugger received invalid pre-step screenshot bytes")
            self._on_screenshot_unavailable(
                "Pre-step screenshot could not be displayed. "
                "The last valid preview is still shown."
            )
            return
        annotated = self._draw_pre_overlays(pixmap, self._last_screenshot_step_id)
        self._screenshot_widget.set_screenshot_pixmap(annotated)
        ts = datetime.now().strftime("%H:%M:%S")
        step_short = self._last_screenshot_step_id[:8] if self._last_screenshot_step_id else "—"
        self._screenshot_info_label.setText(f"Pre-step: {ts}  [{step_short}]")

    def _on_screenshot_updated(self, screenshot_bytes: bytes) -> None:
        """Post-execution screenshot: show green overlay of what happened."""
        pixmap = QPixmap()
        pixmap.loadFromData(screenshot_bytes)
        if pixmap.isNull():
            logger.warning("Flow debugger received invalid post-step screenshot bytes")
            self._on_screenshot_unavailable(
                "Post-step screenshot could not be displayed. "
                "The last valid preview is still shown."
            )
            return
        annotated = self._draw_post_overlays(pixmap, self._last_screenshot_step_id)
        self._screenshot_widget.set_screenshot_pixmap(annotated)
        ts = datetime.now().strftime("%H:%M:%S")
        step_short = self._last_screenshot_step_id[:8] if self._last_screenshot_step_id else "—"
        self._screenshot_info_label.setText(f"Post-step: {ts}  [{step_short}]")

    def _on_screenshot_unavailable(self, message: str) -> None:
        self._screenshot_info_label.setText(
            "Preview unavailable — last valid screenshot retained"
        )
        self._screenshot_info_label.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 10pt; padding: 2px;"
        )
        self._log(message, "warn")

    def _draw_pre_overlays(self, pixmap: QPixmap, step_id: str) -> QPixmap:
        """Draw orange overlays showing what the next command WILL do."""
        cmd = self._step_cmd_map.get(step_id)
        if cmd is None:
            return pixmap
        annotated = pixmap.copy()
        painter = QPainter(annotated)

        if cmd.id in _CLICK_CMDS:
            # Orange crosshair at the target position
            cx, cy = self._get_click_coords(cmd, annotated)
            if cx is not None:
                self._draw_crosshair(
                    painter, cx, cy, QColor(COLORS["overlay_click"]), radius=10
                )
        elif cmd.id in _DETECTION_CMDS:
            # Orange scanning border — we don't know the location yet
            pen = QPen(QColor(COLORS["overlay_click"]), 2)
            painter.setPen(pen)
            painter.drawRect(2, 2, annotated.width() - 4, annotated.height() - 4)

        painter.end()
        return annotated

    def _draw_post_overlays(self, pixmap: QPixmap, step_id: str) -> QPixmap:
        """Draw green overlays showing what the last command DID."""
        annotated = pixmap.copy()
        painter = QPainter(annotated)

        cmd = self._step_cmd_map.get(step_id)

        # Green circle for image match — ONLY for detection commands
        if (
            cmd is not None
            and cmd.id in _DETECTION_CMDS
            and cmd.id != "click_best_target"
            and self._current_worker is not None
            and self._current_worker.ctx.variables.get("_image_found") == "true"
        ):
            try:
                mx = int(self._current_worker.ctx.variables.get("_match_x", "0"))
                my = int(self._current_worker.ctx.variables.get("_match_y", "0"))
                self._draw_match_circle(painter, mx, my, QColor(COLORS["overlay_match"]))
            except (ValueError, TypeError):
                pass

        # Green crosshair for click commands — confirms where the click landed
        if cmd is not None and cmd.id in _CLICK_CMDS:
            cx, cy = self._get_click_coords(cmd, annotated)
            if cx is not None:
                self._draw_crosshair(
                    painter, cx, cy, QColor(COLORS["overlay_match"]), radius=10
                )

        if (
            cmd is not None
            and cmd.id == "click_best_target"
            and self._current_worker is not None
        ):
            variables = self._current_worker.ctx.variables
            try:
                cx = int(variables.get("_visual_chosen_x", "0"))
                cy = int(variables.get("_visual_chosen_y", "0"))
                if cx > 0 or cy > 0:
                    self._draw_crosshair(
                        painter, cx, cy, QColor(COLORS["overlay_best"]), radius=12
                    )
                bx = variables.get("_visual_bonus_x", "")
                by = variables.get("_visual_bonus_y", "")
                if bx and by:
                    self._draw_match_circle(
                        painter, int(bx), int(by), QColor(COLORS["accent"])
                    )
            except (ValueError, TypeError):
                pass

        painter.end()
        return annotated

    # ── Overlay helpers ───────────────────────────────────────────────

    def _get_click_coords(
        self, cmd: FlowCommand, pixmap: QPixmap
    ) -> tuple[int | None, int | None]:
        """Return (cx, cy) pixel coords for a click command, or (None, None)."""
        if cmd.params.get("x") is not None:
            try:
                unit = str(cmd.params.get("coordinate_unit", "pixels")).lower()
                if unit in {"percent", "percentage", "pct", "%"}:
                    return (
                        int(float(cmd.params["x"]) / 100.0 * pixmap.width()),
                        int(float(cmd.params["y"]) / 100.0 * pixmap.height()),
                    )
                return int(float(cmd.params["x"])), int(float(cmd.params["y"]))
            except (ValueError, TypeError):
                pass
        if cmd.params.get("x_pct") is not None:
            try:
                return (
                    int(float(cmd.params["x_pct"]) / 100.0 * pixmap.width()),
                    int(float(cmd.params["y_pct"]) / 100.0 * pixmap.height()),
                )
            except (ValueError, TypeError):
                pass
        return None, None

    def _draw_crosshair(
        self, painter: QPainter, cx: int, cy: int, color: QColor, radius: int = 10
    ) -> None:
        """Draw a crosshair (lines + circle) at (cx, cy)."""
        pen = QPen(color, 3)
        painter.setPen(pen)
        painter.drawLine(cx - radius * 2, cy, cx + radius * 2, cy)
        painter.drawLine(cx, cy - radius * 2, cx, cy + radius * 2)
        painter.drawEllipse(cx - radius, cy - radius, radius * 2, radius * 2)

    def _draw_match_circle(
        self, painter: QPainter, mx: int, my: int, color: QColor
    ) -> None:
        """Draw a match circle + crosshair at (mx, my)."""
        pen = QPen(color, 3)
        painter.setPen(pen)
        painter.drawEllipse(mx - 12, my - 12, 24, 24)
        painter.drawLine(mx - 18, my, mx + 18, my)
        painter.drawLine(mx, my - 18, mx, my + 18)

    # ── Logging ───────────────────────────────────────────────────────

    def _log(self, text: str, level: str = "info") -> None:
        """Append a semantically colored, copyable log line."""
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        append_semantic_log(
            self._log_view,
            text,
            level=level,
            generated_timestamp=timestamp,
            follow=self._tail_log_btn.isChecked(),
        )

    def _clear_log(self) -> None:
        self._log_view.clear()

    # ── Elapsed timer ─────────────────────────────────────────────────

    def _start_elapsed_timer(self) -> None:
        import time
        self._run_start_time = time.monotonic()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

    def _stop_elapsed_timer(self) -> None:
        if hasattr(self, "_elapsed_timer") and self._elapsed_timer.isActive():
            self._elapsed_timer.stop()

    def _update_elapsed(self) -> None:
        import time
        elapsed = int(time.monotonic() - self._run_start_time)
        mins, secs = divmod(elapsed, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            self._elapsed_label.setText(f"Elapsed: {hours}h {mins:02d}m {secs:02d}s")
        elif mins > 0:
            self._elapsed_label.setText(f"Elapsed: {mins}m {secs:02d}s")
        else:
            self._elapsed_label.setText(f"Elapsed: {secs}s")

    # ── Cleanup ───────────────────────────────────────────────────────

    def shutdown(self, *, wait_ms: int = 0) -> bool:
        """Stop all runs and report whether every worker actually exited."""
        self._stop_elapsed_timer()
        return self._runner_service.stop_all(wait_ms=wait_ms)
