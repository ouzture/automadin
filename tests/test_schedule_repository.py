from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from automadin.models.flow import AutomationFlow
from automadin.models.run_config import RunConfig, SavedRunConfig
from automadin.models.schedule import (
    RunHistoryOutcome,
    SchedulePattern,
    ScheduleSpec,
)
from automadin.services.database import CURRENT_SCHEMA_VERSION, DatabaseService
from automadin.services.schedule_repository import ScheduleRepository


def _database_with_run_setup(path: Path) -> tuple[DatabaseService, int]:
    database = DatabaseService(str(path))
    database.save_flow("Morning flow", AutomationFlow("Morning flow"))
    run_setup = SavedRunConfig(
        name="Morning run",
        flow_name="Morning flow",
        device_id="android:test",
        config=RunConfig(),
    )
    database.save_run_config(run_setup.name, run_setup)
    identity = database.get_run_config_id(run_setup.name)
    assert identity is not None
    return database, identity


def _daily_spec() -> ScheduleSpec:
    return ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0))


def test_schema_v4_contains_schedule_tables_and_indexes(tmp_path: Path) -> None:
    path = tmp_path / "automadin.db"
    database = DatabaseService(str(path))
    database.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            CURRENT_SCHEMA_VERSION
        )
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert {"schedules", "run_history"} <= tables
    assert {"idx_schedules_due", "idx_run_history_scheduled"} <= indexes


def test_create_update_and_list_schedule(tmp_path: Path) -> None:
    database, run_config_id = _database_with_run_setup(tmp_path / "automadin.db")
    repository = ScheduleRepository(database)
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)

    created = repository.create(
        name="Morning",
        run_config_id=run_config_id,
        spec=_daily_spec(),
        next_due_at_utc=due,
    )
    assert repository.list() == [created]

    changed_due = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    changed_spec = ScheduleSpec(
        pattern=SchedulePattern.WEEKLY,
        local_time=time(10, 0),
        weekdays=(6,),
    )
    assert repository.update(
        created.schedule_id,
        name="Weekend",
        run_config_id=run_config_id,
        spec=changed_spec,
        enabled=False,
        next_due_at_utc=changed_due,
    )

    updated = repository.get(created.schedule_id)
    assert updated is not None
    assert updated.name == "Weekend"
    assert updated.spec == changed_spec
    assert updated.enabled is False
    assert updated.next_due_at_utc == changed_due
    database.close()


def test_schedule_names_are_unique_without_case_sensitivity(tmp_path: Path) -> None:
    database, run_config_id = _database_with_run_setup(tmp_path / "automadin.db")
    repository = ScheduleRepository(database)
    repository.create(
        name="Morning",
        run_config_id=run_config_id,
        spec=_daily_spec(),
    )

    with pytest.raises(ValueError, match="already exists"):
        repository.create(
            name="morning",
            run_config_id=run_config_id,
            spec=_daily_spec(),
        )
    database.close()


def test_next_enabled_returns_the_earliest_named_schedule(tmp_path: Path) -> None:
    database, run_config_id = _database_with_run_setup(tmp_path / "automadin.db")
    repository = ScheduleRepository(database)
    later = repository.create(
        name="Later",
        run_config_id=run_config_id,
        spec=_daily_spec(),
        next_due_at_utc=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
    )
    earlier = repository.create(
        name="Earlier",
        run_config_id=run_config_id,
        spec=_daily_spec(),
        next_due_at_utc=datetime(2026, 7, 25, 9, 0, tzinfo=UTC),
    )

    assert repository.next_enabled() == earlier
    assert repository.next_enabled_due() == earlier.next_due_at_utc
    assert later.next_due_at_utc > earlier.next_due_at_utc
    database.close()


def test_run_setup_rename_preserves_schedule_reference(tmp_path: Path) -> None:
    database, run_config_id = _database_with_run_setup(tmp_path / "automadin.db")
    repository = ScheduleRepository(database)
    schedule = repository.create(
        name="Morning",
        run_config_id=run_config_id,
        spec=_daily_spec(),
    )

    assert database.rename_run_config("Morning run", "Renamed run")
    assert repository.get(schedule.schedule_id).run_config_id == run_config_id
    assert database.delete_run_config("Renamed run") is False
    database.close()


