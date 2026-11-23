from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from automadin.models.schedule import (
    RunHistoryOutcome,
    SchedulePattern,
    ScheduledFlow,
)
from automadin.services.database import DatabaseService
from automadin.services.schedule_recurrence import next_occurrence
from automadin.services.schedule_repository import ScheduleRepository
from automadin.services.scheduler_host import system_local_timezone
from automadin.ui.launcher.schedule_dialog import ScheduleDialog

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def schedule_summary(schedule: ScheduledFlow) -> str:
    spec = schedule.spec
    if spec.pattern is SchedulePattern.ONCE and spec.start_at_local is not None:
        return f"Once at {spec.start_at_local:%Y-%m-%d %H:%M}"
    if spec.pattern is SchedulePattern.DAILY and spec.local_time is not None:
        return f"Daily at {spec.local_time:%H:%M}"
    if spec.pattern is SchedulePattern.WEEKLY and spec.local_time is not None:
        days = ", ".join(_WEEKDAYS[day] for day in spec.weekdays)
        return f"{days} at {spec.local_time:%H:%M}"
    if spec.pattern is SchedulePattern.INTERVAL and spec.interval_minutes:
        if spec.interval_minutes % 60 == 0:
            hours = spec.interval_minutes // 60
            unit = "hour" if hours == 1 else "hours"
            return f"Every {hours} {unit}"
        unit = "minute" if spec.interval_minutes == 1 else "minutes"
        return f"Every {spec.interval_minutes} {unit}"
    return spec.pattern.value.title()


