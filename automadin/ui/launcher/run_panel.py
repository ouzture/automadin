"""Run panel widget — shows live log and status for an active flow run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from automadin.execution.attention import AttentionRequest, AttentionResolution
from automadin.execution.runner import FlowRunWorker
from automadin.execution.log_granularity import LogGranularity
from automadin.models.run_config import RunStatus
from automadin.ui.attention_dialog import prompt_for_attention
from automadin.ui.execution_error_details import ExecutionErrorBar
from automadin.ui.log_formatting import append_semantic_log
from automadin.ui.theme import COLORS
from automadin.ui.widgets.buttons import configure_button, fit_button_to_contents
from automadin.ui.widgets.combo_boxes import configure_combo_box
from automadin.ui.widgets.toggle_switch import ToggleSwitch
from automadin.ui.widgets.wrapping_layout import WrappingLayout

_MAX_LOG_LINES = 2000
_TRIM_TO = 1500
_ACTION_BUTTON_WIDTH = 64
_RESTART_BUTTON_WIDTH = 96
_TERMINAL_STATUSES = {
    RunStatus.STOPPED,
    RunStatus.ERROR,
    RunStatus.COMPLETED,
}

_STATUS_COLOR_TOKENS: dict[RunStatus, str] = {
    RunStatus.IDLE: "text_secondary",
    RunStatus.CONNECTING: "warning",
    RunStatus.ACQUIRING_EMULATOR: "warning",
    RunStatus.STARTING_EMULATOR: "warning",
    RunStatus.WAITING_FOR_ANDROID: "warning",
    RunStatus.CHECKING_ANDROID: "warning",
    RunStatus.RUNNING: "success",
    RunStatus.PAUSING: "warning",
    RunStatus.PAUSED: "warning",
    RunStatus.STOPPING: "warning",
    RunStatus.REQUIRES_ATTENTION: "warning",
    RunStatus.STOPPED: "orange",
    RunStatus.ERROR: "error",
    RunStatus.COMPLETED: "primary",
    RunStatus.CLEANING_UP: "warning",
    RunStatus.CLEANUP_PENDING: "error",
}


@dataclass(frozen=True)
class RunPanelSnapshot:
    """Read-only run presentation shared with compact controller surfaces."""

    run_id: str
    name: str
    target_name: str
    status: RunStatus
    can_pause_resume: bool
    can_stop: bool
    can_restart: bool
    close_pending: bool


class RunPanel(QFrame):
    """Card showing live log output for one active flow run."""

    presentation_changed = Signal()

    def __init__(
        self,
        config_name: str,
        run_id: str,
        worker: FlowRunWorker,
        on_close: Any = None,
        parent=None,
        *,
        run_config_id: int | None = None,
        on_restart: Any = None,
    ) -> None:
        super().__init__(parent)
        self._config_name = config_name
        self._run_config_id = run_config_id
        self._run_id = run_id
        self._worker = worker
        self._on_close = on_close
        self._on_restart = on_restart
        self._line_count = 0
        self._close_requested = False
        self._previous_binding: tuple[
            str,
            FlowRunWorker,
            str,
            int,
            object | None,
        ] | None = None

        self.setObjectName("RunPanel")
        self.setStyleSheet(f"""
            #RunPanel {{
                background-color: {COLORS["bg_surface"]};
                border: 1px solid {COLORS["border"]};
                border-radius: 8px;
                margin: 4px 0;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # ── Header ──
        header = QHBoxLayout()
        header.setSpacing(8)

        name_label = QLabel(config_name)
        name_label.setStyleSheet(
            f"font-weight: bold; font-size: 10.5pt; color: {COLORS['text_primary']};"
        )
        header.addWidget(name_label)

        self._status_badge = QLabel("IDLE")
        self._status_badge.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; font-weight: 600; "
            f"padding: 2px 8px; border-radius: 4px; "
            f"background-color: {COLORS['bg_elevated']};"
        )
        header.addWidget(self._status_badge)

        header.addStretch()

        close_btn = QPushButton("\u00d7")
        close_btn.setFixedSize(28, 28)
        close_btn.setToolTip("Close panel")
        close_btn.setAccessibleName(f"Close run panel: {config_name}")
        close_btn.setStyleSheet(
            f"QPushButton {{ color: {COLORS['text_secondary']}; font-size: 12pt; "
            f"font-weight: bold; border: none; padding: 0; }}"
            f"QPushButton:hover {{ color: {COLORS['error']}; }}"
        )
        close_btn.clicked.connect(self._on_close_clicked)
        self._close_btn = close_btn
        header.addWidget(close_btn)

        layout.addLayout(header)

        controls_widget = QWidget()
        controls = WrappingLayout(controls_widget)
        controls.setContentsMargins(0, 0, 0, 0)
        self._controls_widget = controls_widget

        tail_controls = QWidget()
        tail_layout = QHBoxLayout(tail_controls)
        tail_layout.setContentsMargins(0, 0, 0, 0)
        tail_layout.setSpacing(6)
        tail_label = QLabel("Tail")
        tail_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt;"
        )
        tail_layout.addWidget(tail_label)

        tail_btn = ToggleSwitch()
        tail_btn.setChecked(True)
        tail_btn.setAccessibleName("Tail log")
        tail_btn.setToolTip(
            "Follow new log lines. Turn off to keep your current scroll position."
        )
        self._tail_btn = tail_btn
        tail_layout.addWidget(tail_btn)
        controls.addWidget(tail_controls)

        granularity_controls = QWidget()
        granularity_layout = QHBoxLayout(granularity_controls)
        granularity_layout.setContentsMargins(0, 0, 0, 0)
        granularity_layout.setSpacing(6)
        granularity_label = QLabel("Level")
        granularity_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt;"
        )
        granularity_layout.addWidget(granularity_label)
        self._log_granularity_combo = QComboBox()
        self._log_granularity_combo.addItem("Normal", LogGranularity.NORMAL)
        self._log_granularity_combo.addItem("Detailed", LogGranularity.DETAILED)
        self._log_granularity_combo.addItem("Trace", LogGranularity.TRACE)
        configure_combo_box(
            self._log_granularity_combo,
            density="compact",
            minimum_contents_length=len("Detailed"),
        )
        self._log_granularity_combo.setAccessibleName("Run log detail level")
        self._log_granularity_combo.setToolTip(
            "Normal shows outcomes; Detailed adds summaries and timing; "
            "Trace adds every poll and iteration. Changes apply to new lines."
        )
        granularity_layout.addWidget(self._log_granularity_combo)
        controls.addWidget(granularity_controls)

        pause_btn = QPushButton("Pause")
        configure_button(pause_btn, density="compact", minimum_width=_ACTION_BUTTON_WIDTH)
        pause_btn.setAccessibleName(f"Pause or resume run: {config_name}")
        pause_btn.clicked.connect(self._on_pause_resume)
        self._pause_btn = pause_btn
        controls.addWidget(pause_btn)

        stop_btn = QPushButton("Stop")
        configure_button(
            stop_btn,
            semantic="danger",
            appearance="outline",
            density="compact",
            minimum_width=_ACTION_BUTTON_WIDTH,
        )
        stop_btn.setAccessibleName(f"Stop run: {config_name}")
        stop_btn.clicked.connect(self._on_stop)
        self._stop_btn = stop_btn
        controls.addWidget(stop_btn)

        restart_btn = QPushButton("Run again")
        configure_button(
            restart_btn,
            density="compact",
            minimum_width=_RESTART_BUTTON_WIDTH,
        )
        restart_btn.setAccessibleName(f"Run again: {config_name}")
        restart_btn.setToolTip("Start a fresh attempt in this panel")
        restart_btn.clicked.connect(self._on_restart_clicked)
        self._restart_btn = restart_btn
        controls.addWidget(restart_btn)

        clear_btn = QPushButton("Clear")
        configure_button(
            clear_btn,
            appearance="outline",
            density="compact",
            minimum_width=_ACTION_BUTTON_WIDTH,
        )
        clear_btn.clicked.connect(self._clear_log)
        self._clear_btn = clear_btn
        controls.addWidget(clear_btn)
        layout.addWidget(controls_widget)

        # ── Log area ──
        self._log_area = QTextEdit()
        self._log_area.setReadOnly(True)
        self._log_area.setFont(QFont("Consolas", 11))
        self._log_area.setMinimumHeight(150)
        self._log_area.setStyleSheet(
            f"QTextEdit {{ background-color: {COLORS['bg_darkest']}; "
            f"border: 1px solid {COLORS['border_light']}; border-radius: 4px; "
            f"padding: 6px; color: {COLORS['text_primary']}; }}"
        )
        layout.addWidget(self._log_area, 1)

        self._error_bar = ExecutionErrorBar()
        layout.addWidget(self._error_bar)

        self._connect_worker(worker)
        self._sync_actions(worker.status)

    def _connect_worker(self, worker: FlowRunWorker) -> None:
        """Observe one execution attempt owned by this reusable panel."""
        worker.enable_attention()
        worker.log_message.connect(self._append_log)
        worker.detailed_log_message.connect(self._append_detailed_log)
        worker.trace_log_message.connect(self._append_trace_log)
        worker.status_changed.connect(self._update_status)
        worker.error_occurred.connect(self._on_error)
        worker.error_details_available.connect(self._error_bar.show_report)
        worker.attention_required.connect(self._on_attention_required)
        worker.finished.connect(self._on_worker_finished)

    def rebind(self, run_id: str, worker: FlowRunWorker) -> None:
        """Display a fresh execution attempt without replacing this panel."""
        self._previous_binding = (
            self._run_id,
            self._worker,
            self._log_area.toHtml(),
            self._line_count,
            self._error_bar.report,
        )
        previous = self._worker.status.value.title()
        self._run_id = run_id
        self._worker = worker
        self._close_requested = False
        self._error_bar.clear_report()
        self._close_btn.setEnabled(True)
        self._append_log(
            "\n"
            f"──────── Run again · {datetime.now().astimezone():%Y-%m-%d %H:%M:%S} "
            "────────"
        )
        self._append_log(f"Previous result: {previous}. Starting a new attempt…")
        self._connect_worker(worker)
        self._update_status(worker.status.value)

    def commit_rebind(self) -> None:
        self._previous_binding = None

    def rollback_rebind(self) -> str | None:
        """Restore the finished attempt when a replacement never started."""
        previous = self._previous_binding
        self._previous_binding = None
        if previous is None:
            return None
        run_id, worker, log_html, line_count, error_report = previous
        self._run_id = run_id
        self._worker = worker
        self._log_area.setHtml(log_html)
        self._line_count = line_count
        if error_report is None:
            self._error_bar.clear_report()
        else:
            self._error_bar.show_report(error_report)
        self._update_status(worker.status.value)
        return run_id

    def _append_log(self, msg: str) -> None:
        self._line_count += 1
        if self._line_count > _MAX_LOG_LINES:
            self._trim_log()
        append_semantic_log(
            self._log_area,
            msg,
            follow=self._tail_btn.isChecked(),
        )

    def _log_granularity(self) -> LogGranularity:
        return LogGranularity(self._log_granularity_combo.currentData())

    def _append_detailed_log(self, msg: str) -> None:
        if self._log_granularity() >= LogGranularity.DETAILED:
            self._append_log(msg)

    def _append_trace_log(self, msg: str) -> None:
        if self._log_granularity() >= LogGranularity.TRACE:
            self._append_log(msg)

    def _trim_log(self) -> None:
        text = self._log_area.toPlainText()
        lines = text.split("\n")
        if len(lines) > _TRIM_TO:
            self._log_area.setPlainText("\n".join(lines[-_TRIM_TO:]))
            self._line_count = _TRIM_TO

    def _update_status(self, status_value: str) -> None:
        try:
            status = RunStatus(status_value)
        except ValueError:
            status = RunStatus.IDLE
        color_token = _STATUS_COLOR_TOKENS.get(status, "text_secondary")
        color = COLORS[color_token]
        self._status_badge.setText(status.value.upper())
        self._status_badge.setStyleSheet(
            f"color: {COLORS['text_on_accent']}; font-size: 10pt; font-weight: 600; "
            f"padding: 2px 8px; border-radius: 4px; background-color: {color};"
        )
        self._sync_actions(status)
        self.presentation_changed.emit()

    def _sync_actions(self, status: RunStatus) -> None:
        running = self._worker.isRunning()
        if status is RunStatus.PAUSED:
            self._pause_btn.setText("Resume")
            self._pause_btn.setEnabled(running)
        elif status is RunStatus.PAUSING:
            self._pause_btn.setText("Pausing…")
            self._pause_btn.setEnabled(False)
        else:
            self._pause_btn.setText("Pause")
            self._pause_btn.setEnabled(
                running
                and status
                not in {
                    RunStatus.STOPPING,
                    RunStatus.CLEANING_UP,
                    RunStatus.CLEANUP_PENDING,
                    RunStatus.REQUIRES_ATTENTION,
                    *_TERMINAL_STATUSES,
                }
            )
        self._stop_btn.setEnabled(
            running
            and status
            not in {
                RunStatus.STOPPING,
                RunStatus.CLEANING_UP,
                RunStatus.CLEANUP_PENDING,
                *_TERMINAL_STATUSES,
            }
        )
        if status is RunStatus.STOPPING:
            self._stop_btn.setText("Stopping…")
        elif status is RunStatus.STOPPED:
            self._stop_btn.setText("Stopped")
        else:
            self._stop_btn.setText("Stop")
        fit_button_to_contents(self._pause_btn)
        fit_button_to_contents(self._stop_btn)
        can_restart = (
            not running
            and self._worker.isFinished()
            and status in _TERMINAL_STATUSES
            and self._run_config_id is not None
            and self._on_restart is not None
        )
        self._restart_btn.setEnabled(can_restart)

    def _on_error(self, msg: str) -> None:
        append_semantic_log(
            self._log_area,
            f"ERROR: {msg}",
            level="error",
            follow=self._tail_btn.isChecked(),
        )

    def _on_attention_required(self, request: object) -> None:
        if not isinstance(request, AttentionRequest):
            return
        self._append_log(f"ACTION REQUIRED: {request.message}")
        resolution = prompt_for_attention(self, request)
        request.resolve(resolution)
        if resolution is AttentionResolution.STOP:
            self._on_stop()

    def _clear_log(self) -> None:
        self._log_area.clear()
        self._line_count = 0

    def _on_pause_resume(self) -> None:
        if self._worker.status is RunStatus.PAUSED:
            self._worker.request_resume()
        else:
            self._worker.request_pause()

    def _on_stop(self) -> None:
        if not self._worker.isRunning():
            return
        self._worker.request_stop()
        self._stop_btn.setText("Stopping…")
        self._stop_btn.setEnabled(False)
        self._status_badge.setText("STOPPING")
        self._status_badge.setStyleSheet(
            f"color: {COLORS['text_on_accent']}; font-size: 10pt; font-weight: 600; "
            f"padding: 2px 8px; border-radius: 4px; "
            f"background-color: {COLORS['warning']};"
        )
        self._sync_actions(RunStatus.STOPPING)

    def _on_restart_clicked(
        self,
        _checked: bool = False,
        *,
        interaction_parent: QWidget | None = None,
    ) -> None:
        if self._on_restart is None:
            return
        self._restart_btn.setEnabled(False)
        self.presentation_changed.emit()
        if interaction_parent is None:
            restarted = self._on_restart(self)
        else:
            restarted = self._on_restart(
                self,
                interaction_parent=interaction_parent,
            )
        if not restarted:
            self._sync_actions(self._worker.status)

    def _on_close_clicked(self) -> None:
        if self._worker.isRunning():
            self._close_requested = True
            self._close_btn.setEnabled(False)
            self.presentation_changed.emit()
            self._on_stop()
            return
        self._attempt_close()

    def _on_worker_finished(self) -> None:
        status = self._worker.status
        if status not in {RunStatus.STOPPED, RunStatus.ERROR, RunStatus.COMPLETED}:
            status = RunStatus.STOPPED if self._worker.ctx.is_stopped else status
        self._update_status(status.value)
        self._stop_btn.setText("Stopped" if status == RunStatus.STOPPED else "Stop")
        self._stop_btn.setEnabled(False)
        self._sync_actions(status)
        if self._close_requested:
            self._attempt_close()

    def _attempt_close(self) -> bool:
        if self._on_close is None:
            return True
        if self._on_close(self._run_id) is not False:
            return True
        self.show_cleanup_pending()
        return False

    def show_cleanup_pending(self) -> None:
        """Keep this panel visibly retryable while ownership is retained."""
        self._worker.mark_cleanup_pending()
        self._close_btn.setEnabled(True)
        self._status_badge.setText("CLEANUP PENDING")
        self._status_badge.setStyleSheet(
            f"color: {COLORS['text_on_accent']}; font-size: 10pt; font-weight: 600; "
            f"padding: 2px 8px; border-radius: 4px; "
            f"background-color: {COLORS['warning']};"
        )
        self._append_log(
            "Cleanup is still pending. Use Close to retry after checking Diagnostics."
        )
        self._sync_actions(RunStatus.CLEANUP_PENDING)
        self.presentation_changed.emit()

    def snapshot(self) -> RunPanelSnapshot:
        """Return the truthful controls and labels for this retained panel."""
        return RunPanelSnapshot(
            run_id=self._run_id,
            name=self._config_name,
            target_name=self._worker.target_display_name,
            status=self._worker.status,
            can_pause_resume=self._pause_btn.isEnabled(),
            can_stop=self._stop_btn.isEnabled(),
            can_restart=self._restart_btn.isEnabled(),
            close_pending=self._close_requested,
        )

    def request_pause_or_resume(self) -> None:
        if self._pause_btn.isEnabled():
            self._on_pause_resume()

    def request_stop(self) -> None:
        if self._stop_btn.isEnabled():
            self._on_stop()

    def request_restart(self, *, interaction_parent: QWidget | None = None) -> None:
        if self._restart_btn.isEnabled():
            self._on_restart_clicked(interaction_parent=interaction_parent)

    def request_close(self) -> None:
        if self._close_btn.isEnabled():
            self._on_close_clicked()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def run_config_id(self) -> int | None:
        return self._run_config_id