def test_atomic_claim_advances_once_and_snapshots_names(tmp_path: Path) -> None:
    path = tmp_path / "automadin.db"
    first_database, run_config_id = _database_with_run_setup(path)
    first_repository = ScheduleRepository(first_database)
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    next_due = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    schedule = first_repository.create(
        name="Morning",
        run_config_id=run_config_id,
        spec=_daily_spec(),
        next_due_at_utc=due,
    )
    second_database = DatabaseService(str(path))
    second_repository = ScheduleRepository(second_database)

    claimed = first_repository.claim_occurrence(
        schedule.schedule_id,
        expected_due_at_utc=due,
        next_due_at_utc=next_due,
    )
    duplicate = second_repository.claim_occurrence(
        schedule.schedule_id,
        expected_due_at_utc=due,
        next_due_at_utc=next_due,
    )

    assert claimed is not None
    assert claimed.outcome is RunHistoryOutcome.PENDING
    assert claimed.schedule_name == "Morning"
    assert claimed.run_config_name == "Morning run"
    assert claimed.flow_name == "Morning flow"
    assert duplicate is None
    updated = first_repository.get(schedule.schedule_id)
    assert updated.next_due_at_utc == next_due
    assert updated.last_due_at_utc == due
    assert first_repository.list_history() == [claimed]
    second_database.close()
    first_database.close()


def test_history_transitions_and_survives_schedule_deletion(tmp_path: Path) -> None:
    database, run_config_id = _database_with_run_setup(tmp_path / "automadin.db")
    repository = ScheduleRepository(database)
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    schedule = repository.create(
        name="Morning",
        run_config_id=run_config_id,
        spec=_daily_spec(),
        next_due_at_utc=due,
    )
    run_id = str(uuid.uuid4())
    claimed = repository.claim_occurrence(
        schedule.schedule_id,
        expected_due_at_utc=due,
        next_due_at_utc=None,
        run_id=run_id,
    )
    assert claimed is not None

    started = datetime(2026, 7, 25, 9, 1, tzinfo=UTC)
    finished = datetime(2026, 7, 25, 9, 2, tzinfo=UTC)
    assert repository.mark_running(run_id, started_at_utc=started)
    assert repository.finish(
        run_id,
        outcome=RunHistoryOutcome.COMPLETED,
        message="done",
        finished_at_utc=finished,
    )
    assert repository.delete(schedule.schedule_id)
    assert database.delete_run_config("Morning run")

    history = repository.get_history(run_id)
    assert history is not None
    assert history.schedule_id is None
    assert history.run_config_id is None
    assert history.schedule_name == "Morning"
    assert history.run_config_name == "Morning run"
    assert history.started_at_utc == started
    assert history.finished_at_utc == finished
    assert history.outcome is RunHistoryOutcome.COMPLETED
    assert history.message == "done"
    database.close()


def test_history_pruning_preserves_active_runs_and_keeps_newest_terminal(
    tmp_path: Path,
) -> None:
    database, _run_config_id = _database_with_run_setup(tmp_path / "automadin.db")
    repository = ScheduleRepository(database)
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    rows = []
    for index, outcome in enumerate(("completed", "completed", "running")):
        scheduled = now - timedelta(minutes=3 - index)
        rows.append(
            (
                str(uuid.uuid4()),
                "scheduled",
                None,
                f"Schedule {index}",
                None,
                "Run setup",
                "Flow",
                scheduled.isoformat(),
                scheduled.isoformat() if outcome == "running" else None,
                scheduled.isoformat() if outcome == "completed" else None,
                outcome,
                "",
            )
        )
    database._conn.executemany(
        """
        INSERT INTO run_history (
            run_id, source, schedule_id, schedule_name, run_config_id,
            run_config_name, flow_name, scheduled_for_utc, started_at_utc,
            finished_at_utc, outcome, message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    database._conn.commit()

    assert repository.prune_history(
        now_utc=now,
        retention_days=90,
        max_records=2,
        batch_size=10,
    ) == 1

    remaining = repository.list_history(limit=10)
    assert len(remaining) == 2
    assert {record.outcome for record in remaining} == {
        RunHistoryOutcome.COMPLETED,
        RunHistoryOutcome.RUNNING,
    }
    database.close()