def _local_timestamp(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


class ScheduleListView(QWidget):
    changed = Signal()
    run_now_requested = Signal(int)

    _COL_NAME = 0

    def __init__(self, database: DatabaseService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._database = database
        self._repository = ScheduleRepository(database)
        self._schedules: dict[str, ScheduledFlow] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        title = QLabel("Schedules")
        title.setStyleSheet("font-size: 11.25pt; font-weight: 700;")
        root.addWidget(title)

        toolbar = QHBoxLayout()
        self._new_button = QPushButton("+ New")
        self._new_button.clicked.connect(self._new)
        toolbar.addWidget(self._new_button)
        self._edit_button = QPushButton("Edit")
        self._edit_button.clicked.connect(self._edit)
        toolbar.addWidget(self._edit_button)
        self._toggle_button = QPushButton("Enable / Disable")
        self._toggle_button.clicked.connect(self._toggle)
        toolbar.addWidget(self._toggle_button)
        self._run_button = QPushButton("▶ Run now")
        self._run_button.clicked.connect(self._run_now)
        toolbar.addWidget(self._run_button)
        self._delete_button = QPushButton("Delete")
        self._delete_button.clicked.connect(self._delete)
        toolbar.addWidget(self._delete_button)
        refresh_button = QPushButton("↻ Refresh")
        refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(refresh_button)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["Name", "Enabled", "Run setup", "Schedule", "Next", "Last result"]
        )
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.itemDoubleClicked.connect(lambda *_args: self._edit())
        root.addWidget(self._tree)

    def refresh(self) -> None:
        selected_id = self._selected_id()
        schedules = self._repository.list()
        self._schedules = {schedule.schedule_id: schedule for schedule in schedules}
        latest = {}
        for history in self._repository.list_history(limit=5000):
            if history.schedule_id and history.schedule_id not in latest:
                latest[history.schedule_id] = history

        self._tree.clear()
        selected_item = None
        for schedule in schedules:
            run_setup = self._database.load_run_config_by_id(schedule.run_config_id)
            history = latest.get(schedule.schedule_id)
            item = QTreeWidgetItem(
                [
                    schedule.name,
                    "Yes" if schedule.enabled else "No",
                    run_setup.name if run_setup is not None else "Missing",
                    schedule_summary(schedule),
                    _local_timestamp(schedule.next_due_at_utc),
                    history.outcome.value.title() if history is not None else "—",
                ]
            )
            item.setData(self._COL_NAME, Qt.ItemDataRole.UserRole, schedule.schedule_id)
            self._tree.addTopLevelItem(item)
            if schedule.schedule_id == selected_id:
                selected_item = item
        for column in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(column)
        if selected_item is not None:
            self._tree.setCurrentItem(selected_item)

    def _selected_id(self) -> str | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        value = item.data(self._COL_NAME, Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _selected(self) -> ScheduledFlow | None:
        identity = self._selected_id()
        return self._schedules.get(identity) if identity else None

    def _new(self) -> None:
        dialog = ScheduleDialog(self._database, parent=self)
        if dialog.exec() != ScheduleDialog.DialogCode.Accepted:
            return
        try:
            name, run_config_id, spec, enabled = dialog.values()
            self._repository.create(
                name=name,
                run_config_id=run_config_id,
                spec=spec,
                enabled=enabled,
                next_due_at_utc=dialog.next_due_at(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Schedule", str(exc))
            return
        self._changed()

    def _edit(self) -> None:
        schedule = self._selected()
        if schedule is None:
            QMessageBox.information(self, "Schedule", "Select a schedule first.")
            return
        dialog = ScheduleDialog(self._database, existing=schedule, parent=self)
        if dialog.exec() != ScheduleDialog.DialogCode.Accepted:
            return
        try:
            name, run_config_id, spec, enabled = dialog.values()
            self._repository.update(
                schedule.schedule_id,
                name=name,
                run_config_id=run_config_id,
                spec=spec,
                enabled=enabled,
                next_due_at_utc=dialog.next_due_at(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Schedule", str(exc))
            return
        self._changed()

    def _toggle(self) -> None:
        schedule = self._selected()
        if schedule is None:
            return
        enabled = not schedule.enabled
        next_due = schedule.next_due_at_utc
        if enabled:
            next_due = next_occurrence(
                schedule.spec,
                after_utc=datetime.now(UTC),
                local_timezone=system_local_timezone(),
            )
            if next_due is None:
                QMessageBox.information(
                    self,
                    "Schedule Finished",
                    "This one-time schedule has no future occurrence. Edit its "
                    "date and time before enabling it.",
                )
                return
        self._repository.update(
            schedule.schedule_id,
            name=schedule.name,
            run_config_id=schedule.run_config_id,
            spec=schedule.spec,
            enabled=enabled,
            next_due_at_utc=next_due,
        )
        self._changed()

    def _run_now(self) -> None:
        schedule = self._selected()
        if schedule is not None:
            self.run_now_requested.emit(schedule.run_config_id)

    def _delete(self) -> None:
        schedule = self._selected()
        if schedule is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete Schedule",
            f"Delete schedule '{schedule.name}'?\n\nIts history will be retained.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._repository.delete(schedule.schedule_id)
        self._changed()

    def _changed(self) -> None:
        self.refresh()
        self.changed.emit()


class ScheduleHistoryView(QWidget):
    def __init__(self, database: DatabaseService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repository = ScheduleRepository(database)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        toolbar = QHBoxLayout()
        title = QLabel("Scheduled run history")
        title.setStyleSheet("font-size: 11.25pt; font-weight: 700;")
        toolbar.addWidget(title)
        toolbar.addStretch(1)
        refresh_button = QPushButton("↻ Refresh")
        refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(refresh_button)
        root.addLayout(toolbar)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            [
                "Scheduled",
                "Schedule",
                "Run setup",
                "Flow",
                "Outcome",
                "Started",
                "Finished",
                "Message",
            ]
        )
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        root.addWidget(self._tree)

    def refresh(self) -> None:
        self._tree.clear()
        for record in self._repository.list_history(limit=500):
            item = QTreeWidgetItem(
                [
                    _local_timestamp(record.scheduled_for_utc),
                    record.schedule_name,
                    record.run_config_name,
                    record.flow_name,
                    record.outcome.value.title(),
                    _local_timestamp(record.started_at_utc),
                    _local_timestamp(record.finished_at_utc),
                    record.message or "—",
                ]
            )
            if record.outcome in {
                RunHistoryOutcome.FAILED,
                RunHistoryOutcome.BLOCKED,
                RunHistoryOutcome.REQUIRES_ATTENTION,
            }:
                item.setToolTip(4, record.message)
            self._tree.addTopLevelItem(item)
        for column in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(column)
