from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from PySide6.QtCore import QDate, QDateTime, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QStackedWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from automadin.models.schedule import (
    MissedRunPolicy,
    OverlapPolicy,
    SchedulePattern,
    ScheduleSpec,
    ScheduledFlow,
)
from automadin.services.database import DatabaseService
from automadin.services.schedule_recurrence import next_occurrence
from automadin.services.scheduler_host import system_local_timezone

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _qt_datetime(value: datetime) -> QDateTime:
    return QDateTime(
        QDate(value.year, value.month, value.day),
        QTime(value.hour, value.minute),
    )


def _python_datetime(value: QDateTime) -> datetime:
    date_value = value.date()
    time_value = value.time()
    return datetime(
        date_value.year(),
        date_value.month(),
        date_value.day(),
        time_value.hour(),
        time_value.minute(),
    )


class ScheduleDialog(QDialog):
    """Create or edit one schedule using explicit recurrence controls."""

    def __init__(
        self,
        database: DatabaseService,
        *,
        existing: ScheduledFlow | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._existing = existing
        self._timezone = system_local_timezone()
        self.setWindowTitle("Edit Schedule" if existing else "New Schedule")
        self.setMinimumWidth(560)
        self._build_ui()
        self._load(existing)
        self._connect_preview()
        self._refresh_preview()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()

        self._name = QLineEdit()
        self._name.setMaxLength(160)
        form.addRow("Name", self._name)

        self._run_setup = QComboBox()
        for name in self._database.list_run_configs():
            run_config_id = self._database.get_run_config_id(name)
            if run_config_id is not None:
                self._run_setup.addItem(name, run_config_id)
        form.addRow("Run setup", self._run_setup)

        self._pattern = QComboBox()
        for label, pattern in (
            ("Once", SchedulePattern.ONCE),
            ("Daily", SchedulePattern.DAILY),
            ("Weekly", SchedulePattern.WEEKLY),
            ("Interval", SchedulePattern.INTERVAL),
        ):
            self._pattern.addItem(label, pattern)
        form.addRow("Pattern", self._pattern)

        self._pattern_pages = QStackedWidget()
        self._once_at = self._date_time_editor()
        self._pattern_pages.addWidget(self._form_page("Run at", self._once_at))

        self._daily_time = QTimeEdit()
        self._daily_time.setDisplayFormat("HH:mm")
        self._pattern_pages.addWidget(self._form_page("Time", self._daily_time))

        weekly_page = QWidget()
        weekly_form = QFormLayout(weekly_page)
        self._weekly_time = QTimeEdit()
        self._weekly_time.setDisplayFormat("HH:mm")
        weekly_form.addRow("Time", self._weekly_time)
        weekday_row = QWidget()
        weekday_layout = QHBoxLayout(weekday_row)
        weekday_layout.setContentsMargins(0, 0, 0, 0)
        self._weekday_checks: list[QCheckBox] = []
        for label in _WEEKDAYS:
            checkbox = QCheckBox(label)
            self._weekday_checks.append(checkbox)
            weekday_layout.addWidget(checkbox)
        weekly_form.addRow("Days", weekday_row)
        self._pattern_pages.addWidget(weekly_page)

        interval_page = QWidget()
        interval_form = QFormLayout(interval_page)
        self._interval_start = self._date_time_editor()
        interval_form.addRow("Starting", self._interval_start)
        interval_row = QWidget()
        interval_layout = QHBoxLayout(interval_row)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        self._interval_amount = QSpinBox()
        self._interval_amount.setRange(1, 10080)
        self._interval_unit = QComboBox()
        self._interval_unit.addItem("minutes", 1)
        self._interval_unit.addItem("hours", 60)
        interval_layout.addWidget(self._interval_amount)
        interval_layout.addWidget(self._interval_unit)
        interval_layout.addStretch(1)
        interval_form.addRow("Every", interval_row)
        self._pattern_pages.addWidget(interval_page)
        form.addRow("", self._pattern_pages)

        self._missed = QComboBox()
        self._missed.addItem("Skip", MissedRunPolicy.SKIP)
        self._missed.addItem(
            "Run once when Automadin resumes",
            MissedRunPolicy.RUN_ONCE,
        )
        form.addRow("If missed", self._missed)

        self._overlap = QComboBox()
        self._overlap.addItem("Skip", OverlapPolicy.SKIP)
        self._overlap.addItem("Queue one", OverlapPolicy.QUEUE_ONE)
        form.addRow("If still running", self._overlap)

        self._enabled = QCheckBox("Enable this schedule")
        self._enabled.setChecked(True)
        form.addRow("", self._enabled)
        root.addLayout(form)

        preview_title = QLabel("Next occurrences")
        preview_title.setStyleSheet("font-weight: 600;")
        root.addWidget(preview_title)
        self._preview = QLabel()
        self._preview.setWordWrap(True)
        root.addWidget(self._preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _date_time_editor() -> QDateTimeEdit:
        editor = QDateTimeEdit()
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("yyyy-MM-dd HH:mm")
        return editor

    @staticmethod
    def _form_page(label: str, widget: QWidget) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)
        layout.addRow(label, widget)
        return page

    def _load(self, existing: ScheduledFlow | None) -> None:
        default_local = (
            datetime.now(self._timezone).replace(second=0, microsecond=0)
            + timedelta(minutes=1)
        ).replace(tzinfo=None)
        self._once_at.setDateTime(_qt_datetime(default_local))
        self._interval_start.setDateTime(_qt_datetime(default_local))
        default_time = QTime(default_local.hour, default_local.minute)
        self._daily_time.setTime(default_time)
        self._weekly_time.setTime(default_time)
        self._interval_amount.setValue(60)
        self._interval_unit.setCurrentIndex(0)
        if existing is None:
            return

        self._name.setText(existing.name)
        setup_index = self._run_setup.findData(existing.run_config_id)
        if setup_index >= 0:
            self._run_setup.setCurrentIndex(setup_index)
        pattern_index = self._pattern.findData(existing.spec.pattern)
        self._pattern.setCurrentIndex(max(0, pattern_index))
        self._enabled.setChecked(existing.enabled)
        self._missed.setCurrentIndex(
            max(0, self._missed.findData(existing.spec.missed_policy))
        )
        self._overlap.setCurrentIndex(
            max(0, self._overlap.findData(existing.spec.overlap_policy))
        )
        spec = existing.spec
        if spec.start_at_local is not None:
            value = _qt_datetime(spec.start_at_local)
            self._once_at.setDateTime(value)
            self._interval_start.setDateTime(value)
        if spec.local_time is not None:
            value = QTime(spec.local_time.hour, spec.local_time.minute)
            self._daily_time.setTime(value)
            self._weekly_time.setTime(value)
        for day, checkbox in enumerate(self._weekday_checks):
            checkbox.setChecked(day in spec.weekdays)
        if spec.interval_minutes:
            if spec.interval_minutes % 60 == 0:
                self._interval_amount.setValue(spec.interval_minutes // 60)
                self._interval_unit.setCurrentIndex(1)
            else:
                self._interval_amount.setValue(spec.interval_minutes)
                self._interval_unit.setCurrentIndex(0)

    def _connect_preview(self) -> None:
        self._pattern.currentIndexChanged.connect(self._pattern_changed)
        self._once_at.dateTimeChanged.connect(self._refresh_preview)
        self._daily_time.timeChanged.connect(self._refresh_preview)
        self._weekly_time.timeChanged.connect(self._refresh_preview)
        self._interval_start.dateTimeChanged.connect(self._refresh_preview)
        self._interval_amount.valueChanged.connect(self._refresh_preview)
        self._interval_unit.currentIndexChanged.connect(self._refresh_preview)
        for checkbox in self._weekday_checks:
            checkbox.toggled.connect(self._refresh_preview)
        self._pattern_changed()

    def _pattern_changed(self) -> None:
        self._pattern_pages.setCurrentIndex(self._pattern.currentIndex())
        self._refresh_preview()

    def schedule_spec(self) -> ScheduleSpec:
        pattern = self._pattern.currentData()
        common = {
            "missed_policy": self._missed.currentData(),
            "overlap_policy": self._overlap.currentData(),
        }
        if pattern is SchedulePattern.ONCE:
            return ScheduleSpec(
                pattern=pattern,
                start_at_local=_python_datetime(self._once_at.dateTime()),
                **common,
            )
        if pattern is SchedulePattern.DAILY:
            value = self._daily_time.time()
            return ScheduleSpec(
                pattern=pattern,
                local_time=time(value.hour(), value.minute()),
                **common,
            )
        if pattern is SchedulePattern.WEEKLY:
            value = self._weekly_time.time()
            return ScheduleSpec(
                pattern=pattern,
                local_time=time(value.hour(), value.minute()),
                weekdays=tuple(
                    day
                    for day, checkbox in enumerate(self._weekday_checks)
                    if checkbox.isChecked()
                ),
                **common,
            )
        return ScheduleSpec(
            pattern=SchedulePattern.INTERVAL,
            start_at_local=_python_datetime(self._interval_start.dateTime()),
            interval_minutes=(
                self._interval_amount.value() * int(self._interval_unit.currentData())
            ),
            **common,
        )

    def values(self) -> tuple[str, int, ScheduleSpec, bool]:
        run_config_id = self._run_setup.currentData()
        if not isinstance(run_config_id, int):
            raise ValueError("Create a run setup before creating a schedule")
        name = self._name.text().strip()
        if not name:
            raise ValueError("Schedule name is required")
        return name, run_config_id, self.schedule_spec(), self._enabled.isChecked()

    def next_due_at(self, *, after_utc: datetime | None = None) -> datetime | None:
        return next_occurrence(
            self.schedule_spec(),
            after_utc=after_utc or datetime.now(UTC),
            local_timezone=self._timezone,
        )

    def _refresh_preview(self, *_args) -> None:
        try:
            spec = self.schedule_spec()
            cursor = datetime.now(UTC)
            occurrences: list[datetime] = []
            for _ in range(3):
                occurrence = next_occurrence(
                    spec,
                    after_utc=cursor,
                    local_timezone=self._timezone,
                )
                if occurrence is None:
                    break
                occurrences.append(occurrence)
                cursor = occurrence
        except ValueError as exc:
            self._preview.setText(str(exc))
            return
        if not occurrences:
            self._preview.setText("No future occurrence.")
            return
        self._preview.setText(
            "\n".join(
                occurrence.astimezone(self._timezone).strftime(
                    "%a %Y-%m-%d %H:%M %Z"
                )
                for occurrence in occurrences
            )
        )

    def _accept_if_valid(self) -> None:
        try:
            _name, _run_config_id, _spec, enabled = self.values()
            if enabled and self.next_due_at() is None:
                raise ValueError(
                    "An enabled schedule must have a future occurrence"
                )
        except ValueError as exc:
            QMessageBox.warning(self, "Schedule", str(exc))
            return
        self.accept()
